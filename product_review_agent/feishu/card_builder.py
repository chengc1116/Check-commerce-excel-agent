# -*- coding: utf-8 -*-
"""
飞书消息卡片构建器

将审核结果转换为飞书交互式消息卡片。
"""

from __future__ import annotations

import json
from typing import Optional


def build_task_selection_card() -> dict:
    """
    构建任务类型选择卡片。
    
    用户点击按钮后，飞书会发送 card.action.trigger 回调，
    action.value 中包含 task_type 值（如 "hot_upgrade"）。
    """
    task_buttons = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔥 爆品升级"},
            "type": "primary",
            "value": {"task_type": "hot_upgrade"},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "⚔️ 竞品升级"},
            "type": "primary",
            "value": {"task_type": "competitor_upgrade"},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📉 未起量迭代"},
            "type": "primary",
            "value": {"task_type": "low_sale_iterate"},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🗺️ 品类地图缺失"},
            "type": "primary",
            "value": {"task_type": "category_gap"},
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "📋 请选择审核任务类型",
            },
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "请点击选择本次立项审核的任务类型：",
                },
            },
            {
                "tag": "action",
                "actions": task_buttons,
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "选择后请上传Excel文件（.xlsx）开始审核。",
                },
            },
        ],
    }


def build_task_selected_card(task_label: str, task_emoji: str) -> dict:
    """构建任务选择确认卡片（含更换按钮）"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"{task_emoji} 已选择: {task_label}",
            },
            "template": "green",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"您选择的审核类型: **{task_emoji} {task_label}**\n\n"
                        f"请现在上传Excel文件（.xlsx）开始审核。\n\n"
                        f"⏳ 选择5分钟内有效，超时需重新选择。"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🔄 更换任务类型"},
                        "type": "default",
                        "value": {"action": "change_task"},
                    },
                ],
            },
        ],
    }


def build_no_task_selected_card() -> dict:
    """构建提示用户先选择任务的卡片"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "⚠️ 请先选择任务类型",
            },
            "template": "orange",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "请先发送任意消息选择审核任务类型，然后再上传Excel文件。",
                },
            },
        ],
    }


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
    product_analysis: Optional[dict] = None,
    specific_score=None,
    task_label: str = "",
) -> dict:
    """
    构建飞书审核结果消息卡片。

    Args:
        file_name: 审核的文件名
        overall_score: 综合评分
        risk_level: 风险等级 (低/中/高)
        scores: 各维度评分（公共：audience, scenario）
        elapsed: 耗时(秒)
        report_text: 完整文字报告(可选,用于折叠展示)
        error: 错误信息(如有)
        product_analysis: 同类产品分析(可选,已整合到报告中)
        specific_score: 专项分析评分(AnalyzerScore对象或dict)
        task_label: 任务类型标签(如 "🔥 爆品升级")

    Returns:
        飞书消息卡片JSON
    """
    if error:
        return _build_error_card(file_name, error)

    audience_score = scores.get("audience", {}).get("total_score", 0)
    scenario_score = scores.get("scenario", {}).get("total_score", 0)

    # 构建评分维度行
    dimension_elements = []

    # 综合评分头部
    header_content = f"**{file_name}** 审核完成\n"
    if task_label:
        header_content += f"审核类型: {task_label}\n"
    header_content += (
        f"综合评分: **{overall_score}/100** {_score_emoji(overall_score)}\n"
        f"风险等级: **{risk_level}** | 耗时: {elapsed:.1f}s"
    )

    dimension_elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": header_content,
        },
    })

    dimension_elements.append({"tag": "hr"})

    # 人群分析（仅展示分析内容，不评分）
    audience_analysis = scores.get("audience", {})
    if audience_analysis:
        audience_dims = audience_analysis.get("dimensions", {})
        dim_lines = []
        for dim_name, dim_info in audience_dims.items():
            reason = dim_info.get("reason", "") if isinstance(dim_info, dict) else ""
            if reason:
                dim_lines.append(f"  {dim_name}: {reason[:60]}")

        if dim_lines:
            dimension_elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**人群分析**\n" + "\n".join(dim_lines),
                },
            })

        _append_strength_weakness(dimension_elements, audience_analysis)

    # 场景分析（仅展示分析内容，不评分）
    scenario_analysis = scores.get("scenario", {})
    if scenario_analysis:
        scenario_dims = scenario_analysis.get("dimensions", {})
        dim_lines = []
        for dim_name, dim_info in scenario_dims.items():
            reason = dim_info.get("reason", "") if isinstance(dim_info, dict) else ""
            if reason:
                dim_lines.append(f"  {dim_name}: {reason[:60]}")

        if dim_lines:
            dimension_elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**场景分析**\n" + "\n".join(dim_lines),
                },
            })

        _append_strength_weakness(dimension_elements, scenario_analysis)

    # 🆕 专项分析评分
    if specific_score:
        dimension_elements.append({"tag": "hr"})
        _append_specific_score_card(dimension_elements, specific_score)

    # 改进建议（汇总）
    all_suggestions = []
    for key in ["audience", "scenario"]:
        suggestions = scores.get(key, {}).get("suggestions", [])
        if suggestions:
            label = {"audience": "人群", "scenario": "场景"}.get(key, key)
            for s in suggestions:
                all_suggestions.append(f"[{label}] {s}")

    # 专项分析建议
    if specific_score and hasattr(specific_score, "suggestions"):
        for s in specific_score.suggestions:
            all_suggestions.append(f"[专项] {s}")
    elif specific_score and isinstance(specific_score, dict):
        for s in specific_score.get("suggestions", []):
            all_suggestions.append(f"[专项] {s}")

    if all_suggestions:
        dimension_elements.append({"tag": "hr"})
        dimension_elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**改进建议**\n" + "\n".join(f"- {s}" for s in all_suggestions[:10]),
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


def _append_specific_score_card(elements: list, specific_score):
    """在飞书卡片中追加专项分析评分板块"""
    # 兼容 AnalyzerScore 对象和 dict
    if hasattr(specific_score, "total_score"):
        total = specific_score.total_score
        analysis_type = getattr(specific_score, "analysis_type", "专项")
        dimensions = specific_score.dimensions if hasattr(specific_score, "dimensions") else []
        strengths = specific_score.strengths if hasattr(specific_score, "strengths") else []
        weaknesses = specific_score.weaknesses if hasattr(specific_score, "weaknesses") else []
    else:
        total = specific_score.get("total_score", 0)
        analysis_type = specific_score.get("analysis_type", "专项")
        dimensions = specific_score.get("dimensions", [])
        strengths = specific_score.get("strengths", [])
        weaknesses = specific_score.get("weaknesses", [])

    # 标题
    type_labels = {
        "hot_upgrade": "🔥 爆品升级",
        "competitor_upgrade": "⚔️ 竞品升级",
        "low_sale_iterate": "📉 未起量迭代",
        "category_gap": "🗺️ 品类地图缺失",
    }
    label = type_labels.get(analysis_type, "📋 专项分析")

    dim_lines = []
    for d in dimensions:
        if isinstance(d, dict):
            name = d.get("name", "")
            score = d.get("score", 0)
            max_s = d.get("max_score", 25)
            reason = d.get("reason", "")
        elif hasattr(d, "name"):
            name = d.name
            score = d.score
            max_s = d.max_score
            reason = d.reason
        else:
            continue
        # 多行reason拆分显示
        sub_lines = [r.strip() for r in reason.split("\n") if r.strip()]
        if sub_lines:
            dim_lines.append(f"  {name}: {score}/{max_s}")
            for sl in sub_lines:
                dim_lines.append(f"    {sl}")
        else:
            dim_lines.append(f"  {name}: {score}/{max_s}")

    content = f"**{label}专项分析** {total}/100 {_score_emoji(total)}\n"
    if dim_lines:
        content += "\n".join(dim_lines)

    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": content,
        },
    })

    # 优势/不足
    lines = []
    for s in strengths[:3]:
        lines.append(f"+ {s}")
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


def _append_product_analysis_card(elements: list, analysis: dict):
    """在飞书卡片中追加同类产品分析板块"""
    products = analysis.get("products", [])
    ai_analysis = analysis.get("analysis")

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": "**同类产品及销售情况**（共 %d 个）" % len(products),
        },
    })

    # 展示前5个产品的简要信息
    for p in products[:5]:
        sku = p.get("sku", "-")
        brand = p.get("brand", "-")
        version = p.get("version", "")
        cat3 = p.get("category_l3", "")
        sales = p.get("recent_sales", [])

        label = f"{sku}"
        if brand and brand != "-":
            label += f" · {brand}"
        if version:
            label += f" · {version}"

        # 销量趋势
        if len(sales) >= 2:
            m1 = sales[1].get("sales_volume", 0)
            m2 = sales[0].get("sales_volume", 0)
            trend = "↑" if m2 > m1 else ("↓" if m2 < m1 else "—")
            sales_str = f"{m1} → {m2} {trend}"
        elif len(sales) == 1:
            sales_str = str(sales[0].get("sales_volume", 0))
        else:
            sales_str = "暂无数据"

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"• **{label}**  销量: {sales_str}",
            },
        })

    if len(products) > 5:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"... 还有 {len(products) - 5} 个产品，详见文字报告",
            },
        })

    # AI 分析摘要
    if ai_analysis and ai_analysis.get("analysis"):
        elements.append({"tag": "hr"})
        summary = ai_analysis["analysis"]
        # 截取前300字
        if len(summary) > 300:
            summary = summary[:300] + "..."
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**AI分析**: %s" % summary,
            },
        })
