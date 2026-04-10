# -*- coding: utf-8 -*-
"""
LLM 统一调用层（支持异步）

支持通过环境变量灵活配置模型供应商：
  LLM_PROVIDER   = openai | deepseek | zhipu | qwen | ollama  (default: openai)
  LLM_MODEL      = gpt-4o | deepseek-chat | glm-4 | qwen-plus | ...
  LLM_API_KEY    = 你的 API Key
  LLM_BASE_URL   = 自定义 API Endpoint（兼容 OpenAI 格式的服务均可用）
  LLM_TEMPERATURE = 0.3 (default)
  LLM_MAX_TOKENS  = 2048 (default)

所有供应商统一使用 OpenAI 兼容协议，只需设置 base_url 即可切换。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ============================================================
# 配置读取
# ============================================================

_PROVIDER_DEFAULTS: dict[str, dict] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
    },
    "siliconflow":{
        "base_url": "https://api.siliconflow.cn/v1",
        "model":"Qwen/Qwen3.5-27B",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "llama3",
    },
}

PROVIDER = (os.getenv("LLM_PROVIDER") or "siliconflow").lower()
_defaults = _PROVIDER_DEFAULTS.get(PROVIDER, _PROVIDER_DEFAULTS["siliconflow"])

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or _defaults["base_url"]
LLM_MODEL = os.getenv("LLM_MODEL") or _defaults["model"]
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3") or "0.3")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8192") or "8192")


# ============================================================
# LLMClient（同步 + 异步）
# ============================================================

class LLMClient:
    """
    统一 LLM 调用客户端（支持同步和异步）。
    使用 openai Python SDK（兼容所有 OpenAI-compatible 端点）。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        self.api_key = api_key or LLM_API_KEY
        self.base_url = base_url or LLM_BASE_URL
        self.model = model or LLM_MODEL
        self.temperature = temperature if temperature is not None else LLM_TEMPERATURE
        self.max_tokens = max_tokens or LLM_MAX_TOKENS

        if not self.api_key:
            logger.warning(
                "LLM_API_KEY 未设置。如需真实LLM评估，请在环境变量中设置 LLM_API_KEY。"
                "当前将使用规则引擎回退模式。"
            )

        self._client = None
        self._async_client = None

    def _get_client(self):
        """延迟初始化 openai 同步客户端"""
        if self._client is None:
            try:
                from openai import OpenAI
                import httpx
                self._client = OpenAI(
                    api_key=self.api_key or "sk-placeholder",
                    base_url=self.base_url,
                    timeout=httpx.Timeout(120.0, connect=15.0),  # 连接15s，总请求120s
                )
            except ImportError:
                raise RuntimeError(
                    "openai 包未安装，请执行: pip install openai"
                )
        return self._client

    def _get_async_client(self):
        """延迟初始化 openai 异步客户端"""
        if self._async_client is None:
            try:
                from openai import AsyncOpenAI
                import httpx
                self._async_client = AsyncOpenAI(
                    api_key=self.api_key or "sk-placeholder",
                    base_url=self.base_url,
                    timeout=httpx.Timeout(120.0, connect=15.0),  # 连接15s，总请求120s
                )
            except ImportError:
                raise RuntimeError(
                    "openai 包未安装，请执行: pip install openai"
                )
        return self._async_client

    @property
    def is_available(self) -> bool:
        """判断LLM是否可用（有API Key）"""
        return bool(self.api_key)

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: str = "json",
    ) -> dict | str:
        """
        同步调用 LLM。
        """
        client = self._get_client()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        logger.debug(f"LLM 请求: model={self.model}, prompt_len={len(user_prompt)}")

        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as e:
            # 部分供应商不支持 response_format 参数，自动去除重试
            err_msg = str(e)
            if "response_format" in err_msg and "response_format" in kwargs:
                logger.warning(f"供应商不支持 response_format，去除后重试: {err_msg[:100]}")
                kwargs.pop("response_format")
                response = client.chat.completions.create(**kwargs)
            else:
                raise

        content = response.choices[0].message.content or ""

        logger.debug(f"LLM 响应: {content[:200]}...")

        return self._parse_response(content, response_format)

    async def achat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: str = "json",
    ) -> dict | str:
        """
        异步调用 LLM。
        """
        client = self._get_async_client()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        logger.debug(f"LLM 异步请求: model={self.model}, prompt_len={len(user_prompt)}")

        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as e:
            # 部分供应商不支持 response_format 参数，自动去除重试
            err_msg = str(e)
            if "response_format" in err_msg and "response_format" in kwargs:
                logger.warning(f"供应商不支持 response_format，去除后重试: {err_msg[:100]}")
                kwargs.pop("response_format")
                response = await client.chat.completions.create(**kwargs)
            else:
                raise

        content = response.choices[0].message.content or ""

        logger.debug(f"LLM 响应: {content[:200]}...")

        return self._parse_response(content, response_format)

    def chat_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_data: bytes,
        image_ext: str = "png",
        response_format: str = "json",
    ) -> dict | str:
        """
        调用支持视觉的LLM（多模态）。
        用于解析图片中的表格内容。

        Args:
            system_prompt: 系统提示
            user_prompt: 用户提示
            image_data: 图片二进制数据
            image_ext: 图片格式 (png/jpeg/...)
            response_format: "json" 或 "text"
        """
        import base64

        client = self._get_client()

        b64 = base64.b64encode(image_data).decode("utf-8")
        media_type = f"image/{image_ext}" if not image_ext.startswith(".") else f"image/{image_ext.lstrip('.')}"

        user_content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64}"},
            },
            {"type": "text", "text": user_prompt},
        ]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        logger.info(f"LLM Vision 请求: model={self.model}, image_size={len(image_data)}bytes")

        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as e:
            err_msg = str(e)
            if "response_format" in err_msg and "response_format" in kwargs:
                logger.warning(f"Vision: 供应商不支持 response_format，去除后重试")
                kwargs.pop("response_format")
                response = client.chat.completions.create(**kwargs)
            else:
                raise

        content = response.choices[0].message.content or ""
        logger.info(f"LLM Vision 响应: {content[:200]}...")
        return self._parse_response(content, response_format)

    def _parse_response(self, content: str, response_format: str) -> dict | str:
        """解析 LLM 响应"""
        if response_format == "json":
            # 预处理: 去掉 Qwen/thinking 模型的 <think/> 标签
            import re
            content = re.sub(r"<think.*?>.*?</think\s*>", "", content, flags=re.DOTALL).strip()
            content = re.sub(r"</?think\s*>", "", content).strip()

            try:
                return json.loads(content)
            except json.JSONDecodeError:
                # 容错：尝试提取 JSON 块（支持 markdown 代码块包裹的 JSON）
                # 先尝试 ```json ... ``` 代码块
                code_block = re.search(r"```(?:json)?\s*\n?(\{.*?\n?\})\s*\n?```", content, re.DOTALL)
                if code_block:
                    try:
                        return json.loads(code_block.group(1))
                    except json.JSONDecodeError:
                        pass
                # 再尝试裸 JSON（贪婪匹配最外层大括号）
                # 找到第一个 { 和最后一个 }
                first_brace = content.find("{")
                last_brace = content.rfind("}")
                if first_brace != -1 and last_brace > first_brace:
                    try:
                        return json.loads(content[first_brace:last_brace + 1])
                    except json.JSONDecodeError:
                        pass
                logger.warning(f"LLM返回内容无法解析为JSON，返回原始文本: {content[:200]}")
                # 返回包含原始文本的 dict，避免下游崩溃
                return {"_raw_text": content, "_parse_error": True}

        return content


# ============================================================
# 全局单例（懒加载）
# ============================================================
_default_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """获取全局默认LLM客户端"""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
