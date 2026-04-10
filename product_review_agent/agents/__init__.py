# -*- coding: utf-8 -*-
"""
多Agent评估模块

当前可用:
  - LLMClient / get_llm_client: 统一LLM调用层

历史架构（已迁移到 reviewer.py）:
  OrchestratorAgent -> PersonaAgent / ScenarioAgent / MarketAgent
"""

from .llm_client import LLMClient, get_llm_client

__all__ = [
    "LLMClient",
    "get_llm_client",
]
