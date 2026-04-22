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
        """修复版: CARD 类型消息也会分发给 event_handler"""
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
                # 改为: 同 EVENT 一样分发给 event_handler
                if self._event_handler:
                    result = self._event_handler.do_without_validation(pl)
                else:
                    result = None
                _patch_logger.info(f"[WS] 卡片回调已处理: msg_id={msg_id}")
            else:
                return
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
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
    _patch_logger.info("Monkey patch 已应用: lark.ws.Client._handle_data_frame (修复卡片回调)")

# 应用补丁（模块加载时立即生效）
_patch_ws_client_card_handler()

logger = logging.getLogger(__name__)

# 飞书 App 凭证（从环境变量读取，无默认值防止误用旧凭证）
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

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
    """
    try:
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        file_name = os.path.basename(file_path)

        with open(file_path, "rb") as f:
            file_data = f.read()

        request = CreateFileRequest.builder() \
            .request_body(
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(file_name)
                .file(file_data)
                .build()
            ) \
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


# ============================================================
# 后台审核任务（独立线程，独立Client）
# ============================================================

def _run_review_in_thread(
    message_id: str,
    chat_id: str,
    file_key: str,
    file_name: str,
    task_type: Optional[TaskType] = None,
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

        # 发送完整审核报告（文本消息）
        if result.report:
            report_text = result.report
            chunk_size = 3000
            for i in range(0, len(report_text), chunk_size):
                chunk = report_text[i:i + chunk_size]
                reply_text_message(worker_client, message_id, chunk)
            logger.info(f"[{tname}] 完整报告已发送")

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
                common_scores=common_dict,
                report_text=result.report or "",
            )
            logger.info(f"[{tname}] Word 报告已生成: {docx_path}")

            # 上传文件到飞书并发送
            file_key = _upload_file_to_feishu(worker_client, docx_path)
            if file_key:
                _send_file_message(worker_client, chat_id, file_key, os.path.basename(docx_path))
                logger.info(f"[{tname}] Word 报告已发送到飞书")
            else:
                logger.warning(f"[{tname}] Word 报告上传失败，跳过发送")

            # 清理临时文件
            try:
                os.remove(docx_path)
            except OSError:
                pass

        except Exception as e:
            logger.error(f"[{tname}] 生成/发送 Word 报告失败: {e}", exc_info=True)

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
                    # 已选择任务，提示继续上传
                    task = session.task_type
                    reply_text_message(
                        client, message_id,
                        f"您已选择 {task.emoji} {task.label}，请直接上传Excel文件即可。\n"
                        f"如需更换任务类型，请发送任意消息重新选择。"
                    )
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
            f"正在分析中，预计需要 1-2 分钟，请稍候...\n"
            f"分析完成后会自动推送审核报告。"
        )

        # 在独立线程中执行审核
        thread = threading.Thread(
            target=_run_review_in_thread,
            args=(message_id, chat_id, file_key, file_name, task_type),
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
    
    data 类型: P2CardActionTrigger
      - data.event.action.value: 按钮的 value (Dict)
      - data.event.operator.open_id: 操作用户 ID
      - data.event.context.open_chat_id: 聊天 ID
    
    返回 P2CardActionTriggerResponse 可更新卡片内容 + 弹 toast。
    """
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
        CallBackToast,
        CallBackCard,
    )

    try:
        # 从回调数据中提取 action 和用户信息
        action = data.event.action
        operator = data.event.operator
        context = data.event.context

        user_id = operator.open_id if operator else ""
        chat_id = context.open_chat_id if context else ""

        logger.info(f"卡片回调原始数据: action={action}, operator={operator}, context={context}")

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

        task_type_str = action_value.get("task_type", "")
        logger.info(f"卡片回调: user={user_id}, chat={chat_id}, task_type_str={task_type_str}")

        task_type = TASK_TYPE_MAP.get(task_type_str)

        if not task_type:
            logger.warning(f"未知的任务类型: {task_type_str}, 可选: {list(TASK_TYPE_MAP.keys())}")
            resp = P2CardActionTriggerResponse()
            resp.toast = CallBackToast({"type": "error", "content": f"未知的任务类型: {task_type_str}"})
            return resp

        # 记录用户选择
        session_manager.set_task(user_id, task_type, chat_id, "")
        logger.info(f"卡片回调: user={user_id} 选择了 {task_type.label}")

        # 返回确认：toast 提示 + 更新卡片内容
        # CallBackCard 需要 {"type": "update", "data": card_dict} 格式
        resp = P2CardActionTriggerResponse()
        resp.toast = CallBackToast({
            "type": "success",
            "content": f"已选择 {task_type.emoji} {task_type.label}，请上传Excel文件"
        })
        resp.card = CallBackCard({
            "type": "update",
            "data": build_task_selected_card(task_type.label, task_type.emoji),
        })
        return resp

    except Exception as e:
        logger.error(f"处理卡片回调异常: {e}", exc_info=True)
        resp = P2CardActionTriggerResponse()
        resp.toast = CallBackToast({"type": "error", "content": "处理失败，请重试"})
        return resp


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
