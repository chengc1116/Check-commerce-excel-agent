# -*- coding: utf-8 -*-
"""
飞书机器人 - 长连接(WebSocket)模式

核心流程:
    1. 使用 lark-oapi SDK 建立WebSocket长连接
    2. 监听 im.message.receive_v1 事件
    3. 用户上传Excel -> 回复"已收到" -> 后台线程下载+审核 -> 回复结果卡片

不需要公网IP，不需要内网穿透。

关键设计:
    - 飞书SDK回调是同步的，审核任务用独立线程执行
    - 工作线程创建独立的 lark.Client 实例（SDK Client 非线程安全）
    - 工作线程内用 asyncio.run() 执行异步LLM并行评分
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

from product_review_agent.reviewer import review_excel
from product_review_agent.feishu.file_handler import (
    download_message_file,
    is_excel_file,
)
from product_review_agent.feishu.card_builder import (
    build_review_card,
)

logger = logging.getLogger(__name__)

# 飞书 App 凭证
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "cli_a95f771655fa1bce")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "jBeC63k7Mcts4yRuZIOW9gfKuI8WaRO8")


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


# ============================================================
# 后台审核任务（独立线程，独立Client）
# ============================================================

def _run_review_in_thread(message_id: str, chat_id: str, file_key: str, file_name: str):
    """
    在独立线程中执行完整审核流程。
    
    关键: 创建独立的 lark.Client，不复用主线程的实例。
    """
    tname = threading.current_thread().name
    logger.info(f"[{tname}] ========== 审核线程启动 ==========")
    logger.info(f"[{tname}] 文件: {file_name}, file_key: {file_key[:20]}...")

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

        # Step 2: 执行审核（异步并行LLM评分）
        logger.info(f"[{tname}] Step 2/3: 开始异步评分（人群/场景/九宫格 并行）...")
        t0 = time.time()
        result = asyncio.run(review_excel(local_path))
        t1 = time.time()
        logger.info(f"[{tname}] 评分完成，耗时 {(t1-t0):.1f}s, 综合分={result.overall_score}")

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

        result_card = build_review_card(
            file_name=file_name,
            overall_score=result.overall_score,
            risk_level=result.risk_level,
            scores=result.scores,
            elapsed=result.elapsed_seconds,
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
            # 飞书单条消息长度限制，分段发送
            report_text = result.report
            chunk_size = 3000
            for i in range(0, len(report_text), chunk_size):
                chunk = report_text[i:i + chunk_size]
                reply_text_message(worker_client, message_id, chunk)
            logger.info(f"[{tname}] 完整报告已发送")

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

        message_id = message.message_id
        chat_id = message.chat_id
        msg_type = message.message_type
        content_str = message.content

        logger.info(f"收到消息: chat_id={chat_id}, msg_type={msg_type}")

        # 只处理文件类型
        if msg_type != "file":
            if msg_type == "text":
                try:
                    text_content = json.loads(content_str)
                    text = text_content.get("text", "").strip()
                    if text and not text.startswith("@_user"):
                        client = get_feishu_client()
                        reply_text_message(
                            client,
                            message_id,
                            "请直接发送Excel文件(.xlsx)进行立项审核。\n\n"
                            "支持格式: .xlsx / .xls\n"
                            "审核维度: 人群分析 / 场景分析 / 九宫格目标"
                        )
                except (json.JSONDecodeError, AttributeError):
                    pass
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
                client,
                message_id,
                f"不支持的文件格式: {file_name}\n请发送Excel文件(.xlsx / .xls)。"
            )
            return

        logger.info(f"收到Excel文件: {file_name}, file_key={file_key}")

        # 立即回复"已收到"（在SDK回调线程中，用主线程Client）
        client = get_feishu_client()
        reply_text_message(
            client,
            message_id,
            f"文件已接收: {file_name}\n"
            f"正在分析中，预计需要 1-2 分钟，请稍候...\n"
            f"分析完成后会自动推送审核报告。"
        )

        # 在独立线程中执行审核（独立Client，避免线程安全）
        thread = threading.Thread(
            target=_run_review_in_thread,
            args=(message_id, chat_id, file_key, file_name),
            name=f"review-{file_name[:15]}",
            daemon=True,
        )
        thread.start()
        logger.info(f"审核线程已启动: {thread.name}")

    except Exception as e:
        logger.error(f"处理消息事件异常: {e}", exc_info=True)


# ============================================================
# 长连接启动
# ============================================================

def start_bot():
    """启动飞书长连接Bot"""
    logger.info("=" * 60)
    logger.info("  产品立项审核 - 飞书Bot (长连接模式)")
    logger.info("=" * 60)
    logger.info(f"  App ID: {FEISHU_APP_ID[:10]}...")
    logger.info(f"  连接模式: WebSocket 长连接 (无需公网IP)")
    logger.info("")

    # 创建事件处理器
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
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
    logger.info("连接成功后，用户可在飞书中发送Excel文件给机器人进行审核。")
    logger.info("按 Ctrl+C 退出。")
    logger.info("")

    # 启动长连接（阻塞）
    ws_client.start()
