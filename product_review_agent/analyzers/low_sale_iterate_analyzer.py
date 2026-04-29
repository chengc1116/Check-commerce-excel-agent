# -*- coding: utf-8 -*-
"""
📉 未起量迭代分析器

流程：
  1. 按二级品类检索所有产品，获取模块
  2. 标记已起量产品（任一月销量>500）
  3. 获取品类市场概况（品牌分布、销量排行）
  4. 用VL模型拆解竞品图片
  5. 对比我方全部产品模块 vs 竞品模块 → 找可复用模块
  6. 特别说明：已起量产品需标注

量化打分（100分制）：
  - 迭代方向清晰度 (25分): 升级路线图是否聚焦
  - 可复用基础 (25分): 现有模块复用率
  - 增量空间 (25分): 竞品有但我们要补的模块
  - 风险可控度 (25分): 已起量产品影响+新建模块风险
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


class LowSaleIterateAnalyzer(BaseAnalyzer):
    """📉 未起量迭代分析器"""

    analysis_type = "low_sale_iterate"
    display_name = "未起量迭代"
    emoji = "📉"

    SCORING_DIMENSIONS = [
        ("迭代方向清晰度", 25, "升级路线图聚焦度"),
        ("可复用基础", 25, "现有模块复用率"),
        ("增量空间", 25, "竞品有但我们要补的模块"),
        ("风险可控度", 25, "已起量产品+新建模块风险"),
    ]

    async def analyze(self, project_data: dict, images: list = None) -> dict:
        """未起量迭代模块对比分析。"""
        llm = get_llm_client()
        category_l3 = project_data.get("category_l3") or project_data.get("categoryl3", "")
        category_l2 = project_data.get("category_l2") or project_data.get("categoryl2", "")
        category_l1 = project_data.get("category_l1") or project_data.get("categoryl1", "")
        brand = project_data.get("brand", "")
        competitor_name = project_data.get("competitor_name", "")
        product_name = project_data.get("product_name") or project_data.get("project_name", "")

        # Step 1: 检索品类下所有产品
        all_products = []
        launched_products = []
        not_launched_products = []
        market_overview = {}

        try:
            with ProductQuery() as pq:
                # 按二级品类检索所有产品
                all_products = pq.get_products_with_modules(
                    category_l2=category_l2,
                    brand=brand,
                )

                # 批量获取销量数据
                all_skus = [p.get("product_code", "") for p in all_products]
                sales_data = pq.get_products_sales(all_skus)

                launched_check = pq.check_product_launched(
                    category_l2=category_l2,
                    brand=brand,
                    threshold=500,
                )

                # 品类市场概况
                market_overview = pq.get_category_market_overview(
                    category_l2=category_l2,
                )

                launched_map = {}
                for lc in launched_check:
                    launched_map[lc["product_code"]] = lc

                for p in all_products:
                    code = p.get("product_code", "")
                    lc = launched_map.get(code, {})
                    p["launched"] = lc.get("launched", False)
                    p["max_sales"] = lc.get("max_sales", 0)
                    p["launched_month"] = lc.get("launched_month", "")

                    if p["launched"]:
                        launched_products.append(p)
                    else:
                        not_launched_products.append(p)

        except Exception as e:
            logger.error(f"[未起量迭代] 数据库查询异常: {e}")

        # Step 2: 汇总我方所有产品的模块
        our_product_info = {
            "name": f"我方{category_l3 or category_l2}全部产品({len(all_products)}个)",
            "category": f"{category_l2} > {category_l3}",
            "modules": [],
        }

        seen_cbb = set()
        all_our_modules = []
        for p in all_products:
            for m in p.get("modules", []):
                cbb_code = m.get("cbb_code", "")
                if cbb_code and cbb_code not in seen_cbb:
                    seen_cbb.add(cbb_code)
                    all_our_modules.append(m)

        our_product_info["modules"] = all_our_modules

        # Step 3: 拆解竞品模块
        competitor_info = {
            "name": competitor_name or "竞品",
            "category": f"{category_l2} > {category_l3}",
            "modules": [],
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

        # Step 4: 立项产品模块
        project_modules = project_data.get("project_modules") or project_data.get("module_list")
        if isinstance(project_modules, str):
            try:
                project_modules = json.loads(project_modules)
            except (json.JSONDecodeError, TypeError):
                project_modules = None

        # Step 5: 对比
        comparison = await compare_module_sets(
            llm, our_product_info, competitor_info, project_modules
        )

        if launched_products and comparison.get("summary"):
            launched_names = [f"{p['product_code']}(销量{p['max_sales']})" for p in launched_products]
            comparison["summary"]["launched_products_note"] = (
                f"⚠️ 注意：以下产品已起量（任一月销量>500），不推荐直接迭代: {', '.join(launched_names)}"
            )

        return {
            "analysis_type": "low_sale_iterate",
            "all_products": [
                {
                    "product_code": p.get("product_code", ""),
                    "sku": p.get("sku", p.get("product_code", "")),
                    "brand": p.get("brand", ""),
                    "category_l2": p.get("category_l2", category_l2),
                    "category_l3": p.get("category_l3", ""),
                    "launched": p.get("launched", False),
                    "max_sales": p.get("max_sales", 0),
                    "sales_data": sales_data.get(p.get("product_code", ""), []),
                    "module_count": len(p.get("modules", [])),
                    "modules": p.get("modules", []),
                }
                for p in all_products
            ],
            "launched_products": [
                {
                    "product_code": p["product_code"],
                    "sku": p.get("sku", p["product_code"]),
                    "brand": p.get("brand", ""),
                    "category_l2": p.get("category_l2", category_l2),
                    "category_l3": p.get("category_l3", ""),
                    "max_sales": p["max_sales"],
                    "sales_data": sales_data.get(p["product_code"], []),
                    "modules": p.get("modules", []),
                }
                for p in launched_products
            ],
            "not_launched_products": [
                {
                    "product_code": p["product_code"],
                    "sku": p.get("sku", p["product_code"]),
                    "brand": p.get("brand", ""),
                    "category_l2": p.get("category_l2", category_l2),
                    "category_l3": p.get("category_l3", ""),
                    "sales_data": sales_data.get(p["product_code"], []),
                    "module_count": len(p.get("modules", [])),
                    "modules": p.get("modules", []),
                }
                for p in not_launched_products
            ],
            "market_overview": market_overview,
            "our_product": our_product_info,
            "competitor_product": competitor_info,
            "comparison": comparison,
            "project_modules": project_modules,
        }

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """未起量迭代量化打分。"""
        comparison = analysis_result.get("comparison", {})
        summary = comparison.get("summary", {})
        module_comparison = comparison.get("module_comparison", [])
        upgrade_roadmap = comparison.get("upgrade_roadmap", [])
        launched_products = analysis_result.get("launched_products", [])
        not_launched = analysis_result.get("not_launched_products", [])

        # 维度1: 迭代方向清晰度 (25分)
        direction_score, direction_reason = calc_roadmap_score(upgrade_roadmap, max_score=25)

        # 维度2: 可复用基础 (25分)
        reuse_s, reuse_r = calc_reuse_score(summary, max_score=25)

        # 维度3: 增量空间 (25分) — 竞品有但我们要补的模块
        gap_modules = [mc for mc in module_comparison if mc.get("gap_level") in ("高", "中")]
        if module_comparison:
            gap_ratio = len(gap_modules) / len(module_comparison)
            if 0.2 <= gap_ratio <= 0.6:
                increment_score = 20
            elif gap_ratio < 0.2:
                increment_score = 12
            else:
                increment_score = 10
            increment_reason = f"有差距模块{len(gap_modules)}/{len(module_comparison)}，差距比{gap_ratio:.0%}"
        else:
            increment_score = 8
            increment_reason = "无模块对比数据"

        # 维度4: 风险可控度 (25分)
        new_count = len(summary.get("new_modules_needed", []))
        reuse_count = len(summary.get("reuse_modules", []))
        total_ops = new_count + reuse_count
        if total_ops > 0:
            new_ratio = new_count / total_ops
            risk_score = max(5, int(25 * (1 - new_ratio * 0.8)))
            risk_reason = f"新建{new_count}项，复用{reuse_count}项，新建占比{new_ratio:.0%}"
        else:
            risk_score = 10
            risk_reason = "无模块操作数据"

        # 已起量产品影响
        if launched_products:
            risk_score = max(5, risk_score - 5)
            risk_reason += f"，⚠️有{len(launched_products)}个已起量产品"

        dimensions = [
            DimensionScore("迭代方向清晰度", direction_score, 25, direction_reason),
            DimensionScore("可复用基础", reuse_s, 25, reuse_r),
            DimensionScore("增量空间", increment_score, 25, increment_reason),
            DimensionScore("风险可控度", risk_score, 25, risk_reason),
        ]

        total = sum(d.score for d in dimensions)
        strengths = summary.get("our_strengths", [])
        weaknesses = summary.get("our_weaknesses", [])

        suggestions = []
        if direction_score < 15:
            suggestions.append("迭代方向不够聚焦，建议明确1-2个核心迭代方向")
        if increment_score < 12:
            suggestions.append("增量空间评估不足，建议深入分析竞品优势模块")
        if risk_score < 15:
            suggestions.append("迭代风险偏高，建议优先复用现有模块，控制新建比例")
        if not_launched and len(not_launched) > 5:
            suggestions.append(f"有{len(not_launched)}个未起量产品，建议选择1-2个最有潜力的重点迭代")

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
        """格式化未起量迭代分析结果"""
        if not analysis_result:
            return "  (未起量迭代分析不可用)"

        lines = []
        lines.append("[📉 未起量迭代分析]")

        # 评分总览
        if score:
            lines.append(f"  评分: {score.total_score}/100 {'★' * score.star_rating}{'☆' * (5 - score.star_rating)} 风险: {score.risk_level}")
            for d in score.dimensions:
                lines.append(f"    {d.name}: {d.score}/{d.max_score} - {d.reason}")
            lines.append("")

        all_prods = analysis_result.get("all_products", [])
        launched = analysis_result.get("launched_products", [])
        not_launched = analysis_result.get("not_launched_products", [])

        lines.append(f"  品类下产品: {len(all_prods)}个 (已起量{len(launched)}个, 未起量{len(not_launched)}个)")

        # 品类市场概况
        market_overview = analysis_result.get("market_overview", {})
        if market_overview:
            lines.append("")
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
            top_selling = market_overview.get("top_selling_products", [])
            if top_selling:
                lines.append(f"  品类销量TOP:")
                for ts in top_selling[:5]:
                    lines.append(f"    🔥 {ts['product_code']} ({ts['brand']}) - {ts['category_l3']} | 月最高{ts['max_sales']:,}")

        # 已起量产品
        if launched:
            lines.append("")
            lines.append(f"  ── ⚠️ 已起量产品（任一月销量>500，{len(launched)}个）──")
            for p in launched[:10]:
                lines.extend(format_product_detail(p, indent="    "))
            if len(launched) > 10:
                lines.append(f"    ... 还有{len(launched)-10}个已起量产品")

        # 未起量产品
        if not_launched:
            lines.append("")
            lines.append(f"  ── 未起量产品（{len(not_launched)}个）──")
            for p in not_launched[:10]:
                lines.extend(format_product_detail(p, indent="    "))
            if len(not_launched) > 10:
                lines.append(f"    ... 还有{len(not_launched)-10}个未起量产品")

        # 模块差距矩阵
        comparison = analysis_result.get("comparison", {})
        module_comparison = comparison.get("module_comparison", [])
        if module_comparison:
            lines.append("")
            lines.append("  ── 逐模块差距分析 ──")
            lines.append(f"  {'模块':<10} {'我方状态':<12} {'差距':<6} {'感知':<6} {'优先级':<6} {'建议'}")
            lines.append(f"  {'-'*70}")
            for mc in module_comparison:
                lines.append(f"  {mc.get('module_name', ''):<10} {mc.get('our_status', ''):<12} "
                            f"{mc.get('gap_level', ''):<6} {mc.get('user_perception', ''):<6} "
                            f"{mc.get('upgrade_priority', ''):<6} {mc.get('suggestion', '')}")

        # 分析摘要
        summary = comparison.get("summary", {})
        if summary:
            lines.append("")
            lines.append("  ── 分析摘要 ──")
            if summary.get("launched_products_note"):
                lines.append(f"  {summary['launched_products_note']}")
            if summary.get("our_strengths"):
                lines.append("  我方优势:")
                for s in summary["our_strengths"]:
                    lines.append(f"    + {s}")
            if summary.get("our_weaknesses"):
                lines.append("  我方不足:")
                for w in summary["our_weaknesses"]:
                    lines.append(f"    - {w}")
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

        roadmap = comparison.get("upgrade_roadmap", [])
        if roadmap:
            lines.append("")
            lines.append("  ── 迭代路线图 ──")
            for r in roadmap:
                lines.append(f"    [{r.get('priority', '?')}] {r.get('module', '?')}: "
                            f"{r.get('action', '')} → {r.get('expected_impact', '')}")

        if score and score.suggestions:
            lines.append("")
            lines.append("  改进建议:")
            for s in score.suggestions:
                lines.append(f"    > {s}")

        return "\n".join(lines)
