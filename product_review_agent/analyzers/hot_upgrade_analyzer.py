# -*- coding: utf-8 -*-
"""
🔥 爆品升级分析器

流程：
  1. 根据二级品类检索爆品（任一月销量>2000）
  2. 获取爆品模块（数据库检索）
  3. 获取品类市场概况（品牌分布、销量排行）
  4. 用VL模型拆解爆品图片（补充模块信息，数据库可能不全）
  5. 用VL模型拆解竞品图片
  6. 对比爆品模块 vs 竞品模块 → 差距矩阵 + 升级建议

量化打分（100分制）：
  - 模块差距 (30分): 差距越小分越高
  - 升级可行性 (25分): P0越少越聚焦，可行性越高
  - 复用基础 (25分): 可复用模块越多分越高
  - 市场匹配度 (20分): 爆品销量越高+差距可控，市场匹配越好
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

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
    calc_summary_quality_score,
    format_product_detail,
)

logger = logging.getLogger(__name__)


class HotUpgradeAnalyzer(BaseAnalyzer):
    """🔥 爆品升级分析器"""

    analysis_type = "hot_upgrade"
    display_name = "爆品升级"
    emoji = "🔥"

    SCORING_DIMENSIONS = [
        ("模块差距", 30, "差距越小分越高"),
        ("升级可行性", 25, "P0越少越聚焦"),
        ("复用基础", 25, "可复用模块越多越好"),
        ("市场匹配度", 20, "爆品销量+差距可控"),
    ]

    async def analyze(self, project_data: dict, images: list = None) -> dict:
        """爆品升级模块对比分析。"""
        llm = get_llm_client()
        category_l3 = project_data.get("category_l3") or project_data.get("categoryl3", "")
        category_l2 = project_data.get("category_l2") or project_data.get("categoryl2", "")
        category_l1 = project_data.get("category_l1") or project_data.get("categoryl1", "")
        brand = project_data.get("brand", "")
        competitor_name = project_data.get("competitor_name", "")
        product_name = project_data.get("product_name") or project_data.get("project_name", "")

        # Step 1: 检索爆品
        hot_products = []
        our_product_info = {"name": "我方爆品", "category": f"{category_l2} > {category_l3}", "modules": []}
        market_overview = {}
        all_products_with_modules = []

        try:
            with ProductQuery() as pq:
                hot_products = pq.find_hot_products(
                    category_l3=category_l3,
                    category_l2=category_l2,
                    brand=brand,
                )

                # 批量获取销量数据
                all_skus = [p.get("product_code", "") for p in hot_products]
                sales_data = pq.get_products_sales(all_skus)

                # 品类市场概况
                market_overview = pq.get_category_market_overview(
                    category_l2=category_l2,
                    category_l3=category_l3,
                )

                if hot_products:
                    top_hot = hot_products[0]
                    our_product_info["name"] = f"{top_hot['product_code']} ({top_hot['brand']})"
                    our_product_info["modules"] = top_hot.get("modules", [])

                    if not our_product_info["modules"] and top_hot.get("image_url"):
                        image_path = top_hot["image_url"]
                        if os.path.exists(image_path):
                            with open(image_path, "rb") as f:
                                img_data = f.read()
                            if llm.is_available:
                                vision_result = await vision_decompose(
                                    llm, img_data,
                                    category_info=ProductCategoryInfo(
                                        category_l1=category_l1,
                                        category_l2=category_l2,
                                        category_l3=category_l3,
                                        product_name=top_hot["product_code"],
                                    ),
                                )
                                if vision_result.get("modules"):
                                    our_product_info["modules"] = vision_result["modules"]
                                    our_product_info["_source"] = "vision"
                else:
                    all_products_with_modules = pq.get_products_with_modules(
                        category_l2=category_l2,
                        brand=brand,
                    )
                    # 无爆品时也获取销量
                    all_skus = [p.get("product_code", "") for p in all_products_with_modules]
                    sales_data = pq.get_products_sales(all_skus)

                    if all_products_with_modules:
                        top_product = all_products_with_modules[0]
                        our_product_info["name"] = f"{top_product['product_code']} ({top_product['brand']})"
                        our_product_info["modules"] = top_product.get("modules", [])

        except Exception as e:
            logger.error(f"[爆品升级] 数据库查询异常: {e}")

        # Step 2: 拆解竞品模块
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

        # Step 3: 立项产品的模块信息
        project_modules = project_data.get("project_modules") or project_data.get("module_list")
        if isinstance(project_modules, str):
            try:
                project_modules = json.loads(project_modules)
            except (json.JSONDecodeError, TypeError):
                project_modules = None

        # Step 4: 对比
        comparison = await compare_module_sets(
            llm, our_product_info, competitor_info, project_modules
        )

        return {
            "analysis_type": "hot_upgrade",
            "hot_products": [
                {
                    "product_code": p.get("product_code", ""),
                    "sku": p.get("sku", p.get("product_code", "")),
                    "brand": p.get("brand", ""),
                    "category_l2": p.get("category_l2", category_l2),
                    "category_l3": p.get("category_l3", ""),
                    "max_sales": p.get("max_sales", 0),
                    "avg_sales": p.get("avg_sales", 0),
                    "hot_months": p.get("hot_months", []),
                    "sales_data": sales_data.get(p.get("product_code", ""), []),
                    "module_count": len(p.get("modules", [])),
                    "modules": p.get("modules", []),
                }
                for p in hot_products
            ],
            "market_overview": market_overview,
            "our_product": our_product_info,
            "competitor_product": competitor_info,
            "comparison": comparison,
            "project_modules": project_modules,
        }

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """爆品升级量化打分。"""
        comparison = analysis_result.get("comparison", {})
        summary = comparison.get("summary", {})
        module_comparison = comparison.get("module_comparison", [])
        upgrade_roadmap = comparison.get("upgrade_roadmap", [])
        hot_products = analysis_result.get("hot_products", [])

        # 维度1: 模块差距 (30分)
        gap_score, gap_reason = calc_gap_score(module_comparison, max_score=30)

        # 维度2: 升级可行性 (25分)
        feasibility_score, feasibility_reason = calc_roadmap_score(upgrade_roadmap, max_score=25)

        # 维度3: 复用基础 (25分)
        reuse_score, reuse_reason = calc_reuse_score(summary, max_score=25)

        # 维度4: 市场匹配度 (20分)
        market_score = 10  # 基础分
        market_reason = ""
        if hot_products:
            top_sales = hot_products[0].get("avg_sales", 0)
            if top_sales >= 5000:
                market_score = 18
                market_reason = f"头部爆品月均销量{top_sales:.0f}，市场验证充分"
            elif top_sales >= 2000:
                market_score = 14
                market_reason = f"头部爆品月均销量{top_sales:.0f}，市场基础良好"
            else:
                market_score = 10
                market_reason = f"爆品月均销量{top_sales:.0f}，市场验证一般"
        else:
            market_score = 6
            market_reason = "未检索到爆品，市场匹配度不确定"

        # 市场概况加持
        market_overview = analysis_result.get("market_overview", {})
        total_sales = market_overview.get("total_category_sales", 0)
        if total_sales >= 50000:
            market_score = min(20, market_score + 2)
            market_reason += "，品类市场成熟"

        dimensions = [
            DimensionScore("模块差距", gap_score, 30, gap_reason),
            DimensionScore("升级可行性", feasibility_score, 25, feasibility_reason),
            DimensionScore("复用基础", reuse_score, 25, reuse_reason),
            DimensionScore("市场匹配度", market_score, 20, market_reason),
        ]

        total = sum(d.score for d in dimensions)
        strengths = summary.get("our_strengths", [])
        weaknesses = summary.get("our_weaknesses", [])

        # 自动生成建议
        suggestions = []
        if gap_score < 20:
            suggestions.append("模块差距较大，建议优先聚焦1-2个核心模块升级")
        if reuse_score < 15:
            suggestions.append("复用基础薄弱，需评估新建模块的供应链风险")
        if feasibility_score < 15:
            suggestions.append("升级路线P0项过多，建议分批迭代而非一次性大改")
        if hot_products and hot_products[0].get("avg_sales", 0) > 3000:
            suggestions.append(f"爆品月均{hot_products[0]['avg_sales']:.0f}销量验证强，建议快速迭代抢占市场窗口")

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
        """格式化爆品升级分析结果"""
        if not analysis_result:
            return "  (爆品升级分析不可用)"

        lines = []
        lines.append("[🔥 爆品升级分析]")

        # 评分总览
        if score:
            lines.append(f"  评分: {score.total_score}/100 {_stars(score.star_rating)} 风险: {score.risk_level}")
            for d in score.dimensions:
                lines.append(f"    {d.name}: {d.score}/{d.max_score} - {d.reason}")
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
            top_selling = market_overview.get("top_selling_products", [])
            if top_selling:
                lines.append(f"  品类销量TOP:")
                for ts in top_selling[:5]:
                    lines.append(f"    🔥 {ts['product_code']} ({ts['brand']}) - {ts['category_l3']} | 月最高{ts['max_sales']:,}")
            lines.append("")

        # 爆品信息（含货号+销量+模块明细）
        hot_products = analysis_result.get("hot_products", [])
        if hot_products:
            lines.append(f"  ── 检索到 {len(hot_products)} 个爆品（任一月销量>2000） ──")
            for hp in hot_products[:5]:
                lines.extend(format_product_detail(hp, indent="    "))
            if len(hot_products) > 5:
                lines.append(f"    ... 还有{len(hot_products)-5}个爆品")
        else:
            lines.append("  未检索到爆品（任一月销量>2000）")

        # 模块差距矩阵
        comparison = analysis_result.get("comparison", {})
        module_comparison = comparison.get("module_comparison", [])
        if module_comparison:
            lines.append("")
            lines.append("  ── 逐模块差距矩阵 ──")
            lines.append(f"  {'模块':<10} {'我方状态':<12} {'差距':<6} {'感知':<6} {'优先级':<6} {'建议'}")
            lines.append(f"  {'-'*70}")
            for mc in module_comparison:
                lines.append(f"  {mc.get('module_name', ''):<10} {mc.get('our_status', ''):<12} "
                            f"{mc.get('gap_level', ''):<6} {mc.get('user_perception', ''):<6} "
                            f"{mc.get('upgrade_priority', ''):<6} {mc.get('suggestion', '')}")

        # 对比结果摘要
        summary = comparison.get("summary", {})
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
                lines.append(f"    [{r.get('priority', '?')}] {r.get('module', '?')}: "
                            f"{r.get('action', '')} → {r.get('expected_impact', '')}")

        # 改进建议
        if score and score.suggestions:
            lines.append("")
            lines.append("  改进建议:")
            for s in score.suggestions:
                lines.append(f"    > {s}")

        return "\n".join(lines)


def _stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)
