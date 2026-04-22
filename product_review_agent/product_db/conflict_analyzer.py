# -*- coding: utf-8 -*-
"""
LLM 冲突分析 — 基于真实产品数据和销量信息

输入: 新项目信息 + 数据库查出的同类产品（含近2月销量）
输出: LLM 基于数据表格给出的分析建议
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from product_review_agent.agents.llm_client import LLMClient

logger = logging.getLogger(__name__)


def _build_product_table(products: list[dict]) -> str:
    """将产品列表格式化为文本表格"""
    if not products:
        return "（无历史产品数据）"

    lines = ["货号 | 品牌 | 版本 | 三级品类 | 近期销量 | 趋势"]
    lines.append("---|------|------|----------|----------|----")

    for p in products:
        sku = p.get("sku", "-")
        brand = p.get("brand", "-")
        version = p.get("version", "-")
        cat3 = p.get("category_l3", "-")
        sales = p.get("recent_sales", [])

        if len(sales) >= 2:
            m1_val = sales[1].get("sales_volume", 0)  # 较早的月份
            m2_val = sales[0].get("sales_volume", 0)  # 较近的月份
            m1_label = sales[1].get("month", "")
            m2_label = sales[0].get("month", "")
            trend = "↑" if m2_val > m1_val else ("↓" if m2_val < m1_val else "—")
            sales_str = f"{m1_label}:{m1_val} → {m2_label}:{m2_val} {trend}"
        elif len(sales) == 1:
            m_val = sales[0].get("sales_volume", 0)
            m_label = sales[0].get("month", "")
            sales_str = f"{m_label}:{m_val}"
        else:
            sales_str = "暂无数据"

        lines.append(f"{sku} | {brand} | {version} | {cat3} | {sales_str}")

    return "\n".join(lines)


def _build_analysis_prompt(
    new_product: dict, products: list[dict]
) -> str:
    """构建 LLM 分析 prompt"""
    product_table = _build_product_table(products)

    return f'''你是一个电商产品线管理专家。请根据以下数据，分析新产品立项的可行性。

== 新项目信息 ==
产品名称: {new_product.get('product_name', '未知')}
品牌: {new_product.get('brand', '未知')}
品类: {new_product.get('category_l1', '')} > {new_product.get('category_l2', '')} > {new_product.get('category_l3', '')}
目标人群: {new_product.get('target_audience', '未填写')[:500]}
使用场景: {new_product.get('usage_scenarios', '未填写')[:500]}
竞争对手: {new_product.get('competitor_name', '未填写')}
定价/毛利: {new_product.get('price_margin', '未填写')}

== 同品类现有产品及销量 ==
{product_table}

== 分析要求 ==
请基于以上真实数据，给出专业分析：

1. **品牌内竞争**: 同品牌下是否有直接同类产品？新产品与它们的差异化是否足够？
2. **市场趋势**: 现有产品销量趋势如何（上升/下降/持平）？品类整体是在增长还是萎缩？
3. **风险评估**: 新产品可能面临哪些风险（品牌内竞争、市场饱和、同质化等）？
4. **具体建议**: 给出2-3条可操作的建议。

注意：
- 分析必须严格基于表格中的真实数据，不要编造数据
- 如果品类无现有产品，可以说"品类空白，有一定机会"
- 如果销量数据较少或产品较少，请如实说明数据局限性
- 语气客观专业，不要过度乐观或悲观
- 建议要具体可操作，不要泛泛而谈

返回JSON格式：
{{
    "has_conflict": true/false,
    "conflict_count": 0,
    "analysis": "200-400字的分析说明",
    "suggestions": ["建议1", "建议2", "建议3"]
}}'''


async def analyze_with_sales_data(
    llm: LLMClient,
    new_product: dict,
    existing_products: list[dict],
) -> dict:
    """
    基于真实产品数据和销量信息，用 LLM 给出分析建议。

    Args:
        llm: LLM 客户端
        new_product: 新项目的解析数据（从 parse_result.data 取）
        existing_products: 数据库查出的产品列表（含 recent_sales 字段）

    Returns:
        {
            "has_conflict": true/false,
            "conflict_count": N,
            "analysis": "分析说明",
            "suggestions": ["建议1", ...],
            "error": None or str
        }
    """
    prompt = _build_analysis_prompt(new_product, existing_products)

    original_max_tokens = llm.max_tokens
    llm.max_tokens = 4096

    try:
        result = await llm.achat(
            system_prompt="你是一个严谨的电商产品线管理专家，请只返回JSON，不要任何其他文字。",
            user_prompt=prompt,
            response_format="json",
        )

        if not isinstance(result, dict):
            logger.warning(f"[冲突分析] LLM返回非dict: {type(result).__name__}")
            return _fallback_result("LLM返回格式异常")

        if result.get("_parse_error"):
            logger.warning("[冲突分析] JSON解析失败")
            return _fallback_result("JSON解析失败")

        # 确保返回完整结构
        return {
            "has_conflict": result.get("has_conflict", False),
            "conflict_count": result.get("conflict_count", 0),
            "analysis": result.get("analysis", ""),
            "suggestions": result.get("suggestions", []),
            "error": None,
        }

    except Exception as e:
        logger.error(f"[冲突分析] LLM调用异常: {e}")
        return _fallback_result(str(e))

    finally:
        llm.max_tokens = original_max_tokens


def _fallback_result(error_msg: str) -> dict:
    """降级返回"""
    return {
        "has_conflict": False,
        "conflict_count": 0,
        "analysis": f"AI分析暂不可用（{error_msg}），请参考上方产品数据表格人工判断。",
        "suggestions": [],
        "error": error_msg,
    }
