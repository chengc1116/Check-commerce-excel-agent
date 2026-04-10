# -*- coding: utf-8 -*-
"""
飞书消息卡片构建器

将审核结果转换为飞书交互式消息卡片。
"""

from __future__ import annotations

import json
from typing import Optional


def _score_emoji(score: int) -> str:
    """评分对应emoji"""
    if score >= 80:
        return "\u2705"  # ✅
    elif score >= 60:
        return "\u26a0\ufe0f"  # ⚠️
    else:
        return "\u274c"  # ❌


def _risk_color(risk: str) -> str:
    """风险等级对应颜色"""
    if risk == "低":
        return "green"
    elif risk == "中":
        return "orange"
    else:
        return "red"


def _score_bar(score: int) -> str:
    """生成分数进度条文本"""
    filled = int(score / 10)
    return "|" + "=" * filled + "-" * (10 - filled) + "|"


def build_review_card(
    file_name: str,
    overall_score: int,
    risk_level: str,
    scores: dict,
    elapsed: float,
    report_text: Optional[str] = None,
    error: Optional[str] = None,
) -> dict:
    """
    构建飞书审核结果消息卡片。

    Args:
        file_name: 审核的文件名
        overall_score: 综合评分
        risk_level: 风险等级 (低/中/高)
        scores: 各维度评分
        elapsed: 耗时(秒)
        report_text: 完整文字报告(可选,用于折叠展示)
        error: 错误信息(如有)

    Returns:
        飞书消息卡片JSON
    """
    if error:
        return _build_error_card(file_name, error)

    audience_score = scores.get("audience", {}).get("total_score", 0)
    scenario_score = scores.get("scenario", {}).get("total_score", 0)
    competitive_score = scores.get("competitive", {}).get("total_score", 0)

    # 构建评分维度行
    dimension_elements = []

    # 综合评分头部
    dimension_elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"**{file_name}** 审核完成\n"
                f"综合评分: **{overall_score}/100** {_score_emoji(overall_score)}\n"
                f"风险等级: **{risk_level}** | 耗时: {elapsed:.1f}s"
            ),
        },
    })

    dimension_elements.append({"tag": "hr"})

    # 人群评分
    if audience_score > 0:
        audience_dims = scores.get("audience", {}).get("dimensions", {})
        dim_lines = []
        for dim_name, dim_info in audience_dims.items():
            s = dim_info.get("score", 0) if isinstance(dim_info, dict) else 0
            dim_lines.append(f"  {dim_name}: {s}/25")

        dimension_elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**人群分析** {audience_score}/100 {_score_emoji(audience_score)}\n"
                    + "\n".join(dim_lines)
                ),
            },
        })

        # 优势/不足
        _append_strength_weakness(dimension_elements, scores.get("audience", {}))

    # 场景评分
    if scenario_score > 0:
        scenario_dims = scores.get("scenario", {}).get("dimensions", {})
        dim_lines = []
        for dim_name, dim_info in scenario_dims.items():
            s = dim_info.get("score", 0) if isinstance(dim_info, dict) else 0
            dim_lines.append(f"  {dim_name}: {s}/25")

        dimension_elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**场景分析** {scenario_score}/100 {_score_emoji(scenario_score)}\n"
                    + "\n".join(dim_lines)
                ),
            },
        })

        _append_strength_weakness(dimension_elements, scores.get("scenario", {}))

    # 九宫格评分
    if competitive_score > 0:
        competitive_dims = scores.get("competitive", {}).get("dimensions", {})
        dim_lines = []
        for dim_name, dim_info in competitive_dims.items():
            s = dim_info.get("score", 0) if isinstance(dim_info, dict) else 0
            dim_lines.append(f"  {dim_name}: {s}/25")

        dimension_elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**九宫格目标** {competitive_score}/100 {_score_emoji(competitive_score)}\n"
                    + "\n".join(dim_lines)
                ),
            },
        })

        _append_strength_weakness(dimension_elements, scores.get("competitive", {}))

    # 改进建议（汇总）
    all_suggestions = []
    for key in ["audience", "scenario", "competitive"]:
        suggestions = scores.get(key, {}).get("suggestions", [])
        if suggestions:
            label = {"audience": "人群", "scenario": "场景", "competitive": "九宫格"}[key]
            for s in suggestions:
                all_suggestions.append(f"[{label}] {s}")

    if all_suggestions:
        dimension_elements.append({"tag": "hr"})
        dimension_elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**改进建议**\n" + "\n".join(f"- {s}" for s in all_suggestions[:8]),
            },
        })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "产品立项审核报告",
            },
            "template": _risk_color(risk_level),
        },
        "elements": dimension_elements,
    }

    return card


def _append_strength_weakness(elements: list, score: dict):
    """追加优势/不足到卡片元素"""
    strengths = score.get("strengths", [])
    weaknesses = score.get("weaknesses", [])

    lines = []
    if strengths:
        for s in strengths[:3]:
            lines.append(f"+ {s}")
    if weaknesses:
        for w in weaknesses[:3]:
            lines.append(f"- {w}")

    if lines:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(lines),
            },
        })


def _build_error_card(file_name: str, error: str) -> dict:
    """构建错误消息卡片"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "审核失败",
            },
            "template": "red",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"文件: **{file_name}**\n错误: {error}",
                },
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "请检查文件格式是否正确，或联系管理员。",
                },
            },
        ],
    }


def build_processing_card(file_name: str) -> dict:
    """构建"正在审核中"消息卡片"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "审核进行中",
            },
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"正在审核 **{file_name}** ...\n预计需要15-30秒，请稍候。",
                },
            },
        ],
    }
