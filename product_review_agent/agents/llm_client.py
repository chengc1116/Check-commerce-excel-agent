# -*- coding: utf-8 -*-
"""
LLM 统一调用层（双模型 + 同步/异步）

配置方式（全部通过 .env 环境变量）：
  LLM_API_KEY     = 你的 API Key（必填）
  LLM_BASE_URL    = API 端点（如 https://api.siliconflow.cn/v1）
  LLM_MODEL       = 文本模型（如 ）
  LLM_VL_MODEL    = 视觉模型（如 Qwen/Qwen2.5-VL-7B-Instruct）
  LLM_TEMPERATURE = 0.3（默认）
  LLM_MAX_TOKENS  = 20000（默认）

使用 OpenAI 兼容协议，只需设置 base_url 即可切换任意供应商。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ============================================================
# 配置读取（全部从环境变量）
# ============================================================

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "Pro/zai-org/GLM-5")
LLM_FAST_MODEL = os.getenv("LLM_FAST_MODEL", "Qwen/Qwen2.5-7B-Instruct")
LLM_VL_MODEL = os.getenv("LLM_VL_MODEL", "Pro/moonshotai/Kimi-K2.6")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3") or "0.3")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "20000") or "20000")


# ============================================================
# LLMClient（双模型 + 同步/异步）
# ============================================================

class LLMClient:
    """
    统一 LLM 调用客户端。

    持有三个模型：
      - model:      文本模型（call_text / acall_text）— 深度分析
      - fast_model: 快速文本模型（call_text_fast / acall_text_fast）— 简单提取
      - vl_model:   视觉模型（call_vision / acall_vision）

    使用 openai Python SDK（兼容所有 OpenAI-compatible 端点）。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        fast_model: Optional[str] = None,
        vl_model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        self.api_key = api_key or LLM_API_KEY
        self.base_url = base_url or LLM_BASE_URL
        self.model = model or LLM_MODEL
        self.fast_model = fast_model or LLM_FAST_MODEL
        self.vl_model = vl_model or LLM_VL_MODEL
        self.temperature = temperature if temperature is not None else LLM_TEMPERATURE
        self.max_tokens = max_tokens or LLM_MAX_TOKENS

        if not self.api_key:
            logger.warning(
                "LLM_API_KEY 未设置。如需真实LLM评估，请在 .env 中设置 LLM_API_KEY。"
                "当前将使用规则引擎回退模式。"
            )

        self._client = None
        self._async_client = None

    # ----------------------------------------------------------
    # 客户端初始化
    # ----------------------------------------------------------

    def _get_client(self):
        """延迟初始化 openai 同步客户端"""
        if self._client is None:
            try:
                from openai import OpenAI
                import httpx
                self._client = OpenAI(
                    api_key=self.api_key or "sk-placeholder",
                    base_url=self.base_url,
                    timeout=httpx.Timeout(300.0, connect=15.0),
                )
            except ImportError:
                raise RuntimeError("openai 包未安装，请执行: pip install openai")
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
                    timeout=httpx.Timeout(120.0, connect=15.0),
                )
            except ImportError:
                raise RuntimeError("openai 包未安装，请执行: pip install openai")
        return self._async_client

    @property
    def is_available(self) -> bool:
        """判断LLM是否可用（有API Key）"""
        return bool(self.api_key)

    # ----------------------------------------------------------
    # 辅助方法：构造消息
    # ----------------------------------------------------------

    @staticmethod
    def build_text_message(text: str) -> dict:
        """构造纯文本 user message"""
        return {"role": "user", "content": text}

    @staticmethod
    def build_image_message(images: list[bytes | str], text: str = "") -> dict:
        """
        构造含图片的 user message，支持多图。

        Args:
            images: 列表元素为 bytes（自动 base64 编码）或 str（当作 URL 直传）
            text: 附加的文字提示

        Returns:
            OpenAI 格式的 user message dict

        Example:
            msg = LLMClient.build_image_message(
                [img1_bytes, img2_bytes],
                text="对比这两张图的模块差异"
            )
        """
        content_parts = []
        for img in images:
            if isinstance(img, bytes):
                b64 = base64.b64encode(img).decode("utf-8")
                # 默认 png，可后续扩展根据 magic bytes 判断
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                })
            elif isinstance(img, str):
                # 字符串直接当 URL
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": img},
                })
            else:
                logger.warning(f"build_image_message: 跳过不支持的图片类型 {type(img)}")

        if text:
            content_parts.append({"type": "text", "text": text})

        return {"role": "user", "content": content_parts}

    @staticmethod
    def build_image_url(image_data: bytes, ext: str = "png") -> str:
        """bytes → data:image/xxx;base64,... URL"""
        b64 = base64.b64encode(image_data).decode("utf-8")
        media_type = f"image/{ext}" if not ext.startswith(".") else f"image/{ext.lstrip('.')}"
        return f"data:{media_type};base64,{b64}"

    # ----------------------------------------------------------
    # 底层统一调用（含 429 重试）
    # ----------------------------------------------------------

    def _do_call(
        self,
        client,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
        response_format: str,
    ) -> str | dict:
        """同步调用 + 429 指数退避重试"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return self._single_call(client, messages, model, temperature,
                                         max_tokens, response_format)
            except Exception as e:
                err_str = str(e)
                # 429 / 529 限流 → 重试
                if ("429" in err_str or "529" in err_str or
                    "rate" in err_str.lower() or "limit" in err_str.lower()):
                    if attempt < max_retries - 1:
                        wait = 2 ** attempt  # 1s, 2s, 4s
                        logger.warning(f"[LLM] 限流重试 {attempt+1}/{max_retries}，等待 {wait}s: {err_str[:100]}")
                        time.sleep(wait)
                        continue
                # response_format 不支持 → 去掉重试
                if "response_format" in err_str:
                    logger.warning(f"[LLM] 供应商不支持 response_format，去除后重试: {err_str[:100]}")
                    return self._single_call(client, messages, model, temperature,
                                             max_tokens, "text")
                raise

    async def _do_async_call(
        self,
        client,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
        response_format: str,
    ) -> str | dict:
        """异步调用 + 429 指数退避重试"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return await self._single_async_call(client, messages, model, temperature,
                                                     max_tokens, response_format)
            except Exception as e:
                err_str = str(e)
                if ("429" in err_str or "529" in err_str or
                    "rate" in err_str.lower() or "limit" in err_str.lower()):
                    if attempt < max_retries - 1:
                        wait = 2 ** attempt
                        logger.warning(f"[LLM] 限流重试 {attempt+1}/{max_retries}，等待 {wait}s: {err_str[:100]}")
                        await asyncio.sleep(wait)
                        continue
                if "response_format" in err_str:
                    logger.warning(f"[LLM] 供应商不支持 response_format，去除后重试: {err_str[:100]}")
                    return await self._single_async_call(client, messages, model, temperature,
                                                         max_tokens, "text")
                raise

    def _single_call(self, client, messages, model, temperature, max_tokens, response_format):
        """单次同步调用"""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        logger.debug(f"LLM 请求: model={model}, msgs={len(messages)}")
        response = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        logger.debug(f"LLM 响应: {content[:200]}...")
        return self._parse_response(content, response_format)

    async def _single_async_call(self, client, messages, model, temperature, max_tokens, response_format):
        """单次异步调用"""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        logger.debug(f"LLM 异步请求: model={model}, msgs={len(messages)}")
        response = await client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        logger.debug(f"LLM 响应: {content[:200]}...")
        return self._parse_response(content, response_format)

    # ----------------------------------------------------------
    # 基础接口：纯文本
    # ----------------------------------------------------------

    def call_text(
        self,
        messages: list[dict],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: str = "text",
    ) -> str | dict:
        """
        纯文本调用，使用文本模型（self.model）。

        Args:
            messages: OpenAI 格式消息列表
            model: 覆盖默认文本模型
            temperature: 覆盖默认温度
            max_tokens: 覆盖默认最大 token
            response_format: "text" 返回 str, "json" 返回 dict
        """
        client = self._get_client()
        return self._do_call(
            client, messages,
            model=model or self.model,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens or self.max_tokens,
            response_format=response_format,
        )

    async def acall_text(
        self,
        messages: list[dict],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: str = "text",
    ) -> str | dict:
        """纯文本调用（异步）"""
        client = self._get_async_client()
        return await self._do_async_call(
            client, messages,
            model=model or self.model,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens or self.max_tokens,
            response_format=response_format,
        )

    # ----------------------------------------------------------
    # 快速模型接口（简单提取/格式转换场景）
    # ----------------------------------------------------------

    def call_text_fast(
        self,
        messages: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: str = "text",
    ) -> str | dict:
        """快速文本调用，使用快速模型（self.fast_model）。适用于简单提取/格式转换场景。"""
        return self.call_text(
            messages,
            model=self.fast_model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    async def acall_text_fast(
        self,
        messages: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: str = "text",
    ) -> str | dict:
        """快速文本调用（异步），使用快速模型（self.fast_model）。适用于简单提取/格式转换场景。"""
        return await self.acall_text(
            messages,
            model=self.fast_model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    # ----------------------------------------------------------
    # 基础接口：多模态（视觉）
    # ----------------------------------------------------------

    def call_vision(
        self,
        messages: list[dict],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: str = "text",
    ) -> str | dict:
        """
        多模态调用，使用视觉模型（self.vl_model）。
        messages 中可包含多张图片。

        Args:
            messages: OpenAI 格式消息列表（可用 build_image_message 构造图片消息）
            model: 覆盖默认视觉模型
            temperature: 覆盖默认温度
            max_tokens: 覆盖默认最大 token
            response_format: "text" 返回 str, "json" 返回 dict
        """
        client = self._get_client()
        return self._do_call(
            client, messages,
            model=model or self.vl_model,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens or self.max_tokens,
            response_format=response_format,
        )

    async def acall_vision(
        self,
        messages: list[dict],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: str = "text",
    ) -> str | dict:
        """多模态调用（异步）"""
        client = self._get_async_client()
        return await self._do_async_call(
            client, messages,
            model=model or self.vl_model,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens or self.max_tokens,
            response_format=response_format,
        )

    # ----------------------------------------------------------
    # 旧接口兼容（内部调用新方法）
    # ----------------------------------------------------------

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: str = "json",
    ) -> dict | str:
        """同步调用 LLM（旧接口，向后兼容）"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self.call_text(messages, response_format=response_format)

    async def achat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: str = "json",
    ) -> dict | str:
        """异步调用 LLM（旧接口，向后兼容）"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self.acall_text(messages, response_format=response_format)

    def chat_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_data: bytes,
        image_ext: str = "png",
        response_format: str = "json",
    ) -> dict | str:
        """
        调用支持视觉的LLM（旧接口，向后兼容，单张图片）。
        """
        messages = [
            {"role": "system", "content": system_prompt},
            LLMClient.build_image_message([image_data], text=user_prompt),
        ]
        logger.info(f"LLM Vision 请求: model={self.vl_model}, image_size={len(image_data)}bytes")
        return self.call_vision(messages, response_format=response_format)

    # ----------------------------------------------------------
    # 响应解析
    # ----------------------------------------------------------

    def _parse_response(self, content: str, response_format: str) -> dict | str:
        """解析 LLM 响应，含多重容错修复"""
        if response_format == "json":
            # 预处理: 去掉 Qwen/thinking 模型的 <think/> 标签
            content = re.sub(r"<think.*?>.*?</think\s*>", "", content, flags=re.DOTALL).strip()
            content = re.sub(r"</?think\s*>", "", content).strip()

            # 1. 直接解析
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                pass

            # 2. 提取 markdown 代码块包裹的 JSON
            code_block = re.search(r"```(?:json)?\s*\n?(\{.*?\n?\})\s*\n?```", content, re.DOTALL)
            if code_block:
                try:
                    return json.loads(code_block.group(1))
                except json.JSONDecodeError:
                    pass

            # 3. 裸 JSON（贪婪匹配最外层大括号）
            first_brace = content.find("{")
            last_brace = content.rfind("}")
            if first_brace != -1 and last_brace > first_brace:
                json_str = content[first_brace:last_brace + 1]
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    # 3b. 尝试修复常见问题后重试
                    fixed = self._try_fix_json(json_str)
                    if fixed:
                        return fixed

            logger.warning(f"LLM返回内容无法解析为JSON，返回原始文本: {content[:200]}")
            return {"_raw_text": content, "_parse_error": True}

        return content

    def _try_fix_json(self, json_str: str) -> dict | None:
        """尝试修复常见的 JSON 格式问题"""
        import re as _re

        fixed = json_str.strip()

        # 修复0: 双层花括号 { { → {（LLM常见：把JSON包在外层{}里）
        if fixed.startswith("{ {") or fixed.startswith("{{"):
            # 尝试去掉最外层花括号
            inner = fixed[1:].strip()
            if inner.startswith("{"):
                fixed = inner
                logger.debug("JSON修复: 去掉多余外层花括号")

        # 修复1: 去掉行内注释 (// ... 和 /* ... */)
        fixed = _re.sub(r"//.*?$", "", fixed, flags=_re.MULTILINE)
        fixed = _re.sub(r"/\*.*?\*/", "", fixed, flags=_re.DOTALL)

        # 修复2: 去掉尾逗号（, } 和 , ] 中的逗号）
        fixed = _re.sub(r",\s*([}\]])", r"\1", fixed)

        # 修复3: 单引号→双引号（简单场景）
        # 只在确定没有双引号混用时修复
        if '"' not in fixed and "'" in fixed:
            fixed = fixed.replace("'", '"')

        # 修复4: 去掉多余尾部内容（截断的JSON，尝试补全）
        # 统计未闭合的括号
        open_braces = fixed.count("{") - fixed.count("}")
        open_brackets = fixed.count("[") - fixed.count("]")
        if open_braces > 0 or open_brackets > 0:
            fixed = fixed + "]" * open_brackets + "}" * open_braces

        try:
            result = json.loads(fixed)
            logger.info(f"JSON修复成功: 尾逗号/注释/截断修复")
            return result
        except json.JSONDecodeError:
            return None


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
