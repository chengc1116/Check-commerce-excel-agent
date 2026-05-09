# -*- coding: utf-8 -*-
"""
产品立项审核 — 公共评分模块

本文件仅保留被 pipeline.py 复用的核心函数：
  1. _build_audience_prompt / _build_scenario_prompt / _build_competitive_prompt — Prompt构建
  2. _fallback_score — 规则引擎回退评分
  3. ascore_with_llm — 异步LLM评分（核心）
  4. analyze_with_history — 同类产品分析

主流程入口请使用: from product_review_agent.pipeline import run_pipeline
命令行入口请使用: scripts/review_from_template.py（已改为调用 pipeline）
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from product_review_agent.agents.llm_client import get_llm_client, LLMClient

logger = logging.getLogger(__name__)


# ============================================================
# Prompt 构建
# ============================================================

def _build_audience_prompt(text: str) -> str:
    return (
        '【任务】对以下"人群分析"数据进行评分。\n'
        '【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。\n'
        '【评分维度】每项25分，总分100分：\n'
        '1. 目标人群明确性：核心人群是否有清晰定义（年龄、性别、画像特征）\n'
        '2. 数据支撑度：是否有量化数据（占比、百分比、市场规模等）\n'
        '3. 痛点分析深度：是否明确了人群痛点和需求，痛点描述是否具体\n'
        '4. 细分合理性：人群细分是否合理、有层次、有主次区分\n\n'
        f'【人群数据】\n{text}\n\n'
        '【输出格式】严格按此JSON结构输出（score必须是0-25的整数，reason是简短说明，strengths/weaknesses/suggestions各2-3条）：\n'
        '{\n'
        '  "total_score": 75,\n'
        '  "dimensions": {\n'
        '    "目标人群明确性": {"score": 20, "reason": "核心人群定义清晰"},\n'
        '    "数据支撑度": {"score": 18, "reason": "有市场规模数据"},\n'
        '    "痛点分析深度": {"score": 19, "reason": "痛点描述具体"},\n'
        '    "细分合理性": {"score": 18, "reason": "细分有层次"}\n'
        '  },\n'
        '  "strengths": ["优势1", "优势2"],\n'
        '  "weaknesses": ["不足1", "不足2"],\n'
        '  "suggestions": ["建议1", "建议2"]\n'
        '}'
    )


def _build_scenario_prompt(text: str) -> str:
    return (
        '【任务】对以下"场景分析"数据进行评分。\n'
        '【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。\n'
        '【评分维度】每项25分，总分100分：\n'
        '1. 核心场景清晰度：主场景是否明确，是否有优先级排序\n'
        '2. 场景覆盖完整性：是否覆盖了用户主要使用场景\n'
        '3. 问题需求分析：每个场景是否有清晰的问题描述和需求提取\n'
        '4. 场景价值评估：场景是否有商业价值判断（规模、频次、付费意愿）\n\n'
        f'【场景数据】\n{text}\n\n'
        '【输出格式】严格按此JSON结构输出（score必须是0-25的整数，reason是简短说明，strengths/weaknesses/suggestions各2-3条）：\n'
        '{\n'
        '  "total_score": 70,\n'
        '  "dimensions": {\n'
        '    "核心场景清晰度": {"score": 18, "reason": "主场景明确"},\n'
        '    "场景覆盖完整性": {"score": 17, "reason": "覆盖主要场景"},\n'
        '    "问题需求分析": {"score": 18, "reason": "问题描述清晰"},\n'
        '    "场景价值评估": {"score": 17, "reason": "有商业价值判断"}\n'
        '  },\n'
        '  "strengths": ["优势1", "优势2"],\n'
        '  "weaknesses": ["不足1", "不足2"],\n'
        '  "suggestions": ["建议1", "建议2"]\n'
        '}'
    )


def _build_audience_scenario_prompt(
    audience_text: str,
    scenario_text: str,
    product_context: dict,
) -> str:
    """构建合并版人群+场景分析报告 prompt"""
    product_name = product_context.get("product_name", "未知")
    brand = product_context.get("brand", "未知")
    category_l1 = product_context.get("category_l1", "")
    category_l2 = product_context.get("category_l2", "")
    category_l3 = product_context.get("category_l3", "")
    pricing = product_context.get("pricing", "未填写")
    estimated_sales = product_context.get("estimated_sales", "未填写")

    return (
        '【任务】对以下产品立项的"人群与场景分析"数据撰写审核报告。'
        '你不仅是评审者，更是顾问——如果项目方的人群与场景分析存在偏差或遗漏，'
        '你需要基于产品品类和常识给出你认为正确的人群与场景分析，作为项目方的参考标杆。\n'
        '【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。\n\n'
        f'【产品信息】\n'
        f'产品名称：{product_name}\n'
        f'品牌：{brand}\n'
        f'品类：{category_l1} > {category_l2} > {category_l3}\n'
        f'定价：{pricing}\n'
        f'目标销量：{estimated_sales}\n\n'
        f'【项目方提交的人群数据】\n{audience_text}\n\n'
        f'【项目方提交的场景数据】\n{scenario_text}\n\n'
        '【分析要求】\n'
        '1. 不要只给分数和一句话理由，要写出具体的分析过程和发现\n'
        '2. 要引用项目方数据中的具体内容，而非笼统概括\n'
        '3. 人群和场景要交叉分析：哪些人群对应哪些场景、是否存在断层\n'
        '4. 如果项目方的人群或场景有明显偏差、遗漏、不合理，你需要给出自己的分析'
        '——这类产品真正应该关注哪些人群、哪些核心场景\n'
        '5. 评分是基于分析结论的自然结果，不是先给分再凑理由\n\n'
        '【输出格式】严格按此JSON结构输出：\n'
        '{\n'
        '  "analysis": {\n'
        '    "audience_scene_fit": "人群-场景匹配度分析（150-250字）：项目方定义的核心人群是谁，主要场景是什么，'
        '人群特征是否自然导向场景需求，是否存在人群与场景不匹配或缺失关键场景的情况，引用数据具体说明",\n'
        '    "insight_depth": "需求洞察深度分析（150-250字）：从人群痛点推导到场景需求的逻辑链是否完整，'
        '痛点是否具体可操作（对比"需要好产品"式空话），哪些洞察有说服力、哪些流于表面",\n'
        '    "data_coverage": "数据支撑与覆盖度分析（150-250字）：人群是否有量化画像，场景是否有优先级排序，'
        '是否存在纯定性无数据支撑的部分，覆盖面是否有明显遗漏",\n'
        '    "commercial_value": "商业价值判断分析（150-250字）：场景是否有频次/规模/付费意愿评估，'
        '人群细分是否有主次和商业优先级，人-场景组合指向什么样的市场机会"\n'
        '  },\n'
        '  "expert_analysis": {\n'
        '    "target_audience": "基于该品类和产品特征，你认为正确的目标人群分析（100-200字）：核心人群是谁'
        '（年龄/性别/职业/消费特征），次要人群是谁，与项目方定义的差异在哪里",\n'
        '    "core_scenarios": "基于该品类和产品特征，你认为正确的核心使用场景（100-200字）：'
        'TOP3场景及优先级理由，每个场景下的核心需求是什么，与项目方定义的差异在哪里",\n'
        '    "key_suggestion": "人群与场景方面最关键的一条建议（50-100字）：如果项目方只能改一件事，应该改什么"\n'
        '  },\n'
        '  "scores": {\n'
        '    "人群-场景匹配度": {"score": 20, "reason": "一句话结论"},\n'
        '    "需求洞察深度": {"score": 18, "reason": "一句话结论"},\n'
        '    "数据支撑与覆盖度": {"score": 19, "reason": "一句话结论"},\n'
        '    "商业价值判断": {"score": 18, "reason": "一句话结论"}\n'
        '  },\n'
        '  "total_score": 75,\n'
        '  "strengths": ["具体优势1（附数据引用）", "具体优势2"],\n'
        '  "weaknesses": ["具体不足1（附数据引用）", "具体不足2"],\n'
        '  "suggestions": ["具体建议1（可操作）", "具体建议2", "具体建议3"]\n'
        '}'
    )


def _build_competitive_prompt(project_data: dict) -> str:
    """构建九宫格目标评分prompt"""
    competitor_name = project_data.get("competitor_name", "未知")
    competitor_copy = project_data.get("competitor_strengths_copy", "未填写")
    competitor_advantage = project_data.get("competitor_advantage", "未填写")
    price_margin = project_data.get("price_margin", "未填写")
    market_size = project_data.get("market_size", "未填写")
    competitor_sales = project_data.get("competitor_sales", "未填写")
    competitor_sku = project_data.get("competitor_sku", "未填写")

    return (
        '【任务】对以下"九宫格目标"板块的填写质量进行评分，重点考察完整度和严谨性。\n'
        '【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。\n'
        '【评分维度】每项25分，总分100分：\n'
        '1. 信息完整度：各字段是否都已填写（竞争对手、竞品卖点、差异化策略、定价/毛利、市场规模等），'
        '缺失字段是否影响评审判断\n'
        '2. 数据严谨性：填写的竞品销售额、市场规模、价格等数据是否有量化支撑，'
        '还是泛泛而谈（如"很大""不错"），数据是否可信\n'
        '3. 逻辑自洽性：竞品卖点与差异化策略之间是否存在矛盾（如声称的"超越点"实际是竞品已有卖点），'
        '定价策略与毛利目标是否匹配\n'
        '4. 分析深度：是否只简单列了对手名称而没有深入分析竞品，'
        '差异化策略是否有具体方案而非口号式描述\n\n'
        f'【项目方填写的数据】\n'
        f'竞争对手: {competitor_name}\n'
        f'竞品SKU: {competitor_sku}\n'
        f'竞品销售额: {competitor_sales}\n'
        f'竞品卖点(复制): {competitor_copy}\n'
        f'竞品卖点(超越/差异化): {competitor_advantage}\n'
        f'定价/毛利: {price_margin}\n'
        f'市场规模: {market_size}\n\n'
        '请仅基于以上填写内容，评估完整度和严谨性。不要凭外部知识判断数据真伪，'
        '而是看填写者是否做到了认真、细致、有据可循。\n\n'
        '【输出格式】严格按此JSON结构输出（score必须是0-25的整数，reason是简短说明，strengths/weaknesses/suggestions各2-3条）：\n'
        '{\n'
        '  "total_score": 65,\n'
        '  "dimensions": {\n'
        '    "信息完整度": {"score": 18, "reason": "大部分字段已填写"},\n'
        '    "数据严谨性": {"score": 15, "reason": "部分数据缺少量化支撑"},\n'
        '    "逻辑自洽性": {"score": 16, "reason": "策略基本自洽"},\n'
        '    "分析深度": {"score": 16, "reason": "分析较为深入"}\n'
        '  },\n'
        '  "strengths": ["优势1", "优势2"],\n'
        '  "weaknesses": ["不足1", "不足2"],\n'
        '  "suggestions": ["建议1", "建议2"]\n'
        '}'
    )


# ============================================================
# 规则引擎回退
# ============================================================

def _fallback_score(label: str, reason: str = "unknown") -> dict:
    """
    规则引擎评分回退

    Args:
        label: 评分维度 (audience/scenario/competitive)
        reason: 回退原因，用于区分不同的失败场景
    """
    dim_map = {
        "audience": ["目标人群明确性", "数据支撑度", "痛点分析深度", "细分合理性"],
        "scenario": ["核心场景清晰度", "场景覆盖完整性", "问题需求分析", "场景价值评估"],
        "audience_scenario": ["人群-场景匹配度", "需求洞察深度", "数据支撑与覆盖度", "商业价值判断"],
        "competitive": ["信息完整度", "数据严谨性", "逻辑自洽性", "分析深度"],
    }
    dims = dim_map.get(label, dim_map["audience"])

    # 根据不同原因生成不同的提示信息
    _reason_templates = {
        "no_llm": {
            "score_reason": "LLM未配置,规则回退",
            "strength": "LLM未配置，无法进行AI评估",
            "weakness": "请在 .env 文件中设置 LLM_API_KEY",
            "suggestion": "配置 LLM API Key 后可获得更精准的智能评分",
        },
        "api_error": {
            "score_reason": "LLM接口调用失败,规则回退",
            "strength": "LLM调用出错，无法进行AI评估",
            "weakness": "LLM API 接口异常，请检查网络连接和 API Key 余额",
            "suggestion": "确认 API Key 有效且余额充足，检查网络是否正常",
        },
        "timeout": {
            "score_reason": "LLM响应超时,规则回退",
            "strength": "LLM响应超时，无法完成评估",
            "weakness": "当前模型响应较慢或网络不稳定",
            "suggestion": "可尝试更换更快的模型或检查网络环境",
        },
        "parse_error": {
            "score_reason": "LLM返回格式异常,规则回退",
            "strength": "LLM返回了无法解析的内容，无法完成评估",
            "weakness": "模型可能返回了非JSON格式的内容",
            "suggestion": "可尝试更换模型或调整 prompt 后重试",
        },
        "no_data": {
            "score_reason": "数据为空或含图片,跳过评分",
            "strength": "该维度缺少可评估的文本数据",
            "weakness": "Excel中该字段为空或仅包含图片",
            "suggestion": "请在 Excel 中填写对应的文本内容后重新上传",
        },
    }

    tmpl = _reason_templates.get(reason, _reason_templates["no_llm"])

    scores = {d: {"score": 10, "reason": tmpl["score_reason"]} for d in dims}
    return {
        "total_score": sum(s["score"] for s in scores.values()),
        "dimensions": scores,
        "strengths": [tmpl["strength"]],
        "weaknesses": [tmpl["weakness"]],
        "suggestions": [tmpl["suggestion"]],
    }


# ============================================================
# 异步评分
# ============================================================

async def ascore_with_llm(
    llm: LLMClient, text: str, field_type: str, extra_context: str = ""
) -> dict:
    """异步LLM评分，根据不同失败原因返回不同的回退信息"""
    # 场景1: LLM 未配置
    if not llm.is_available:
        logger.warning(f"[{field_type}] LLM不可用: api_key未设置")
        return _fallback_score(field_type, reason="no_llm")

    if field_type == "audience":
        prompt = _build_audience_prompt(text)
    elif field_type == "scenario":
        prompt = _build_scenario_prompt(text)
    elif field_type == "audience_scenario":
        prompt = text  # 已由调用方构建好的完整prompt
    elif field_type == "competitive":
        prompt = extra_context
    else:
        return _fallback_score(field_type, reason="no_llm")

    original_max_tokens = llm.max_tokens
    llm.max_tokens = 4096

    try:
        result = await llm.achat(
            system_prompt="你是一个严谨的电商产品审核评分专家。严格只返回JSON对象，禁止输出思考过程、解释文字或markdown代码块。",
            user_prompt=prompt,
            response_format="json",
        )

        # 场景2: 返回结果格式异常（非dict、解析错误、缺少total_score）
        if not isinstance(result, dict):
            logger.warning(f"[{field_type}] LLM返回非dict类型: type={type(result).__name__}, content={str(result)[:200]}")
            return _fallback_score(field_type, reason="parse_error")

        if result.get("_parse_error"):
            raw = result.get("_raw_text", "")
            logger.warning(f"[{field_type}] LLM返回JSON解析失败: {raw[:200]}")
            return _fallback_score(field_type, reason="parse_error")

        if "total_score" not in result:
            logger.warning(f"[{field_type}] LLM返回缺少total_score字段: keys={list(result.keys())}")
            return _fallback_score(field_type, reason="parse_error")

        return result

    # 场景3: 超时
    except TimeoutError as e:
        logger.error(f"[{field_type}] LLM调用超时: {e}")
        return _fallback_score(field_type, reason="timeout")

    # 场景4: 网络/API错误
    except ConnectionError as e:
        logger.error(f"[{field_type}] LLM网络连接失败: {e}")
        return _fallback_score(field_type, reason="api_error")
    except Exception as e:
        err_str = str(e)
        # 根据 API 返回的错误信息细分
        err_lower = err_str.lower()
        if "timeout" in err_lower or "timed out" in err_lower:
            logger.error(f"[{field_type}] LLM调用超时: {err_str[:200]}")
            return _fallback_score(field_type, reason="timeout")
        elif "auth" in err_lower or "api_key" in err_lower or "unauthorized" in err_lower or "invalid" in err_lower:
            logger.error(f"[{field_type}] LLM认证失败(API Key无效): {err_str[:200]}")
            return _fallback_score(field_type, reason="api_error")
        elif "rate" in err_lower or "quota" in err_lower or "limit" in err_lower or "429" in err_lower:
            logger.error(f"[{field_type}] LLM调用频率限制/余额不足: {err_str[:200]}")
            return _fallback_score(field_type, reason="api_error")
        elif "connection" in err_lower or "network" in err_lower:
            logger.error(f"[{field_type}] LLM网络错误: {err_str[:200]}")
            return _fallback_score(field_type, reason="api_error")
        else:
            logger.error(f"[{field_type}] LLM调用异常: {err_str[:200]}", exc_info=True)
            return _fallback_score(field_type, reason="api_error")

    finally:
        llm.max_tokens = original_max_tokens


# ============================================================
# 同类产品分析（产品库检索 + LLM 数据分析）
# ============================================================

async def analyze_with_history(project_data: dict) -> dict:
    """
    查产品库 + 拉销量 + LLM分析。
    任何异常都降级返回空结果，不阻塞审核主流程。
    """
    try:
        from product_review_agent.product_db.database import ProductDB
        from product_review_agent.product_db.conflict_analyzer import analyze_with_sales_data

        db = ProductDB()
        # 兼容两种键名: category_l2 (旧) / categoryl2 (ExcelParsingAgent新格式)
        category_l2 = project_data.get("category_l2") or project_data.get("categoryl2")
        if not category_l2:
            return {"products": [], "analysis": None}

        products = db.get_products_by_category2(category_l2)
        if not products:
            return {"products": [], "analysis": None}

        logger.info(f"[同类产品分析] 检索到 {len(products)} 个品类「{category_l2}」产品")

        # LLM 基于真实数据做分析
        llm = get_llm_client()
        if not llm.is_available:
            return {"products": products, "analysis": None, "error": "LLM不可用"}

        analysis = await analyze_with_sales_data(llm, project_data, products)
        db.close()
        return {"products": products, "analysis": analysis}

    except Exception as e:
        logger.error(f"[同类产品分析] 异常: {e}")
        return {"products": [], "analysis": None, "error": str(e)}
