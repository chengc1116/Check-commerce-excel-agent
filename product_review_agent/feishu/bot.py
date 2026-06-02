# -*- coding: utf-8 -*-
"""
飞书机器人 - 长连接(WebSocket)模式

核心流程:
    1. 用户发文本消息 → 弹出任务选择卡片
    2. 用户点击选择任务类型 → 记录到会话，提示上传文件
    3. 用户上传Excel → 根据任务类型执行对应审核 → 回复结果卡片

不需要公网IP，不需要内网穿透。

关键设计:
    - 飞书SDK回调是同步的，审核任务用独立线程执行
    - 工作线程创建独立的 lark.Client 实例（SDK Client 非线程安全）
    - 工作线程内用 asyncio.run() 执行异步LLM并行评分
    - 用户会话状态存储在内存中（SessionManager）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from product_review_agent.pipeline import run_pipeline
from product_review_agent.feishu.file_handler import (
    download_message_file,
    is_excel_file,
)
from product_review_agent.feishu.card_builder import (
    build_review_card,
    build_task_selection_card,
    build_task_selected_card,
    build_no_task_selected_card,
)
from product_review_agent.feishu.bitable_writer import write_review_record
from product_review_agent.feishu.session_manager import (
    SessionManager,
    SessionState,
    TaskType,
    TASK_TYPE_MAP,
)


# ============================================================
# Monkey Patch: 修复 lark-oapi 长连接模式下卡片回调被丢弃的 BUG
# ============================================================
# lark-oapi <= 1.5.3 的 ws.Client._handle_data_frame 对 MessageType.CARD
# 类型直接 return，导致卡片按钮回调完全无法到达 event_handler。
# 修复: 当 message_type == CARD 时，也调用 event_handler.do() 处理。
#
# 参考: https://github.com/larksuite/oapi-sdk-python/issues
# ============================================================

def _patch_ws_client_card_handler():
    """对 lark.ws.Client 打补丁，修复卡片回调被丢弃的问题"""
    import base64
    import http
    import time

    from lark_oapi.core.json import JSON
    from lark_oapi.core.const import UTF_8
    from lark_oapi.ws.model import Response
    from lark_oapi.ws.enum import MessageType
    from lark_oapi.ws.const import (
        HEADER_MESSAGE_ID, HEADER_TRACE_ID,
        HEADER_SUM, HEADER_SEQ, HEADER_TYPE, HEADER_BIZ_RT,
    )
    from lark_oapi.ws.client import _get_by_key

    _patch_logger = logging.getLogger(__name__)

    async def _patched_handle_data_frame(self, frame):
        """修复版: CARD 类型消息也会分发给 event_handler
        
        关键修改：
        1. CARD 消息分发给 event_handler（原版直接 return 丢弃）
        2. CARD 响应不往 WebSocket 回写 result data（飞书服务端不期望收到）
           只回写一个空的 OK 响应即可
        """
        hs = frame.headers
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        type_ = _get_by_key(hs, HEADER_TYPE)

        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return

        message_type = MessageType(type_)
        _patch_logger.debug(
            f"[WS] receive message, message_type={message_type.value}, "
            f"message_id={msg_id}, trace_id={trace_id}"
        )

        resp = Response(code=http.HTTPStatus.OK)
        try:
            start = int(round(time.time() * 1000))
            if message_type == MessageType.EVENT:
                result = self._event_handler.do_without_validation(pl)
            elif message_type == MessageType.CARD:
                # 🔧 修复: 原版直接 return 丢弃卡片回调
                # 改为: 分发给 event_handler 处理业务逻辑
                if self._event_handler:
                    self._event_handler.do_without_validation(pl)
                # ⚠️ 关键: CARD 回调不往 resp.data 写 result
                # 飞书服务端对 CARD 类型不期望收到序列化的响应数据
                # 写了会导致 200672 错误
                _patch_logger.info(f"[WS] 卡片回调已处理: msg_id={msg_id}")
            else:
                return
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if message_type == MessageType.EVENT and result is not None:
                # 只有 EVENT 类型才回写 result data
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as e:
            _patch_logger.error(
                f"[WS] handle message failed, message_type={message_type.value}, "
                f"message_id={msg_id}, trace_id={trace_id}, err={e}",
                exc_info=True,
            )
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())

    # 应用补丁
    lark.ws.Client._handle_data_frame = _patched_handle_data_frame
    _patch_logger.info("Monkey patch 已应用: lark.ws.Client._handle_data_frame (修复卡片回调，不回写data)")

# 应用补丁（模块加载时立即生效）
_patch_ws_client_card_handler()

logger = logging.getLogger(__name__)

# 飞书 App 凭证（从环境变量读取，无默认值防止误用旧凭证）
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

# 审核报告额外接收人（逗号分隔的 open_id 列表）
_REPORT_RECEIVERS_RAW = os.getenv("FEISHU_REPORT_RECEIVERS", "")
REPORT_RECEIVERS: list[str] = [
    uid.strip() for uid in _REPORT_RECEIVERS_RAW.split(",") if uid.strip()
]

# 全局会话管理器
session_manager = SessionManager()


# ============================================================
# Client 工厂（每个线程创建独立实例）
# ============================================================

_feishu_client_main: Optional[lark.Client] = None


def get_feishu_client() -> lark.Client:
    """获取主线程的飞书客户端实例"""
    global _feishu_client_main
    if _feishu_client_main is None:
        _feishu_client_main = lark.Client.builder() \
            .app_id(FEISHU_APP_ID) \
            .app_secret(FEISHU_APP_SECRET) \
            .log_level(lark.LogLevel.DEBUG) \
            .build()
        logger.info(f"飞书客户端初始化(主线程): app_id={FEISHU_APP_ID[:10]}...")
    return _feishu_client_main


def _create_feishu_client() -> lark.Client:
    """创建全新的飞书客户端实例（用于工作线程，避免线程安全问题）"""
    client = lark.Client.builder() \
        .app_id(FEISHU_APP_ID) \
        .app_secret(FEISHU_APP_SECRET) \
        .log_level(lark.LogLevel.DEBUG) \
        .build()
    logger.info("飞书客户端创建(工作线程): 独立实例")
    return client


# ============================================================
# 消息发送
# ============================================================

def reply_text_message(client: lark.Client, message_id: str, text: str):
    """回复文本消息"""
    request = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}))
            .build()
        ) \
        .build()

    response = client.im.v1.message.reply(request)
    if not response.success():
        logger.error(f"回复文本失败: code={response.code}, msg={response.msg}")
    else:
        logger.info(f"回复文本成功: message_id={message_id}")
    return response


def reply_card_message(client: lark.Client, message_id: str, card: dict):
    """回复消息卡片"""
    request = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        ) \
        .build()

    response = client.im.v1.message.reply(request)
    if not response.success():
        logger.error(f"回复卡片失败: code={response.code}, msg={response.msg}")
    else:
        logger.info(f"回复卡片成功: message_id={message_id}")
    return response


def send_card_to_chat(client: lark.Client, chat_id: str, card: dict):
    """直接往聊天发送卡片（非回复，作为备用方案）"""
    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        ) \
        .build()

    response = client.im.v1.message.create(request)
    if not response.success():
        logger.error(f"发送卡片失败: code={response.code}, msg={response.msg}")
    else:
        logger.info(f"发送卡片成功: chat_id={chat_id}")
    return response


def _upload_file_to_feishu(client: lark.Client, file_path: str) -> Optional[str]:
    """
    上传文件到飞书，返回 file_key。

    使用 im/v1/files 接口上传（适用于消息中发送文件）。
    file 参数需要传入 IO 对象（文件句柄），且在请求完成前保持打开。
    """
    try:
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        file_name = os.path.basename(file_path)

        with open(file_path, "rb") as f:
            body = CreateFileRequestBody.builder() \
                .file_type("stream") \
                .file_name(file_name) \
                .file(f) \
                .build()

            request = CreateFileRequest.builder() \
                .request_body(body) \
                .build()

            response = client.im.v1.file.create(request)

        if response.success():
            file_key = response.data.file_key
            logger.info(f"文件上传成功: file_key={file_key}, name={file_name}")
            return file_key
        else:
            logger.error(f"文件上传失败: code={response.code}, msg={response.msg}")
            return None

    except Exception as e:
        logger.error(f"文件上传异常: {e}", exc_info=True)
        return None


def _send_file_message(client: lark.Client, chat_id: str, file_key: str, file_name: str):
    """发送文件消息到聊天"""
    try:
        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("file")
                .content(json.dumps({"file_key": file_key, "file_name": file_name}))
                .build()
            ) \
            .build()

        response = client.im.v1.message.create(request)
        if response.success():
            logger.info(f"文件消息发送成功: chat_id={chat_id}, file={file_name}")
        else:
            logger.error(f"文件消息发送失败: code={response.code}, msg={response.msg}")
        return response

    except Exception as e:
        logger.error(f"发送文件消息异常: {e}", exc_info=True)
        return None


def _send_file_to_user(client: lark.Client, open_id: str, file_key: str, file_name: str):
    """发送文件消息到指定用户（通过open_id）"""
    try:
        request = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("file")
                .content(json.dumps({"file_key": file_key, "file_name": file_name}))
                .build()
            ) \
            .build()

        response = client.im.v1.message.create(request)
        if response.success():
            logger.info(f"文件消息发送成功: user={open_id}, file={file_name}")
        else:
            logger.error(f"文件消息发送失败: user={open_id}, code={response.code}, msg={response.msg}")
        return response

    except Exception as e:
        logger.error(f"发送文件消息异常: {e}", exc_info=True)
        return None


# ============================================================
# 用户信息 & 通知消息
# ============================================================

def _get_user_name(client: lark.Client, open_id: str) -> str:
    """通过飞书API获取用户显示名称，失败时返回open_id"""
    try:
        from lark_oapi.api.contact.v3 import GetUserRequest

        request = GetUserRequest.builder() \
            .user_id(open_id) \
            .user_id_type("open_id") \
            .build()

        response = client.contact.v3.user.get(request)
        if response.success() and response.data and response.data.user:
            return response.data.user.name or open_id
        else:
            logger.warning(f"获取用户名失败: {open_id}, code={response.code}, msg={response.msg}")
            return open_id
    except Exception as e:
        logger.warning(f"获取用户名异常: {open_id}, {e}")
        return open_id


def _send_text_to_user(client: lark.Client, open_id: str, text: str):
    """发送文本消息到指定用户（通过open_id）"""
    try:
        request = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            ) \
            .build()

        response = client.im.v1.message.create(request)
        if response.success():
            logger.info(f"文本消息发送成功: user={open_id}")
        else:
            logger.error(f"文本消息发送失败: user={open_id}, code={response.code}, msg={response.msg}")
        return response

    except Exception as e:
        logger.error(f"发送文本消息异常: {e}", exc_info=True)
        return None


def _build_receiver_notification(
    sender_name: str,
    product_name: str,
    task_label: str,
    overall_score: int,
    risk_level: str,
    file_name: str,
) -> str:
    """构建发送给接收人的通知消息"""
    risk_emoji = {"低": "✅", "中": "⚠️", "高": "❌"}.get(risk_level, "❓")
    return (
        f"📋 立项审核报告通知\n"
        f"━━━━━━━━━━━━━━\n"
        f"提交人: {sender_name}\n"
        f"产品名称: {product_name}\n"
        f"审核类型: {task_label}\n"
        f"文件名称: {file_name}\n"
        f"综合评分: {overall_score}/100 {risk_emoji}\n"
        f"风险等级: {risk_level}\n"
        f"━━━━━━━━━━━━━━\n"
        f"以下为审核报告和原始文件，请查收。"
    )


# ============================================================
# 后台审核任务（独立线程，独立Client）
# ============================================================

def _run_review_in_thread(
    message_id: str,
    chat_id: str,
    file_key: str,
    file_name: str,
    task_type: Optional[TaskType] = None,
    user_id: str = "",
):
    """
    在独立线程中执行完整审核流程。
    
    关键: 创建独立的 lark.Client，不复用主线程的实例。
    使用 pipeline.py 编排全流程: Excel解析 → 图片提取 → 公共+专项分析 → 报告整合。
    """
    tname = threading.current_thread().name
    task_label = task_type.label if task_type else "通用审核"
    task_emoji = task_type.emoji if task_type else "📋"
    task_type_str = task_type.value if task_type else "hot_upgrade"
    logger.info(f"[{tname}] ========== 审核线程启动 ==========")
    logger.info(f"[{tname}] 任务类型: {task_label}, 文件: {file_name}")

    # 在工作线程创建独立的 Client 实例
    try:
        worker_client = _create_feishu_client()
        logger.info(f"[{tname}] Client 创建成功")
    except Exception as e:
        logger.error(f"[{tname}] Client 创建失败: {e}", exc_info=True)
        return

    try:
        # Step 1: 下载文件
        logger.info(f"[{tname}] Step 1/3: 下载文件...")
        save_dir = os.path.join(tempfile.gettempdir(), "feishu_review")
        os.makedirs(save_dir, exist_ok=True)

        local_path = download_message_file(
            worker_client, message_id, file_key, save_dir=save_dir
        )

        if not local_path:
            logger.error(f"[{tname}] 文件下载失败")
            reply_text_message(worker_client, message_id, "文件下载失败，请重新上传。")
            return

        # 重命名为原文件名
        if file_name and is_excel_file(file_name):
            new_path = os.path.join(save_dir, file_name)
            try:
                os.rename(local_path, new_path)
                local_path = new_path
            except OSError:
                pass

        logger.info(f"[{tname}] 文件下载成功: {local_path}")

        # Step 2: 执行 Pipeline 审核
        logger.info(f"[{tname}] Step 2/3: Pipeline审核（类型: {task_label}）...")
        t0 = time.time()
        result = asyncio.run(run_pipeline(local_path, task_type=task_type_str))
        t1 = time.time()
        logger.info(f"[{tname}] 审核完成，耗时 {(t1-t0):.1f}s, 综合分={result.overall_score}")

        # Step 3: 发送审核结果
        logger.info(f"[{tname}] Step 3/3: 发送审核结果卡片...")

        if result.error:
            error_card = build_review_card(
                file_name=file_name,
                overall_score=0,
                risk_level="未知",
                scores={},
                elapsed=0,
                error=result.error,
            )
            reply_card_message(worker_client, message_id, error_card)
            logger.error(f"[{tname}] 审核出错: {result.error}")
            return

        # Step 3.5: 写入飞书多维表格
        pd = result.project_data or {}
        product_name = pd.get("project_name") or pd.get("product_name", "未知")
        category_l1 = pd.get("category_l1") or pd.get("categoryl1", "")
        sender_name = _get_user_name(worker_client, user_id)
        try:
            write_review_record(
                client=worker_client,
                product_name=product_name,
                category_l1=category_l1,
                task_label=result.task_label or task_label,
                overall_score=result.overall_score,
                risk_level=result.risk_level,
                submitter=sender_name,
                file_name=file_name,
            )
            logger.info(f"[{tname}] 多维表格写入完成")
        except Exception as bt_err:
            logger.warning(f"[{tname}] 多维表格写入失败（不影响主流程）: {bt_err}")

        # 构建评分信息（兼容旧卡片格式）
        scores = result.common_scores or {}

        result_card = build_review_card(
            file_name=file_name,
            overall_score=result.overall_score,
            risk_level=result.risk_level,
            scores=scores,
            elapsed=result.elapsed_seconds,
            product_analysis=None,  # 已整合到报告中
            specific_score=result.specific_score,
            task_label=result.task_label,
        )

        # 发送评分卡片
        resp = reply_card_message(worker_client, message_id, result_card)
        if resp and resp.success():
            logger.info(f"[{tname}] 评分卡片已发送(reply)")
        else:
            logger.warning(f"[{tname}] reply 失败，改用 send_card_to_chat")
            send_card_to_chat(worker_client, chat_id, result_card)

        # Step 4: 生成并发送 Word 审核报告
        logger.info(f"[{tname}] Step 4/4: 生成 Word 报告...")
        try:
            from product_review_agent.docx_generator import generate_review_docx

            # 将 specific_score 转为 dict（兼容 AnalyzerScore 对象）
            specific_dict = {}
            if result.specific_score:
                if hasattr(result.specific_score, "total_score"):
                    # AnalyzerScore 对象 → dict
                    specific_dict = {
                        "total_score": result.specific_score.total_score,
                        "dimensions": [
                            {"name": d.name, "score": d.score, "max_score": d.max_score, "reason": d.reason}
                            if hasattr(d, "name") else d
                            for d in (result.specific_score.dimensions or [])
                        ],
                        "strengths": result.specific_score.strengths or [],
                        "weaknesses": result.specific_score.weaknesses or [],
                        "suggestions": result.specific_score.suggestions or [],
                    }
                elif isinstance(result.specific_score, dict):
                    specific_dict = result.specific_score

            common_dict = result.common_scores or {}

            docx_path = generate_review_docx(
                file_name=file_name,
                task_label=result.task_label or task_label,
                overall_score=result.overall_score,
                risk_level=result.risk_level,
                project_data=result.project_data or {},
                specific_score=specific_dict,
                specific_analysis=result.specific_analysis or {},
                common_scores=common_dict,
                report_text=result.report or "",
            )
            logger.info(f"[{tname}] Word 报告已生成: {docx_path}")

            # 上传文件到飞书并发送给提交人
            docx_file_key = _upload_file_to_feishu(worker_client, docx_path)
            if docx_file_key:
                _send_file_message(worker_client, chat_id, docx_file_key, os.path.basename(docx_path))
                logger.info(f"[{tname}] Word 报告已发送到飞书")
            else:
                logger.warning(f"[{tname}] Word 报告上传失败，改发文本摘要")
                reply_text_message(
                    worker_client, message_id,
                    f"Word报告上传失败，综合评分: {result.overall_score}/100，风险等级: {result.risk_level}"
                )

            # Step 5: 将报告和原始Excel发送给配置的额外接收人
            if REPORT_RECEIVERS and docx_file_key:
                logger.info(f"[{tname}] Step 5/5: 发送报告给额外接收人 ({len(REPORT_RECEIVERS)}人)...")
                # 获取提交人名称
                sender_name = _get_user_name(worker_client, user_id)
                # 从项目数据中提取产品名称
                product_name = (result.project_data or {}).get("project_name") \
                    or (result.project_data or {}).get("product_name", "未知")
                # 构建通知消息
                notify_text = _build_receiver_notification(
                    sender_name=sender_name,
                    product_name=product_name,
                    task_label=result.task_label or task_label,
                    overall_score=result.overall_score,
                    risk_level=result.risk_level,
                    file_name=file_name,
                )
                # 上传原始Excel文件
                excel_file_key = _upload_file_to_feishu(worker_client, local_path)
                for uid in REPORT_RECEIVERS:
                    _send_text_to_user(worker_client, uid, notify_text)
                    _send_file_to_user(worker_client, uid, docx_file_key, os.path.basename(docx_path))
                    if excel_file_key:
                        _send_file_to_user(worker_client, uid, excel_file_key, file_name)
                    logger.info(f"[{tname}] 已发送给接收人: {uid}")

            # 清理临时文件
            try:
                os.remove(docx_path)
            except OSError:
                pass

        except Exception as e:
            logger.error(f"[{tname}] 生成/发送 Word 报告失败: {e}", exc_info=True)
            # 降级：发送简短文本摘要
            try:
                reply_text_message(
                    worker_client, message_id,
                    f"报告生成失败，综合评分: {result.overall_score}/100，风险等级: {result.risk_level}"
                )
            except Exception:
                pass

        logger.info(f"[{tname}] ========== 审核线程完成 ==========")

    except Exception as e:
        logger.error(f"[{tname}] 审核线程异常: {e}", exc_info=True)
        try:
            reply_text_message(worker_client, message_id, f"审核处理出错: {str(e)[:200]}")
        except Exception:
            pass


# ============================================================
# 事件处理器（飞书SDK同步回调，在SDK内部线程执行）
# ============================================================

def on_message_receive(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """处理接收到的消息事件"""
    try:
        event = data.event
        message = event.message
        sender = event.sender

        message_id = message.message_id
        chat_id = message.chat_id
        msg_type = message.message_type
        content_str = message.content

        # 获取用户标识（用 sender.sender_id.open_id 或 user_id）
        user_id = sender.sender_id.open_id if sender and sender.sender_id else "unknown"

        logger.info(f"收到消息: user={user_id}, msg_type={msg_type}")

        # ---- 文本消息: 弹出任务选择卡片 ----
        if msg_type == "text":
            try:
                text_content = json.loads(content_str)
                text = text_content.get("text", "").strip()
                # 群聊中 @机器人 的文本格式: @_user_1 实际内容
                # 去掉所有 @_user_N 提及标记，只保留实际文本
                import re
                text = re.sub(r"@_user_\d+\s*", "", text).strip()
                if not text:
                    # 纯 @机器人 无其他文字，也弹出选择卡片
                    text = "选择任务"

                client = get_feishu_client()

                # 检查用户是否已选择了任务
                session = session_manager.get_or_create(user_id, chat_id)
                if session.is_waiting_file:
                    # 已选择任务，重新弹出选择卡片（允许更换）
                    task = session.task_type
                    card = build_task_selection_card()
                    # 在卡片前加一段提示，告诉用户当前选择
                    try:
                        reply_text_message(
                            client, message_id,
                            f"当前已选择 {task.emoji} {task.label}，请重新选择或直接上传Excel文件："
                        )
                    except Exception:
                        pass
                    reply_card_message(client, message_id, card)
                else:
                    # 弹出任务选择卡片
                    card = build_task_selection_card()
                    reply_card_message(client, message_id, card)

            except (json.JSONDecodeError, AttributeError):
                pass
            return

        # ---- 文件消息: 检查任务类型后审核 ----
        if msg_type != "file":
            return

        # 解析文件消息
        content = json.loads(content_str)
        file_key = content.get("file_key", "")
        file_name = content.get("file_name", "")

        if not file_key:
            logger.warning("消息中无file_key")
            return

        if not is_excel_file(file_name):
            client = get_feishu_client()
            reply_text_message(
                client, message_id,
                f"不支持的文件格式: {file_name}\n请发送Excel文件(.xlsx / .xls)。"
            )
            return

        # 检查用户是否已选择任务类型
        client = get_feishu_client()
        task_type = session_manager.consume_task(user_id)

        if not task_type:
            # 未选择任务类型，提示先选择
            card = build_no_task_selected_card()
            reply_card_message(client, message_id, card)
            return

        logger.info(f"收到Excel: {file_name}, 任务: {task_type.label}, user: {user_id}")

        # 立即回复"已收到"
        reply_text_message(
            client, message_id,
            f"{task_type.emoji} {task_type.label} 审核\n"
            f"文件已接收: {file_name}\n"
            f"正在分析中，预计需要 3-5 分钟，请稍候...\n"
            f"分析完成后会自动推送审核报告。"
        )

        # 在独立线程中执行审核
        thread = threading.Thread(
            target=_run_review_in_thread,
            args=(message_id, chat_id, file_key, file_name, task_type, user_id),
            name=f"review-{file_name[:15]}",
            daemon=True,
        )
        thread.start()
        logger.info(f"审核线程已启动: {thread.name}, 任务: {task_type.label}")

    except Exception as e:
        logger.error(f"处理消息事件异常: {e}", exc_info=True)


def on_card_action_trigger(data) -> dict:
    """
    处理飞书卡片交互回调（按钮点击）。
    
    用户点击任务选择按钮时触发。
    通过 EventDispatcherHandler.register_p2_card_action_trigger 注册。
    
    支持两种动作:
    - task_type=xxx: 选择任务类型
    - action=change_task: 更换任务类型（重新弹出选择卡片）
    
    注意：长连接模式下，回调返回值无法直接更新卡片（会触发 200672 错误），
    因此改用主动发消息的方式确认用户选择。
    """
    try:
        # 从回调数据中提取 action 和用户信息
        action = data.event.action
        operator = data.event.operator
        context = data.event.context

        user_id = operator.open_id if operator else ""
        chat_id = context.open_chat_id if context else ""

        logger.info(f"卡片回调: user={user_id}, chat={chat_id}")

        # 获取用户点击的按钮 value
        action_value = {}
        if action and action.value:
            if isinstance(action.value, str):
                try:
                    action_value = json.loads(action.value)
                except json.JSONDecodeError:
                    logger.warning(f"action.value JSON解析失败: {action.value}")
            elif isinstance(action.value, dict):
                action_value = action.value
            else:
                logger.warning(f"action.value 类型异常: {type(action.value)} = {action.value}")

        # ---- 处理「更换任务类型」动作 ----
        action_name = action_value.get("action", "")
        if action_name == "change_task":
            logger.info(f"卡片回调: user={user_id} 请求更换任务类型")
            # 重置会话状态
            session_manager.reset(user_id)
            # 重新弹出任务选择卡片
            try:
                client = get_feishu_client()
                selection_card = build_task_selection_card()
                send_card_to_chat(client, chat_id, selection_card)
            except Exception as e:
                logger.warning(f"发送选择卡片失败: {e}，不影响主流程")
            return {}

        # ---- 处理「选择任务类型」动作 ----
        task_type_str = action_value.get("task_type", "")
        logger.info(f"卡片回调: user={user_id}, chat={chat_id}, task_type_str={task_type_str}")

        task_type = TASK_TYPE_MAP.get(task_type_str)

        if not task_type:
            logger.warning(f"未知的任务类型: {task_type_str}, 可选: {list(TASK_TYPE_MAP.keys())}")
            return {}

        # 记录用户选择
        session_manager.set_task(user_id, task_type, chat_id, "")
        logger.info(f"卡片回调: user={user_id} 选择了 {task_type.label}")

        # 主动发送确认消息（不依赖回调返回值更新卡片，避免 200672 错误）
        try:
            client = get_feishu_client()
            confirm_card = build_task_selected_card(task_type.label, task_type.emoji)
            # 使用 chat_id 直接发送卡片消息
            send_card_to_chat(client, chat_id, confirm_card)
        except Exception as e:
            logger.warning(f"发送确认卡片失败: {e}，不影响主流程")

        # 返回空 dict，不触发 SDK 序列化响应
        return {}

    except Exception as e:
        logger.error(f"处理卡片回调异常: {e}", exc_info=True)
        return {}


# ============================================================
# 长连接启动
# ============================================================

def start_bot():
    """启动飞书长连接Bot"""
    # 检查凭证
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        logger.error("=" * 60)
        logger.error("  飞书凭证未配置！请在 .env 中设置：")
        logger.error("    FEISHU_APP_ID=cli_xxxxxxxx")
        logger.error("    FEISHU_APP_SECRET=xxxxxxxxxxxxxxxx")
        logger.error("=" * 60)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("  产品立项审核 - 飞书Bot (长连接模式)")
    logger.info("=" * 60)
    logger.info(f"  App ID: {FEISHU_APP_ID[:10]}...")
    logger.info(f"  连接模式: WebSocket 长连接 (无需公网IP)")
    logger.info(f"  支持任务: 爆品升级 / 竞品升级 / 未起量迭代 / 品类地图缺失")
    logger.info("")

    # 创建事件处理器（消息事件 + 卡片交互回调，统一注册）
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .register_p2_card_action_trigger(on_card_action_trigger)
        .build()
    )

    # 创建WebSocket客户端
    ws_client = lark.ws.Client(
        app_id=FEISHU_APP_ID,
        app_secret=FEISHU_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )

    logger.info("正在建立WebSocket长连接...")
    logger.info("连接成功后，用户可在飞书中与机器人交互。")
    logger.info("流程: 发送消息 → 选择任务类型 → 上传Excel → 审核报告")
    logger.info("按 Ctrl+C 退出。")
    logger.info("")

    # 启动长连接（阻塞）
    ws_client.start()
