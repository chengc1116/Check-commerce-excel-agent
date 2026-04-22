# -*- coding: utf-8 -*-
"""
飞书Bot启动入口 - 长连接WebSocket模式

使用方法:
    python run_feishu_bot.py

前置条件:
    1. 在飞书开放平台创建应用，开启机器人能力
    2. 事件订阅选择"使用长连接接收事件"
    3. 添加事件: im.message.receive_v1
    4. 配置 .env 文件:
       - LLM_API_KEY (用于AI评分)
       - LLM_MODEL (文本模型，默认 Qwen/Qwen2.5-7B-Instruct)
       - LLM_VL_MODEL (视觉模型，默认 Qwen/Qwen3-VL-8B-Instruct)
"""

import io
import logging
import os
import sys
from pathlib import Path

# Windows 编码处理
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 .env 环境变量（在所有 import 之前）
from product_review_agent.load_env import load_env
load_env(PROJECT_ROOT / ".env")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    # 检查依赖
    try:
        import lark_oapi
        ver = getattr(lark_oapi, "__version__", "unknown")
        logger.info(f"lark-oapi 已安装 (version: {ver})")
    except ImportError:
        logger.error("缺少依赖: lark-oapi")
        logger.error("请执行: pip install lark-oapi>=1.4.0")
        sys.exit(1)

    try:
        import openai
        ver = getattr(openai, "__version__", "unknown")
        logger.info(f"openai 版本: {ver}")
    except ImportError:
        logger.warning("缺少依赖: openai (LLM评分将回退到规则引擎)")
        logger.warning("如需AI评分，请执行: pip install openai")

    # 检查LLM配置
    llm_key = os.getenv("LLM_API_KEY", "")
    if llm_key:
        model = os.getenv("LLM_MODEL", "unknown")
        vl_model = os.getenv("LLM_VL_MODEL", "unknown")
        logger.info(f"LLM配置: model={model}, vl_model={vl_model}")
    else:
        logger.warning("LLM_API_KEY 未设置，将使用规则引擎回退模式")
        logger.warning("如需AI评分，请设置环境变量: LLM_API_KEY")

    # 显示飞书配置
    app_id = os.getenv("FEISHU_APP_ID", "cli_a95f771655fa1bce")
    logger.info(f"飞书App ID: {app_id[:10]}...")
    logger.info("")

    # 启动Bot
    from product_review_agent.feishu.bot import start_bot
    start_bot()


if __name__ == "__main__":
    main()
