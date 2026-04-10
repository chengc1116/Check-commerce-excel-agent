# -*- coding: utf-8 -*-
"""
产品立项审核核心逻辑 - 供飞书Bot / 命令行 / API 共用

流程:
    1. 解析Excel模板 -> 结构化数据
    2. 异步并行: 人群评分 / 场景评分 / 九宫格评分 (三线并行)
    3. 生成文字审核报告
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from product_review_agent.parsers.template_parser import TemplateParser, TemplateParseResult
from product_review_agent.agents.llm_client import get_llm_client, LLMClient

logger = logging.getLogger(__name__)


# ============================================================
# Prompt 构建
# ============================================================

def _build_audience_prompt(text: str) -> str:
    return (
        '你是一个电商产品立项审核专家。请对以下"人群分析"数据进行评分，返回纯JSON（不要任何多余文字）。\n\n'
        '评分维度（每项25分，总分100分）：\n'
        '1. 目标人群明确性：核心人群是否有清晰定义（年龄、性别、画像特征）\n'
        '2. 数据支撑度：是否有量化数据（占比、百分比、市场规模等）\n'
        '3. 痛点分析深度：是否明确了人群痛点和需求，痛点描述是否具体\n'
        '4. 细分合理性：人群细分是否合理、有层次、有主次区分\n\n'
        f'人群数据：\n{text}\n\n'
        '返回JSON格式：\n'
        '{"total_score": 整数0-100, "dimensions": {"目标人群明确性": {"score": 0-25, "reason": "说明"}, '
        '"数据支撑度": {"score": 0-25, "reason": "说明"}, '
        '"痛点分析深度": {"score": 0-25, "reason": "说明"}, '
        '"细分合理性": {"score": 0-25, "reason": "说明"}}, '
        '"strengths": ["优势1", "优势2"], "weaknesses": ["不足1", "不足2"], '
        '"suggestions": ["建议1", "建议2"]}'
    )


def _build_scenario_prompt(text: str) -> str:
    return (
        '你是一个电商产品立项审核专家。请对以下"场景分析"数据进行评分，返回纯JSON（不要任何多余文字）。\n\n'
        '评分维度（每项25分，总分100分）：\n'
        '1. 核心场景清晰度：主场景是否明确，是否有优先级排序\n'
        '2. 场景覆盖完整性：是否覆盖了用户主要使用场景\n'
        '3. 问题需求分析：每个场景是否有清晰的问题描述和需求提取\n'
        '4. 场景价值评估：场景是否有商业价值判断（规模、频次、付费意愿）\n\n'
        f'场景数据：\n{text}\n\n'
        '返回JSON格式：\n'
        '{"total_score": 整数0-100, "dimensions": {"核心场景清晰度": {"score": 0-25, "reason": "说明"}, '
        '"场景覆盖完整性": {"score": 0-25, "reason": "说明"}, '
        '"问题需求分析": {"score": 0-25, "reason": "说明"}, '
        '"场景价值评估": {"score": 0-25, "reason": "说明"}}, '
        '"strengths": ["优势1", "优势2"], "weaknesses": ["不足1", "不足2"], '
        '"suggestions": ["建议1", "建议2"]}'
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
        '你是一个电商产品立项审核专家。请对以下"九宫格目标"板块的填写质量进行评估，'
        '重点考察填写的完整度和严谨性，返回纯JSON（不要任何多余文字）。\n\n'
        '评分维度（每项25分，总分100分）：\n'
        '1. 信息完整度：各字段是否都已填写（竞争对手、竞品卖点、差异化策略、定价/毛利、市场规模等），'
        '缺失字段是否影响评审判断\n'
        '2. 数据严谨性：填写的竞品销售额、市场规模、价格等数据是否有量化支撑，'
        '还是泛泛而谈（如"很大""不错"），数据是否可信\n'
        '3. 逻辑自洽性：竞品卖点与差异化策略之间是否存在矛盾（如声称的"超越点"实际是竞品已有卖点），'
        '定价策略与毛利目标是否匹配\n'
        '4. 分析深度：是否只简单列了对手名称而没有深入分析竞品，'
        '差异化策略是否有具体方案而非口号式描述\n\n'
        f'== 项目方填写的数据 ==\n'
        f'竞争对手: {competitor_name}\n'
        f'竞品SKU: {competitor_sku}\n'
        f'竞品销售额: {competitor_sales}\n'
        f'竞品卖点(复制): {competitor_copy}\n'
        f'竞品卖点(超越/差异化): {competitor_advantage}\n'
        f'定价/毛利: {price_margin}\n'
        f'市场规模: {market_size}\n\n'
        '请仅基于以上填写内容，评估完整度和严谨性。不要凭外部知识判断数据真伪，'
        '而是看填写者是否做到了认真、细致、有据可循。\n'
        '返回JSON格式：\n'
        '{"total_score": 整数0-100, "dimensions": {"信息完整度": {"score": 0-25, "reason": "说明"}, '
        '"数据严谨性": {"score": 0-25, "reason": "说明"}, '
        '"逻辑自洽性": {"score": 0-25, "reason": "说明"}, '
        '"分析深度": {"score": 0-25, "reason": "说明"}}, '
        '"strengths": ["优势1", "优势2"], "weaknesses": ["不足1", "不足2"], '
        '"suggestions": ["建议1", "建议2"]}'
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
    elif field_type == "competitive":
        prompt = extra_context
    else:
        return _fallback_score(field_type, reason="no_llm")

    original_max_tokens = llm.max_tokens
    llm.max_tokens = 4096

    try:
        result = await llm.achat(
            system_prompt="你是一个严谨的电商产品审核评分专家，请只返回JSON，不要任何其他文字。",
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


async def async_competitive_analysis(
    llm: LLMClient,
    competitor_name: str,
    category: str,
    project_data: dict,
) -> tuple[dict, dict]:
    """九宫格目标评分"""
    competitor_info = {
        "competitor_name": competitor_name,
        "category": category,
        "source": "project_data_only",
    }

    logger.info(f"    [九宫格评分] 评估填写完整度和严谨性...")
    competitive_prompt = _build_competitive_prompt(project_data)
    competitive_score = await ascore_with_llm(
        llm, "competitive", "competitive", extra_context=competitive_prompt,
    )
    logger.info(f"    [九宫格评分] {competitive_score.get('total_score', '?')}/100")

    return competitor_info, competitive_score


# ============================================================
# 审核结果数据类
# ============================================================

@dataclass
class ReviewResult:
    """审核结果"""
    file_name: str
    parse_result: TemplateParseResult
    scores: dict = field(default_factory=dict)
    report: str = ""
    overall_score: int = 0
    risk_level: str = "未知"
    elapsed_seconds: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "file_name": self.file_name,
            "overall_score": self.overall_score,
            "risk_level": self.risk_level,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "scores": self.scores,
            "error": self.error,
        }


# ============================================================
# 核心审核函数 - 供外部调用
# ============================================================

async def review_excel(file_path: str | Path) -> ReviewResult:
    """
    审核一个Excel文件，返回 ReviewResult。

    可供飞书Bot、命令行脚本、API等统一调用。

    Args:
        file_path: Excel文件路径

    Returns:
        ReviewResult 审核结果
    """
    file_path = Path(file_path)
    start_time = time.time()

    if not file_path.exists():
        return ReviewResult(
            file_name=file_path.name,
            parse_result=TemplateParseResult(file_path.name),
            error=f"文件不存在: {file_path}",
        )

    # Step 1: 解析模板
    logger.info(f"[审核] 开始解析: {file_path.name}")
    parser = TemplateParser()
    try:
        parse_result = parser.parse(file_path)
    except Exception as e:
        return ReviewResult(
            file_name=file_path.name,
            parse_result=TemplateParseResult(file_path.name),
            error=f"解析失败: {e}",
        )

    logger.info(f"[审核] 解析完成, 提取字段数: {len(parse_result.data)}")

    # Step 2: 异步并行评分
    logger.info("[审核] 开始异步并行评分...")
    llm = get_llm_client()

    tasks = {}
    task_labels = {}

    # 人群评分
    audience_text = parse_result.data.get("target_audience", "")
    if audience_text and "[图片" not in audience_text:
        tasks["audience"] = ascore_with_llm(llm, audience_text, "audience")
        task_labels["audience"] = "人群评分"
    else:
        tasks["audience"] = asyncio.coroutine(lambda: _fallback_score("audience", reason="no_data"))()
        task_labels["audience"] = "人群(跳过)"

    # 场景评分
    scenario_text = parse_result.data.get("usage_scenarios", "")
    if scenario_text and "[图片" not in scenario_text:
        tasks["scenario"] = ascore_with_llm(llm, scenario_text, "scenario")
        task_labels["scenario"] = "场景评分"
    else:
        tasks["scenario"] = asyncio.coroutine(lambda: _fallback_score("scenario", reason="no_data"))()
        task_labels["scenario"] = "场景(跳过)"

    # 九宫格评分
    competitor_name = parse_result.data.get("competitor_name", "")
    category = " > ".join(filter(None, [
        parse_result.data.get("category_l1", ""),
        parse_result.data.get("category_l2", ""),
    ])) or parse_result.data.get("category_l1", "")

    if competitor_name and competitor_name not in ["(未填写)", "/", "-"]:
        tasks["competitive"] = async_competitive_analysis(llm, competitor_name, category, parse_result.data)
        task_labels["competitive"] = f"九宫格 ({competitor_name})"
    else:
        tasks["competitive"] = asyncio.coroutine(lambda: (
            {"research_text": "未填写", "source": "skip"},
            _fallback_score("competitive", reason="no_data"),
        ))()
        task_labels["competitive"] = "九宫格(跳过)"

    # 并行执行
    logger.info(f"[审核] 并行启动: {', '.join(task_labels.values())}")
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    # 解析结果
    scores = {}
    task_keys = list(tasks.keys())
    for key, result_val in zip(task_keys, results):
        label = task_labels[key]
        if isinstance(result_val, Exception):
            logger.warning(f"  [{label}] 失败: {result_val}")
            scores[key] = _fallback_score(key)
        elif key == "competitive":
            if isinstance(result_val, tuple) and len(result_val) == 2:
                _, competitive_score = result_val
                scores["competitive"] = competitive_score
            else:
                scores["competitive"] = _fallback_score("competitive")
        else:
            scores[key] = result_val

    # Step 3: 生成报告
    report = generate_report(parse_result, scores)

    # 计算综合分
    audience_total = scores.get("audience", {}).get("total_score", 0)
    scenario_total = scores.get("scenario", {}).get("total_score", 0)
    competitive_total = scores.get("competitive", {}).get("total_score", 0)

    valid_scores = []
    if audience_total > 0:
        valid_scores.append(("人群", audience_total))
    if scenario_total > 0:
        valid_scores.append(("场景", scenario_total))
    if competitive_total > 0:
        valid_scores.append(("九宫格", competitive_total))

    overall = 0
    risk = "未知"
    if valid_scores:
        weights = {"人群": 0.3, "场景": 0.3, "九宫格": 0.4}
        total_weight = sum(weights.get(n, 0) for n, _ in valid_scores)
        if total_weight > 0:
            overall = int(round(sum(s * weights.get(n, 0) / total_weight for n, s in valid_scores)))
        else:
            overall = sum(s for _, s in valid_scores) // len(valid_scores)

        if overall >= 80:
            risk = "低"
        elif overall >= 60:
            risk = "中"
        else:
            risk = "高"

    elapsed = time.time() - start_time
    logger.info(f"[审核] 完成, 综合评分: {overall}/100, 风险: {risk}, 耗时: {elapsed:.1f}s")

    return ReviewResult(
        file_name=file_path.name,
        parse_result=parse_result,
        scores=scores,
        report=report,
        overall_score=overall,
        risk_level=risk,
        elapsed_seconds=elapsed,
    )


# ============================================================
# 文字报告生成
# ============================================================

SEPARATOR = "=" * 78
THIN_SEP = "-" * 78


def score_stars(score: int) -> int:
    if score >= 90: return 5
    elif score >= 75: return 4
    elif score >= 60: return 3
    elif score >= 40: return 2
    else: return 1


def stars_display(n: int) -> str:
    return "*" * n + "-" * (5 - n)


def _append_score_detail(lines: list, score: dict, total: int, section_name: str):
    """将评分明细追加到报告行列表中"""
    if score and total > 0:
        lines.append("[评分明细]")
        for dim_name, dim_info in score.get("dimensions", {}).items():
            s = dim_info.get("score", 0) if isinstance(dim_info, dict) else 0
            r = dim_info.get("reason", "") if isinstance(dim_info, dict) else str(dim_info)
            lines.append(f"  {dim_name}: {s}/25 - {r}")
        lines.append("")

        for label, key in [("优势", "strengths"), ("不足", "weaknesses"), ("改进建议", "suggestions")]:
            items = score.get(key, [])
            if items:
                prefix = "+" if label == "优势" else ("-" if label == "不足" else ">")
                lines.append(f"[{label}]")
                for item in items:
                    lines.append(f"  {prefix} {item}")
                lines.append("")
    else:
        lines.append(f"  ({section_name}评分不可用 - 请查看上方原因)")
        lines.append("")


def generate_report(result: TemplateParseResult, scores: dict) -> str:
    """生成文字版审核报告"""
    d = result.data
    lines = []

    # 标题
    lines.append(SEPARATOR)
    lines.append("                    产品立项审核报告")
    lines.append(SEPARATOR)
    lines.append("")

    # 产品概览
    product_name = d.get("product_name", "(未填写)")
    brand = d.get("brand", "(未填写)")
    cat = " > ".join(filter(None, [
        d.get("category_l1"), d.get("category_l2"), d.get("category_l3")
    ])) or "(未填写)"
    owner = d.get("owner", "(未填写)")
    competitor = d.get("competitor_name", "(未填写)")

    lines.append(f"产品名称: {product_name}")
    lines.append(f"品牌: {brand}")
    lines.append(f"品类: {cat}")
    lines.append(f"负责人: {owner}")
    lines.append(f"竞争对手: {competitor}")
    lines.append(f"来源文件: {result.file_name}")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # 立项信息
    lines.append(THIN_SEP)
    lines.append("一、立项信息")
    lines.append(THIN_SEP)
    for key in ["立项时间", "设计时间", "打样时间", "上架时间"]:
        lines.append(f"  {key}: {d.get(key, '(未填写)')}")
    lines.append("")

    # 市场信息
    lines.append(THIN_SEP)
    lines.append("二、市场信息")
    lines.append(THIN_SEP)
    lines.append(f"  市场规模: {d.get('market_size', '(未填写)')}")
    lines.append(f"  对手销售额: {d.get('competitor_sales', '(未填写)')}")
    lines.append(f"  对手SKU: {d.get('competitor_sku', '(未填写)')}")
    lines.append("")

    # 人群分析
    audience_text = d.get("target_audience", "")
    audience_score = scores.get("audience", {})
    audience_total = audience_score.get("total_score", 0)

    lines.append(THIN_SEP)
    lines.append(f"三、人群分析 [评分: {audience_total}/100] [{stars_display(score_stars(audience_total))}]")
    lines.append(THIN_SEP)

    if audience_text:
        lines.append("[原始数据]")
        for l in audience_text.split("\n"):
            lines.append(f"  {l}")
        lines.append("")
    _append_score_detail(lines, audience_score, audience_total, "人群")

    # 场景分析
    scenario_text = d.get("usage_scenarios", "")
    scenario_score = scores.get("scenario", {})
    scenario_total = scenario_score.get("total_score", 0)

    lines.append(THIN_SEP)
    lines.append(f"四、场景分析 [评分: {scenario_total}/100] [{stars_display(score_stars(scenario_total))}]")
    lines.append(THIN_SEP)

    if scenario_text:
        lines.append("[原始数据]")
        for l in scenario_text.split("\n"):
            lines.append(f"  {l}")
        lines.append("")
    _append_score_detail(lines, scenario_score, scenario_total, "场景")

    # 九宫格目标
    competitive_score = scores.get("competitive", {})
    competitive_total = competitive_score.get("total_score", 0)

    lines.append(THIN_SEP)
    lines.append(f"五、九宫格目标 [评分: {competitive_total}/100] [{stars_display(score_stars(competitive_total))}]")
    lines.append(THIN_SEP)
    lines.append(f"  价格/毛利: {d.get('price_margin', '(未填写)')}")
    lines.append(f"  竞争对手: {d.get('competitor_name', '(未填写)')}")
    lines.append(f"  对手SKU: {d.get('competitor_sku', '(未填写)')}")
    lines.append(f"  对手销售额: {d.get('competitor_sales', '(未填写)')}")
    lines.append(f"  对手卖点(复制): {d.get('competitor_strengths_copy', '(未填写)')}")
    lines.append(f"  对手卖点(超越): {d.get('competitor_advantage', '(未填写)')}")
    lines.append("")
    _append_score_detail(lines, competitive_score, competitive_total, "九宫格")

    # 设计要求
    lines.append(THIN_SEP)
    lines.append("六、设计要求")
    lines.append(THIN_SEP)
    for key, label in [
        ("design_purpose", "设计目的"), ("appearance_change", "改外观/品牌"),
        ("material_change", "改材料"), ("function_change", "改功能"),
    ]:
        lines.append(f"  {label}: {d.get(key, '(未填写)')}")
    lines.append("")

    # 具体情况
    lines.append(THIN_SEP)
    lines.append("七、具体情况")
    lines.append(THIN_SEP)
    for key, label in [
        ("upgrade_details", "升级方向"), ("model_number", "产品型号"), ("erp_cost", "ERP成本"),
    ]:
        lines.append(f"  {label}: {d.get(key, '(未填写)')}")
    lines.append("")

    # 综合评估
    lines.append(THIN_SEP)
    lines.append("八、综合评估")
    lines.append(THIN_SEP)

    valid_scores = []
    if audience_total > 0:
        valid_scores.append(("人群", audience_total))
    if scenario_total > 0:
        valid_scores.append(("场景", scenario_total))
    if competitive_total > 0:
        valid_scores.append(("九宫格", competitive_total))

    if valid_scores:
        weights = {"人群": 0.3, "场景": 0.3, "九宫格": 0.4}
        total_weight = sum(weights.get(n, 0) for n, _ in valid_scores)
        if total_weight > 0:
            overall = int(round(sum(s * weights.get(n, 0) / total_weight for n, s in valid_scores)))
        else:
            overall = sum(s for _, s in valid_scores) // len(valid_scores)

        for name, score in valid_scores:
            lines.append(f"  {name}评分: {score}/100")
        lines.append(f"  综合评分: {overall}/100")
        stars = score_stars(overall)
        lines.append(f"  星级: [{stars_display(stars)}]")

        if overall >= 80: risk = "低"
        elif overall >= 60: risk = "中"
        else: risk = "高"
        lines.append(f"  风险等级: {risk}")
    else:
        lines.append("  综合评分: 暂无法评估（缺少人群/场景/竞品数据或LLM评分）")

    lines.append("")

    if result.warnings:
        lines.append(THIN_SEP)
        lines.append("附: 解析警告")
        lines.append(THIN_SEP)
        for w in result.warnings:
            lines.append(f"  ! {w}")
        lines.append("")

    lines.append(SEPARATOR)
    lines.append("报告结束")
    lines.append(SEPARATOR)

    return "\n".join(lines)
