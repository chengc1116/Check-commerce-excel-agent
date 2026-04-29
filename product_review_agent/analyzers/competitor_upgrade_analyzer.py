# -*- coding: utf-8 -*-
"""
⚔️ 竞品升级分析器

流程：
  1. 从项目数据中提取竞品信息（竞品名称、卖点复制、卖点超越）
  2. 获取我方同品类产品模块（数据库检索）
  3. 获取品类市场概况（品牌分布、销量排行）
  4. 用VL模型拆解竞品图片→模块清单
  5. 重点：竞品优势模块识别 + 差异化超越方案
  6. 对比我方模块 vs 竞品模块 → 差距矩阵 + 超越建议

量化打分（100分制）：
  - 竞品理解度 (25分): 竞品卖点/模块拆解是否清晰
  - 差异化空间 (25分): 差距矩阵中有多少模块可超越
  - 复制可行性 (25分): 竞品卖点可复制程度+复用基础
  - 超越潜力 (25分): 有差异化创新空间的模块数量
"""

from __future__ import annotations

import json
import logging
import os

from product_review_agent.agents.llm_client import LLMClient, get_llm_client
from product_review_agent.product_db.product_query import ProductQuery
from product_review_agent.analyzers.module_vision import (
    vision_decompose,
    vision_decompose_multiple,
    compare_module_sets,
    build_module_table,
    build_product_module_summary,
    ProductCategoryInfo,
)
from product_review_agent.analyzers.base import (
    BaseAnalyzer,
    AnalyzerScore,
    DimensionScore,
    calc_gap_score,
    calc_reuse_score,
    calc_roadmap_score,
    format_product_detail,
)

logger = logging.getLogger(__name__)


class CompetitorUpgradeAnalyzer(BaseAnalyzer):
    """⚔️ 竞品升级分析器"""

    analysis_type = "competitor_upgrade"
    display_name = "竞品升级"
    emoji = "⚔️"

    SCORING_DIMENSIONS = [
        ("竞品理解度", 25, "竞品模块拆解完整度"),
        ("差异化空间", 25, "可超越模块比例"),
        ("复制可行性", 25, "竞品卖点可复制+复用基础"),
        ("超越潜力", 25, "有创新空间的模块数"),
    ]

    async def analyze(self, project_data: dict, images: list = None) -> dict:
        """竞品升级模块对比分析。"""
        llm = get_llm_client()
        category_l3 = project_data.get("category_l3") or project_data.get("categoryl3", "")
        category_l2 = project_data.get("category_l2") or project_data.get("categoryl2", "")
        category_l1 = project_data.get("category_l1") or project_data.get("categoryl1", "")
        brand = project_data.get("brand", "")
        competitor_name = project_data.get("competitor_name", "")
        competitor_strengths_copy = project_data.get("competitor_strengths_copy", "")
        competitor_advantage = project_data.get("competitor_advantage", "")
        product_name = project_data.get("product_name") or project_data.get("project_name", "")

        # 从 product_comparison 中也尝试提取
        pc = project_data.get("product_comparison", {})
        if not competitor_name and pc.get("comparison_name"):
            competitor_name = pc["comparison_name"]
        if not competitor_strengths_copy and pc.get("selling_point"):
            competitor_strengths_copy = pc["selling_point"]
        if not competitor_advantage and pc.get("improving_point"):
            competitor_advantage = pc["improving_point"]

        # Step 1: 获取我方同品类产品+模块
        our_products = []
        our_product_info = {
            "name": f"我方{category_l3 or category_l2}产品",
            "category": f"{category_l2} > {category_l3}",
            "modules": [],
        }
        market_overview = {}

        try:
            with ProductQuery() as pq:
                our_products = pq.get_products_with_modules(
                    category_l2=category_l2,
                    category_l3=category_l3,
                    brand=brand,
                )

                # 批量获取销量数据
                all_skus = [p.get("product_code", "") for p in our_products]
                sales_data = pq.get_products_sales(all_skus)

                # 品类市场概况
                market_overview = pq.get_category_market_overview(
                    category_l2=category_l2,
                    category_l3=category_l3,
                )

                if our_products:
                    seen_cbb = set()
                    all_modules = []
                    for p in our_products:
                        for m in p.get("modules", []):
                            cbb_code = m.get("cbb_code", "")
                            if cbb_code and cbb_code not in seen_cbb:
                                seen_cbb.add(cbb_code)
                                all_modules.append(m)

                    our_product_info["name"] = f"我方{category_l3 or category_l2}产品({len(our_products)}个)"
                    our_product_info["modules"] = all_modules

        except Exception as e:
            logger.error(f"[竞品升级] 数据库查询异常: {e}")

        # Step 2: 用VL模型拆解竞品图片
        competitor_info = {
            "name": competitor_name or "竞品",
            "category": f"{category_l2} > {category_l3}",
            "modules": [],
            "selling_points": {
                "copy": competitor_strengths_copy,
                "advantage": competitor_advantage,
            },
        }

        if images and llm.is_available:
            vision_result = await vision_decompose_multiple(
                llm, images,
                category_info=ProductCategoryInfo(
                    category_l1=category_l1,
                    category_l2=category_l2,
                    category_l3=category_l3,
                    product_name=competitor_name,
                ),
            )
            if vision_result.get("modules"):
                competitor_info["modules"] = vision_result["modules"]
                competitor_info["_source"] = "vision"
                competitor_info["overall_description"] = vision_result.get("overall_description", "")

        # Step 3: 竞品升级专用对比
        comparison = await _competitor_compare(
            llm,
            our_product_info,
            competitor_info,
            project_data,
            market_overview,
        )

        # Step 4: 立项产品模块
        project_modules = project_data.get("project_modules") or project_data.get("module_list")
        if isinstance(project_modules, str):
            try:
                project_modules = json.loads(project_modules)
            except (json.JSONDecodeError, TypeError):
                project_modules = None

        return {
            "analysis_type": "competitor_upgrade",
            "competitor_name": competitor_name,
            "competitor_selling_points": {
                "strengths_copy": competitor_strengths_copy,
                "advantage": competitor_advantage,
            },
            "our_products": [
                {
                    "product_code": p.get("product_code", ""),
                    "sku": p.get("sku", p.get("product_code", "")),
                    "brand": p.get("brand", ""),
                    "category_l2": p.get("category_l2", category_l2),
                    "category_l3": p.get("category_l3", ""),
                    "sales_data": sales_data.get(p.get("product_code", ""), []),
                    "module_count": len(p.get("modules", [])),
                    "modules": p.get("modules", []),
                }
                for p in our_products
            ],
            "market_overview": market_overview,
            "our_product": our_product_info,
            "competitor_product": competitor_info,
            "comparison": comparison,
            "project_modules": project_modules,
        }

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """竞品升级量化打分。"""
        comparison = analysis_result.get("comparison", {})
        summary = comparison.get("summary", {})
        module_comparison = comparison.get("module_comparison", [])
        upgrade_roadmap = comparison.get("upgrade_roadmap", [])
        competitor_product = analysis_result.get("competitor_product", {})

        # 维度1: 竞品理解度 (25分) — 竞品模块拆解完整度
        competitor_modules = competitor_product.get("modules", [])
        if competitor_modules:
            high_conf = sum(1 for m in competitor_modules if m.get("confidence") == "high")
            med_conf = sum(1 for m in competitor_modules if m.get("confidence") == "medium")
            total_mod = len(competitor_modules)
            conf_score = (high_conf * 1.0 + med_conf * 0.6) / total_mod if total_mod > 0 else 0
            understand_score = max(5, int(25 * conf_score))
            understand_reason = f"竞品拆解{total_mod}个模块，高置信{high_conf}个"
        else:
            understand_score = 5
            understand_reason = "无竞品模块拆解数据"

        # 维度2: 差异化空间 (25分) — 可超越/复制模块比例
        if module_comparison:
            surpass_count = sum(1 for mc in module_comparison if mc.get("strategy") in ("超越", "复制"))
            total_count = len(module_comparison)
            surpass_ratio = surpass_count / total_count if total_count > 0 else 0
            diff_score = max(5, int(25 * min(1.0, surpass_ratio * 1.5)))
            diff_reason = f"可超越/复制模块{surpass_count}/{total_count}"
        else:
            diff_score = 8
            diff_reason = "无模块对比数据"

        # 维度3: 复制可行性 (25分) — 复用基础
        copy_score, copy_reason = calc_reuse_score(summary, max_score=25)

        # 维度4: 超越潜力 (25分) — 有创新空间
        surpass_modules = summary.get("surpass_modules", [])
        if surpass_modules:
            potential_score = min(25, 10 + len(surpass_modules) * 5)
            potential_reason = f"可差异化超越{len(surpass_modules)}个模块"
        else:
            potential_score = 8
            potential_reason = "未识别到差异化超越模块"

        dimensions = [
            DimensionScore("竞品理解度", understand_score, 25, understand_reason),
            DimensionScore("差异化空间", diff_score, 25, diff_reason),
            DimensionScore("复制可行性", copy_score, 25, copy_reason),
            DimensionScore("超越潜力", potential_score, 25, potential_reason),
        ]

        total = sum(d.score for d in dimensions)
        strengths = summary.get("our_strengths", [])
        weaknesses = summary.get("our_weaknesses", [])

        suggestions = []
        if understand_score < 15:
            suggestions.append("竞品理解不够深入，建议补充竞品图片或手动拆解")
        if diff_score < 15:
            suggestions.append("差异化空间有限，需从设计/品牌层面寻找突破点")
        if potential_score < 15:
            suggestions.append("超越潜力不足，建议聚焦1个核心差异化创新")
        if copy_score < 12:
            suggestions.append("复用基础薄弱，建议先盘点现有CBB模块库再制定超越方案")

        return AnalyzerScore(
            analysis_type=self.analysis_type,
            dimensions=dimensions,
            total_score=total,
            max_score=100,
            strengths=strengths,
            weaknesses=weaknesses,
            suggestions=suggestions,
        )

    def format_report(self, analysis_result: dict, score: AnalyzerScore = None) -> str:
        """格式化竞品升级分析结果"""
        if not analysis_result:
            return "  (竞品升级分析不可用)"

        lines = []
        lines.append("[⚔️ 竞品升级分析]")

        # 评分总览
        if score:
            lines.append(f"  评分: {score.total_score}/100 {'★' * score.star_rating}{'☆' * (5 - score.star_rating)} 风险: {score.risk_level}")
            for d in score.dimensions:
                lines.append(f"    {d.name}: {d.score}/{d.max_score} - {d.reason}")
            lines.append("")

        # 竞品信息
        competitor_name = analysis_result.get("competitor_name", "未知竞品")
        lines.append(f"  ── 目标竞品: {competitor_name} ──")

        selling_points = analysis_result.get("competitor_selling_points", {})
        if selling_points.get("strengths_copy"):
            lines.append(f"  竞品卖点(复制): {selling_points['strengths_copy']}")
        if selling_points.get("advantage"):
            lines.append(f"  竞品卖点(超越): {selling_points['advantage']}")
        lines.append("")

        # 品类市场概况
        market_overview = analysis_result.get("market_overview", {})
        if market_overview:
            lines.append("  ── 品类市场概况 ──")
            total_products = market_overview.get("total_products", 0)
            total_sales = market_overview.get("total_category_sales", 0)
            brand_dist = market_overview.get("brand_distribution", [])
            lines.append(f"  品类产品总数: {total_products}个 | 累计总销量: {total_sales:,}")
            if brand_dist:
                lines.append(f"  品牌份额:")
                for bd in brand_dist[:6]:
                    pct = (bd["total_sales"] / total_sales * 100) if total_sales > 0 else 0
                    lines.append(f"    • {bd['brand']}: {bd['product_count']}个产品, 销量{bd['total_sales']:,} ({pct:.1f}%)")
            lines.append("")

        # 我方产品（含货号+销量+模块明细）
        our_products = analysis_result.get("our_products", [])
        if our_products:
            lines.append(f"  ── 我方同品类产品 ({len(our_products)}个) ──")
            for p in our_products[:10]:
                lines.extend(format_product_detail(p, indent="    "))
            if len(our_products) > 10:
                lines.append(f"    ... 还有{len(our_products)-10}个产品")

        # 逐模块对比
        comparison = analysis_result.get("comparison", {})
        summary = comparison.get("summary", {})
        module_comparison = comparison.get("module_comparison", [])

        if module_comparison:
            lines.append("")
            lines.append("  ── 逐模块对比（我方 vs 竞品） ──")
            lines.append(f"  {'模块':<10} {'我方状态':<8} {'差距':<6} {'策略':<6} {'建议'}")
            lines.append(f"  {'-'*60}")
            for mc in module_comparison:
                strategy = mc.get("strategy", "")
                strategy_icon = {"复制": "📋", "对标": "🎯", "超越": "🚀", "保持": "⏸️"}.get(strategy, "")
                lines.append(f"  {mc.get('module_name', ''):<10} {mc.get('our_status', ''):<8} "
                            f"{mc.get('gap_level', ''):<6} {strategy_icon}{strategy:<4} {mc.get('suggestion', '')}")

        if summary:
            lines.append("")
            lines.append("  ── 分析摘要 ──")
            if summary.get("our_strengths"):
                lines.append("  我方优势:")
                for s in summary["our_strengths"]:
                    lines.append(f"    + {s}")
            if summary.get("our_weaknesses"):
                lines.append("  我方不足:")
                for w in summary["our_weaknesses"]:
                    lines.append(f"    - {w}")
            if summary.get("copy_modules"):
                lines.append("  需从竞品复制:")
                for m in summary["copy_modules"]:
                    lines.append(f"    📋 {m}")
            if summary.get("surpass_modules"):
                lines.append("  可差异化超越:")
                for m in summary["surpass_modules"]:
                    lines.append(f"    🚀 {m}")
            if summary.get("reuse_modules"):
                lines.append("  可复用模块:")
                for m in summary["reuse_modules"]:
                    lines.append(f"    ✅ {m}")
            if summary.get("new_modules_needed"):
                lines.append("  需新建模块:")
                for m in summary["new_modules_needed"]:
                    lines.append(f"    🆕 {m}")
            if summary.get("overall_assessment"):
                lines.append(f"  整体评价: {summary['overall_assessment']}")

        # 升级路线图
        roadmap = comparison.get("upgrade_roadmap", [])
        if roadmap:
            lines.append("")
            lines.append("  ── 升级路线图 ──")
            for r in roadmap:
                strategy = r.get("strategy", "")
                strategy_icon = {"复制": "📋", "对标": "🎯", "超越": "🚀", "保持": "⏸️"}.get(strategy, "")
                lines.append(f"    [{r.get('priority', '?')}] {r.get('module', '?')}: "
                            f"{r.get('action', '')} {strategy_icon}{strategy} → {r.get('expected_impact', '')}")

        if score and score.suggestions:
            lines.append("")
            lines.append("  改进建议:")
            for s in score.suggestions:
                lines.append(f"    > {s}")

        return "\n".join(lines)


async def _competitor_compare(
    llm: LLMClient,
    our_product: dict,
    competitor_product: dict,
    project_data: dict,
    market_overview: dict = None,
) -> dict:
    """竞品升级专用对比分析，强调竞品优势识别和差异化超越。"""
    if not llm.is_available:
        return {
            "module_comparison": [],
            "summary": {
                "our_strengths": [],
                "our_weaknesses": [],
                "reuse_modules": [],
                "new_modules_needed": [],
                "overall_assessment": "LLM不可用，无法进行竞品对比分析",
            },
            "upgrade_roadmap": [],
            "_error": "LLM不可用",
        }

    our_table = build_module_table(our_product.get("modules", []))
    competitor_table = build_module_table(competitor_product.get("modules", []))

    competitor_name = competitor_product.get("name", "竞品")
    selling_points = competitor_product.get("selling_points", {})

    sp_section = ""
    if selling_points.get("copy"):
        sp_section += f"\n竞品卖点（需复制）: {selling_points['copy']}\n"
    if selling_points.get("advantage"):
        sp_section += f"竞品卖点（需超越）: {selling_points['advantage']}\n"

    # 市场概况
    market_section = ""
    if market_overview:
        total_prods = market_overview.get("total_products", 0)
        total_sales = market_overview.get("total_category_sales", 0)
        brand_dist = market_overview.get("brand_distribution", [])
        top_selling = market_overview.get("top_selling_products", [])

        market_section = f"\n== 品类市场概况 ==\n"
        market_section += f"品类产品总数: {total_prods}个\n"
        market_section += f"品类累计总销量: {total_sales:,}\n"
        if brand_dist:
            market_section += "品牌分布:\n"
            for bd in brand_dist[:6]:
                pct = (bd["total_sales"] / total_sales * 100) if total_sales > 0 else 0
                market_section += f"  - {bd['brand']}: {bd['product_count']}个产品, 销量{bd['total_sales']:,} ({pct:.1f}%)\n"
        if top_selling:
            market_section += "销量TOP产品:\n"
            for ts in top_selling[:5]:
                market_section += f"  - {ts['product_code']} ({ts['brand']}) - {ts['category_l3']} | 月最高{ts['max_sales']:,}\n"

    project_modules = project_data.get("project_modules") or project_data.get("module_list")
    if project_modules:
        project_info = f"立项产品已有模块:\n{build_module_table(project_modules)}"
    else:
        project_info = "（立项产品模块信息暂缺）"

    prompt = f"""【任务】深入对比我方产品与竞品的模块构成，重点输出差异化超越方案和竞争策略。
【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。

== 我方产品信息 ==
产品名称: {our_product.get('name', '我方产品')}
品类: {our_product.get('category', '')}

== 我方产品模块 ==
{our_table}

== 竞品信息 ==
竞品名称: {competitor_name}
竞品品类: {competitor_product.get('category', '')}
{sp_section}
{market_section}
== 竞品模块（来自图片拆解） ==
{competitor_table}

== 立项产品模块信息 ==
{project_info}

【分析要求】
1. 逐模块对比：找到我方和竞品相同/相似模块，详细评估各自优劣
2. 竞品优势识别：竞品哪些模块/设计领先，需要复制或对标？具体说明差距
3. 差异化超越：我方可以在哪些模块上做出差异化创新？给出具体创新方向
4. 市场竞争策略：结合品类市场概况，给出我方的竞争定位建议
5. 升级优先级：结合竞品卖点复制+超越需求排序
6. 可复用建议：哪些现有模块可以直接复用，哪些需要新建/改造

【输出格式】严格按此JSON结构输出（module_comparison至少4条，summary各字段2-4条且内容详细，upgrade_roadmap至少3条）：
{{
    "module_comparison": [
        {{
            "module_name": "绑带",
            "our_status": "持平",
            "competitor_status": "竞品采用X型交叉",
            "gap_level": "低",
            "user_perception": "高",
            "upgrade_priority": "P2",
            "suggestion": "保持现有设计",
            "strategy": "保持"
        }},
        {{
            "module_name": "减震垫",
            "our_status": "落后",
            "competitor_status": "加厚硅胶垫",
            "gap_level": "高",
            "user_perception": "高",
            "upgrade_priority": "P0",
            "suggestion": "复制竞品加厚设计",
            "strategy": "复制"
        }}
    ],
    "summary": {{
        "our_strengths": ["我方优势1（具体说明）", "我方优势2", "我方优势3"],
        "our_weaknesses": ["我方不足1（具体说明）", "我方不足2"],
        "copy_modules": ["需要从竞品复制的模块1（具体说明）", "需要复制的2"],
        "surpass_modules": ["可以差异化超越的模块1（创新方向）", "可以超越的2"],
        "reuse_modules": ["可复用现有模块1 (来源产品)", "可复用2"],
        "new_modules_needed": ["需新建模块1 (具体说明)", "需新建2"],
        "competitive_strategy": "竞争策略建议（150-200字）",
        "overall_assessment": "整体评价（150-300字，结合市场数据和竞品对比给出综合结论）"
    }},
    "upgrade_roadmap": [
        {{
            "priority": "P0",
            "module": "减震垫",
            "action": "升级为加厚硅胶垫",
            "strategy": "复制",
            "expected_impact": "缩小与竞品差距"
        }}
    ]
}}"""

    original_max_tokens = llm.max_tokens
    llm.max_tokens = 8192

    try:
        result = await llm.acall_text(
            [
                {"role": "system", "content": "你是竞品分析专家，擅长模块化产品对比和差异化策略制定。严格只返回JSON对象，禁止输出思考过程、解释文字或markdown代码块。分析必须基于提供的数据，给出具体可执行的结论。"},
                {"role": "user", "content": prompt},
            ],
            response_format="json",
        )

        if isinstance(result, dict) and not result.get("_parse_error"):
            return result
        else:
            return {
                "module_comparison": [],
                "summary": {
                    "our_strengths": [],
                    "our_weaknesses": [],
                    "reuse_modules": [],
                    "new_modules_needed": [],
                    "overall_assessment": "竞品分析返回格式异常",
                },
                "upgrade_roadmap": [],
                "_error": "返回格式异常",
            }

    except Exception as e:
        logger.error(f"[竞品对比] 异常: {e}")
        return {
            "module_comparison": [],
            "summary": {
                "our_strengths": [],
                "our_weaknesses": [],
                "reuse_modules": [],
                "new_modules_needed": [],
                "overall_assessment": f"竞品分析异常: {e}",
            },
            "upgrade_roadmap": [],
            "_error": str(e),
        }

    finally:
        llm.max_tokens = original_max_tokens
