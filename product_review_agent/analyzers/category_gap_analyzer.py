# -*- coding: utf-8 -*-
"""
🗺️ 品类地图缺失分析器

流程：
  1. 检查我方品牌在该品类下是否已有产品
  2. 如果有，列出已有产品+模块（属于品类补全，非全新空白）
  3. 如果没有，区分"完全品类缺失"和"半品类缺失"
     - 完全品类缺失：整个品类没有任何品牌有产品
     - 半品类缺失：别的品牌有产品，但我方品牌没有
  4. 获取品类市场概况（品牌分布、销量排行）
  5. 用VL模型拆解竞品/参考图片→模块清单
  6. 基于现有CBB模块库，给出模块组合建议（尽量复用）
  7. 评估市场空白风险和机会

量化打分（100分制）：
  - 市场机会 (35分): 品类缺失程度+竞品验证+市场容量
  - 模块复用度 (25分): 区分完全/半品类缺失，可复用模块比例
  - 进入门槛 (25分): 需新建模块数量+面料/版型权重+供应链难度
  - 价格竞争力 (15分): 我方定价与竞品定价的偏差率+毛利率
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


class CategoryGapAnalyzer(BaseAnalyzer):
    """🗺️ 品类地图缺失分析器"""

    analysis_type = "category_gap"
    display_name = "品类地图缺失"
    emoji = "🗺️"

    SCORING_DIMENSIONS = [
        ("市场机会", 35, "品类缺失程度+竞品验证+市场容量"),
        ("模块复用度", 25, "区分完全/半品类缺失，可复用模块比例"),
        ("进入门槛", 25, "需新建模块+面料/版型权重+供应链难度"),
        ("价格竞争力", 15, "我方定价与竞品偏差率+毛利率"),
    ]

    async def analyze(self, project_data: dict, images: list = None) -> dict:
        """品类地图缺失模块对比分析。"""
        llm = get_llm_client()
        category_l3 = project_data.get("category_l3") or project_data.get("categoryl3", "")
        category_l2 = project_data.get("category_l2") or project_data.get("categoryl2", "")
        category_l1 = project_data.get("category_l1") or project_data.get("categoryl1", "")
        brand = project_data.get("brand", "")
        competitor_name = project_data.get("competitor_name", "")
        product_name = project_data.get("product_name") or project_data.get("project_name", "")
        target_audience = project_data.get("used_people") or project_data.get("target_audience", "")
        used_scene = project_data.get("used_scene") or project_data.get("target_scene", "")

        # Step 1: 检查品类缺失
        gap_info = {
            "has_gap": True,
            "brand": brand,
            "category_l2": category_l2,
            "category_l3": category_l3,
            "gap_description": "",
        }
        existing_products = []
        nearby_products = []
        market_overview = {}
        category_sales_summary = {}

        try:
            with ProductQuery() as pq:
                gap_check = pq.check_category_gap(
                    category_l2=category_l2,
                    brand=brand,
                    category_l3=category_l3,
                )
                gap_info["has_gap"] = gap_check["has_gap"]
                gap_info["gap_description"] = gap_check["gap_description"]

                if not gap_check["has_gap"]:
                    existing_products = gap_check.get("existing_products", [])

                # 品类市场概况
                market_overview = pq.get_category_market_overview(
                    category_l2=category_l2,
                    category_l3=category_l3,
                )

                all_l2_products = pq.get_products_with_modules(
                    category_l2=category_l2,
                    brand=brand,
                )

                # 批量获取销量数据
                all_skus = [p.get("product_code", "") for p in all_l2_products]
                sales_data = pq.get_products_sales(all_skus)

                existing_codes = {p.get("product_code", "") for p in existing_products}
                nearby_products = [
                    p for p in all_l2_products
                    if p.get("product_code", "") not in existing_codes
                ]

                # 品类销量汇总
                if market_overview.get("brand_distribution"):
                    category_sales_summary = {
                        "total_products": market_overview["total_products"],
                        "total_sales": market_overview["total_category_sales"],
                        "brand_count": len(market_overview["brand_distribution"]),
                        "top_brand": market_overview["brand_distribution"][0]["brand"] if market_overview["brand_distribution"] else "",
                        "top_brand_sales": market_overview["brand_distribution"][0]["total_sales"] if market_overview["brand_distribution"] else 0,
                    }

        except Exception as e:
            logger.error(f"[品类缺失] 数据库查询异常: {e}")

        # Step 2: 汇总我方可复用模块
        seen_cbb = set()
        all_our_modules = []
        all_products_for_modules = existing_products + nearby_products

        for p in all_products_for_modules:
            for m in p.get("modules", []):
                cbb_code = m.get("cbb_code", "")
                if cbb_code and cbb_code not in seen_cbb:
                    seen_cbb.add(cbb_code)
                    all_our_modules.append(m)

        our_product_info = {
            "name": f"我方{category_l2}品类模块库",
            "category": f"{category_l2} > {category_l3}",
            "modules": all_our_modules,
            "is_module_library": True,
        }

        # Step 3: 用VL模型拆解参考/竞品图片
        competitor_info = {
            "name": competitor_name or "参考产品",
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
                    product_name=competitor_name or product_name,
                ),
            )
            if vision_result.get("modules"):
                competitor_info["modules"] = vision_result["modules"]
                competitor_info["_source"] = "vision"
                competitor_info["overall_description"] = vision_result.get("overall_description", "")

        # Step 4: 品类缺失专用对比
        comparison = await _category_gap_compare(
            llm,
            our_product_info,
            competitor_info,
            project_data,
            gap_info,
            all_products_for_modules,
            market_overview,
        )

        # Step 5: 立项产品模块
        project_modules = project_data.get("project_modules") or project_data.get("module_list")
        if isinstance(project_modules, str):
            try:
                project_modules = json.loads(project_modules)
            except (json.JSONDecodeError, TypeError):
                project_modules = None

        return {
            "analysis_type": "category_gap",
            "gap_info": gap_info,
            "existing_products": [
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
                for p in existing_products
            ],
            "nearby_category_products": [
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
                for p in nearby_products
            ],
            "market_overview": market_overview,
            "category_sales_summary": category_sales_summary,
            "our_product": our_product_info,
            "competitor_product": competitor_info,
            "comparison": comparison,
            "project_modules": project_modules,
        }

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """品类地图缺失量化打分。"""
        comparison = analysis_result.get("comparison", {})
        summary = comparison.get("summary", {})
        module_comparison = comparison.get("module_comparison", [])
        upgrade_roadmap = comparison.get("upgrade_roadmap", [])
        gap_info = analysis_result.get("gap_info", {})
        existing_products = analysis_result.get("existing_products", [])
        market_overview = analysis_result.get("market_overview", {})
        category_sales_summary = analysis_result.get("category_sales_summary", {})

        # ── 判断缺失类型 ──
        # full_gap:  完全品类缺失（整个品类没有任何品牌有产品）
        # half_gap:  半品类缺失（别的品牌有产品，但我方品牌没有）
        # no_gap:    同品牌下已有产品（品类补全）
        has_gap = gap_info.get("has_gap", True)
        brand_dist = market_overview.get("brand_distribution", [])
        other_brands_exist = len(brand_dist) > 0  # 品类下有其他品牌的产品

        if has_gap and not other_brands_exist:
            gap_type = "full_gap"
            gap_type_desc = "完全品类缺失"
        elif has_gap and other_brands_exist:
            gap_type = "half_gap"
            gap_type_desc = "半品类缺失"
        else:
            gap_type = "no_gap"
            gap_type_desc = "品类补全"

        # ── 维度1: 市场机会 (35分) ──
        if gap_type == "full_gap":
            market_score = 20
            market_reason = "完全品类空白，市场未被验证，机会与风险并存"
        elif gap_type == "half_gap":
            market_score = 25
            market_reason = f"半品类缺失，{len(brand_dist)}个品牌已有产品，市场已被验证"
        else:
            market_score = 10
            market_reason = f"品类补全，已有{len(existing_products)}个同品牌产品"

        # 有竞品参考说明市场已被验证
        competitor_modules = analysis_result.get("competitor_product", {}).get("modules", [])
        if competitor_modules:
            market_score = min(35, market_score + 5)
            market_reason += "，竞品验证市场存在"

        # 品类总销量越大市场越成熟
        total_sales = category_sales_summary.get("total_sales", 0)
        if total_sales >= 50000:
            market_score = min(35, market_score + 3)
            market_reason += f"，品类月均总销量{total_sales//6}+，市场成熟"
        elif total_sales >= 10000:
            market_score = min(35, market_score + 1)
            market_reason += "，品类有一定市场基础"

        # ── 维度2: 模块复用度 (25分) ──
        # 区分完全/半品类缺失，逻辑不同
        reuse_count = len(summary.get("reuse_modules", []))
        new_count = len(summary.get("new_modules_needed", []))
        total_ops = reuse_count + new_count

        if gap_type == "full_gap":
            # 完全品类缺失：之前从未做过这个产品
            if total_ops == 0:
                # 无任何模块数据
                if not competitor_modules:
                    # 完全找不到类似品，直接低分
                    reuse_score = 5
                    reuse_reason = "完全品类缺失，无类似品参考，模块复用极低"
                else:
                    # 有竞品拆解数据但无复用/新建分类，给中等偏低分
                    reuse_score = 10
                    reuse_reason = "完全品类缺失，有竞品参考但复用数据不完整"
            else:
                reuse_ratio = reuse_count / total_ops
                if reuse_ratio >= 0.5:
                    # 工厂有该品类的模块，可以复用
                    reuse_score = 18
                    reuse_reason = f"完全品类缺失，但工厂有可复用模块，复用{reuse_count}个/新建{new_count}个"
                else:
                    # 复用率低
                    reuse_score = max(3, int(25 * reuse_ratio * 0.6))
                    reuse_reason = f"完全品类缺失，复用率{reuse_ratio:.0%}，基础薄弱"

        elif gap_type == "half_gap":
            # 半品类缺失：公司其他品牌有产品，正常计算复用率
            if total_ops == 0:
                reuse_score = 12
                reuse_reason = "半品类缺失，无复用/新建模块数据"
            else:
                reuse_ratio = reuse_count / total_ops
                reuse_score = max(0, int(25 * reuse_ratio))
                reuse_reason = f"半品类缺失，可复用{reuse_count}个，需新建{new_count}个，复用率{reuse_ratio:.0%}"

        else:
            # 品类补全：同品牌下有产品，正常计算
            if total_ops == 0:
                reuse_score = 12
                reuse_reason = "无复用/新建模块数据"
            else:
                reuse_ratio = reuse_count / total_ops
                reuse_score = max(0, int(25 * reuse_ratio))
                reuse_reason = f"可复用{reuse_count}个，需新建{new_count}个，复用率{reuse_ratio:.0%}"

        # ── 维度3: 进入门槛 (25分) ──
        # 面料/版型模块权重更高（系数1.5），其他模块权重1.0
        FABRIC_PATTERN_KEYWORDS = {"面料", "版型", "布料", "材质", "织物", "内衬", "外层", "包布"}

        if total_ops > 0:
            # 区分模块类型计算加权新建占比
            weighted_new = 0.0
            weighted_total = 0.0
            new_modules = summary.get("new_modules_needed", [])
            reuse_modules = summary.get("reuse_modules", [])

            for m in new_modules:
                m_str = str(m).lower()
                is_fabric = any(kw in m_str for kw in FABRIC_PATTERN_KEYWORDS)
                weight = 1.5 if is_fabric else 1.0
                weighted_new += weight
                weighted_total += weight

            for m in reuse_modules:
                m_str = str(m).lower()
                is_fabric = any(kw in m_str for kw in FABRIC_PATTERN_KEYWORDS)
                weight = 1.5 if is_fabric else 1.0
                weighted_total += weight

            if weighted_total > 0:
                weighted_new_ratio = weighted_new / weighted_total
            else:
                weighted_new_ratio = new_count / total_ops

            entry_score = max(5, int(25 * (1 - weighted_new_ratio * 0.7)))
            entry_reason = f"需新建{new_count}项，复用{reuse_count}项，加权新建比{weighted_new_ratio:.0%}"
        else:
            entry_score = 10
            entry_reason = "无模块操作数据"

        # 特殊说明：工厂有相关产品可做
        factory_note = summary.get("factory_capability", "") or ""
        project_data_brand = analysis_result.get("gap_info", {}).get("brand", "")
        if factory_note or (gap_type == "full_gap" and reuse_count > 0):
            entry_score = min(25, entry_score + 3)
            entry_reason += "，工厂有相关产品可做"

        # ── 维度4: 价格竞争力 (15分) ──
        # 从项目数据中取定价和成本
        # 注意：analysis_result 不直接包含 project_data，需从 gap_info 或其他途径获取
        # 这里从 summary 中尝试获取（LLM 可能输出价格信息）
        pricing = summary.get("pricing", 0) or 0
        erp_price = summary.get("erp_price", 0) or 0
        competitor_price = summary.get("competitor_price", 0) or 0

        # 尝试从 module_comparison 中提取竞品价格信息
        if not competitor_price:
            for mc in module_comparison:
                cp = mc.get("competitor_price")
                if cp:
                    try:
                        competitor_price = float(str(cp).replace("元", "").replace("￥", "").strip())
                        break
                    except (ValueError, TypeError):
                        pass

        if pricing and competitor_price:
            # 两边都有价格，可以计算偏差率
            try:
                our_price = float(str(pricing).replace("元", "").replace("￥", "").strip())
                comp_price = float(str(competitor_price).replace("元", "").replace("￥", "").strip())

                if comp_price > 0:
                    deviation = (our_price - comp_price) / comp_price

                    # 基于偏差率评分
                    if -0.10 <= deviation <= 0.10:
                        price_score = 15  # 价格接近，竞争力强
                        price_reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，价格接近"
                    elif -0.20 <= deviation < -0.10:
                        price_score = 12  # 略低于竞品，性价比路线
                        price_reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，略低于竞品"
                    elif 0.10 < deviation <= 0.20:
                        price_score = 10  # 略高于竞品，需差异化支撑
                        price_reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，略高于竞品"
                    elif -0.30 <= deviation < -0.20:
                        price_score = 8  # 偏低较多
                        price_reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，偏低较多"
                    elif 0.20 < deviation <= 0.30:
                        price_score = 6  # 偏高较多
                        price_reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，偏高较多"
                    elif deviation > 0.30:
                        price_score = 3  # 过高
                        price_reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，价格过高"
                    else:
                        price_score = 5  # 过低
                        price_reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，价格过低"

                    # 毛利空间修正
                    if erp_price and our_price > 0:
                        try:
                            erp_val = float(str(erp_price).replace("元", "").replace("￥", "").strip())
                            margin = (our_price - erp_val) / our_price
                            if margin < 0.20:
                                price_score = max(3, price_score - 3)
                                price_reason += f"，毛利率{margin:.0%}偏低"
                            elif margin < 0.40:
                                price_score = max(3, price_score - 1)
                                price_reason += f"，毛利率{margin:.0%}偏紧"
                        except (ValueError, TypeError):
                            pass
                else:
                    price_score = 8
                    price_reason = "竞品价格数据异常"

            except (ValueError, TypeError):
                price_score = 8
                price_reason = "价格数据格式异常"

        elif pricing and not competitor_price:
            # 有我方定价，无竞品价格
            price_score = 8
            price_reason = f"有定价{pricing}，但无竞品价格参考"
        elif not pricing and competitor_price:
            # 有竞品价格，无我方定价
            price_score = 6
            price_reason = f"竞品价格{competitor_price}，但我方未定价"
        else:
            # 两者都没有
            price_score = 5
            price_reason = "价格信息缺失，无法评估"

        # ── 汇总 ──
        dimensions = [
            DimensionScore("市场机会", market_score, 35, market_reason),
            DimensionScore("模块复用度", reuse_score, 25, reuse_reason),
            DimensionScore("进入门槛", entry_score, 25, entry_reason),
            DimensionScore("价格竞争力", price_score, 15, price_reason),
        ]

        total = sum(d.score for d in dimensions)
        strengths = summary.get("our_strengths", [])
        weaknesses = summary.get("our_weaknesses", [])

        suggestions = []
        if market_score < 20:
            suggestions.append("市场机会偏小，建议重新评估品类进入的必要性")
        if reuse_score < 10:
            if gap_type == "full_gap":
                suggestions.append("完全品类缺失且复用基础薄弱，建议先小批量试产验证供应链能力")
            else:
                suggestions.append("模块复用率偏低，建议优先评估现有CBB模块的适配可能性")
        if entry_score < 15:
            suggestions.append("进入门槛较高，建议分阶段进入：先复用再创新")
        if price_score < 8:
            suggestions.append("价格竞争力不足，建议调整定价策略或增加差异化卖点支撑溢价")
        if has_gap and total_sales > 0:
            suggestions.append(f"品类已有{total_sales}总销量验证，建议参考头部产品模块组合快速切入")

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
        """格式化品类地图缺失分析结果"""
        if not analysis_result:
            return "  (品类地图缺失分析不可用)"

        lines = []
        lines.append("[🗺️ 品类地图缺失分析]")

        # 评分总览
        if score:
            lines.append(f"  评分: {score.total_score}/100 {'★' * score.star_rating}{'☆' * (5 - score.star_rating)} 风险: {score.risk_level}")
            for d in score.dimensions:
                lines.append(f"    {d.name}: {d.score}/{d.max_score} - {d.reason}")
            lines.append("")

        # 品类缺失信息
        gap_info = analysis_result.get("gap_info", {})
        # 判断缺失类型
        market_overview_rpt = analysis_result.get("market_overview", {})
        brand_dist_rpt = market_overview_rpt.get("brand_distribution", [])
        has_gap_rpt = gap_info.get("has_gap", True)
        other_brands_rpt = len(brand_dist_rpt) > 0

        if has_gap_rpt and not other_brands_rpt:
            gap_type = "🆕 完全品类缺失（品类下无任何品牌产品）"
        elif has_gap_rpt and other_brands_rpt:
            gap_type = "🔲 半品类缺失（其他品牌有，我方品牌没有）"
        else:
            gap_type = "📦 品类补全（同品牌下已有产品）"
        lines.append(f"  缺失类型: {gap_type}")
        lines.append(f"  {gap_info.get('gap_description', '')}")

        # 品类市场概况
        market_overview = analysis_result.get("market_overview", {})
        if market_overview:
            lines.append("")
            lines.append("  ── 品类市场概况 ──")
            total_products = market_overview.get("total_products", 0)
            total_sales = market_overview.get("total_category_sales", 0)
            brand_dist = market_overview.get("brand_distribution", [])
            lines.append(f"  品类产品总数: {total_products}个")
            lines.append(f"  品类累计总销量: {total_sales:,}")
            if brand_dist:
                lines.append(f"  品牌分布:")
                for bd in brand_dist[:8]:
                    pct = (bd["total_sales"] / total_sales * 100) if total_sales > 0 else 0
                    lines.append(f"    • {bd['brand']}: {bd['product_count']}个产品, 累计销量{bd['total_sales']:,} ({pct:.1f}%)")

            top_selling = market_overview.get("top_selling_products", [])
            if top_selling:
                lines.append(f"  品类销量TOP产品:")
                for ts in top_selling[:5]:
                    lines.append(f"    🔥 {ts['product_code']} ({ts['brand']}) - {ts['category_l3']} | 月最高{ts['max_sales']:,}")

        # 已有产品
        existing = analysis_result.get("existing_products", [])
        nearby = analysis_result.get("nearby_category_products", [])

        if existing:
            lines.append("")
            lines.append(f"  ── 我方已有同品类产品 ({len(existing)}个) ──")
            for p in existing[:5]:
                lines.extend(format_product_detail(p, indent="    "))
            if len(existing) > 5:
                lines.append(f"    ... 还有{len(existing)-5}个产品")

        if nearby:
            lines.append("")
            lines.append(f"  ── 同二级品类其他产品 ({len(nearby)}个) ──")
            for p in nearby[:5]:
                lines.extend(format_product_detail(p, indent="    "))
            if len(nearby) > 5:
                lines.append(f"    ... 还有{len(nearby)-5}个产品")

        # 模块对比详情
        comparison = analysis_result.get("comparison", {})
        module_comparison = comparison.get("module_comparison", [])
        if module_comparison:
            lines.append("")
            lines.append("  ── 逐模块差距分析 ──")
            lines.append(f"  {'模块':<10} {'我方状态':<12} {'竞品/参考':<16} {'差距':<6} {'感知':<6} {'优先级':<6} {'建议'}")
            lines.append(f"  {'-'*80}")
            for mc in module_comparison:
                lines.append(f"  {mc.get('module_name', ''):<10} {mc.get('our_status', ''):<12} "
                            f"{mc.get('competitor_status', ''):<16} {mc.get('gap_level', ''):<6} "
                            f"{mc.get('user_perception', ''):<6} {mc.get('upgrade_priority', ''):<6} {mc.get('suggestion', '')}")

        # 对比结果摘要
        summary = comparison.get("summary", {})
        if summary:
            lines.append("")
            lines.append("  ── 分析摘要 ──")
            if summary.get("market_opportunity"):
                lines.append(f"  市场机会: {summary['market_opportunity']}")
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
            if summary.get("price_competitiveness"):
                lines.append(f"  价格竞争力: {summary['price_competitiveness']}")
            if summary.get("overall_assessment"):
                lines.append(f"  整体评价: {summary['overall_assessment']}")

        # 模块路线图
        roadmap = comparison.get("upgrade_roadmap", [])
        if roadmap:
            lines.append("")
            lines.append("  ── 模块组合路线图 ──")
            for r in roadmap:
                source = r.get("source", "")
                source_icon = {"复用": "✅", "改造": "🔧", "新建": "🆕"}.get(source, "")
                lines.append(f"    [{r.get('priority', '?')}] {r.get('module', '?')}: "
                            f"{r.get('action', '')} {source_icon}{source} → {r.get('expected_impact', '')}")

        # 我方可复用模块库汇总
        our_product = analysis_result.get("our_product", {})
        our_modules = our_product.get("modules", [])
        if our_modules:
            lines.append("")
            lines.append(f"  ── 我方现有可复用模块库 ({len(our_modules)}个) ──")
            # 按category分组
            by_category = {}
            for m in our_modules:
                cat = m.get("category", "未分类")
                if cat not in by_category:
                    by_category[cat] = []
                by_category[cat].append(m)
            for cat, mods in by_category.items():
                lines.append(f"  [{cat}]")
                for m in mods:
                    cbb_code = m.get("cbb_code", "")
                    cbb_name = m.get("cbb_name", "")
                    sub_type = m.get("sub_type", "")
                    lines.append(f"    • {cbb_code} {cbb_name} ({sub_type})")

        if score and score.suggestions:
            lines.append("")
            lines.append("  改进建议:")
            for s in score.suggestions:
                lines.append(f"    > {s}")

        return "\n".join(lines)


async def _category_gap_compare(
    llm: LLMClient,
    our_product: dict,
    competitor_product: dict,
    project_data: dict,
    gap_info: dict,
    all_products: list[dict],
    market_overview: dict = None,
) -> dict:
    """品类缺失专用对比，重点输出模块组合方案和风险评估。"""
    if not llm.is_available:
        return {
            "module_comparison": [],
            "summary": {
                "our_strengths": [],
                "our_weaknesses": [],
                "reuse_modules": [],
                "new_modules_needed": [],
                "overall_assessment": "LLM不可用，无法进行品类缺失分析",
            },
            "upgrade_roadmap": [],
            "_error": "LLM不可用",
        }

    our_table = build_module_table(our_product.get("modules", []))
    competitor_table = build_module_table(competitor_product.get("modules", []))

    category_l2 = project_data.get("category_l2") or project_data.get("categoryl2", "")
    category_l3 = project_data.get("category_l3") or project_data.get("categoryl3", "")
    brand = project_data.get("brand", "")
    target_audience = project_data.get("used_people") or project_data.get("target_audience", "")
    used_scene = project_data.get("used_scene") or project_data.get("target_scene", "")

    gap_desc = gap_info.get("gap_description", "")
    is_pure_gap = gap_info.get("has_gap", True)

    products_summary = ""
    if all_products:
        products_summary = f"同二级品类({category_l2})下的现有产品:\n"
        for p in all_products[:10]:
            code = p.get("product_code", "?")
            pbrand = p.get("brand", "?")
            cat3 = p.get("category_l3", "")
            mod_count = len(p.get("modules", []))
            products_summary += f"  - {code} ({pbrand}, {cat3}), 模块数{mod_count}\n"
    else:
        products_summary = "该二级品类下无现有产品。"

    # 市场概况信息
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

    gap_type = "全新品类空白" if is_pure_gap else "品类补全（已有同品类产品）"

    prompt = f"""【任务】深入分析品类地图空白，输出详细的模块组合方案、市场竞争分析和风险评估。
【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。

== 品类缺失情况 ==
品牌: {brand}
品类: {category_l2} > {category_l3}
缺失类型: {gap_type}
描述: {gap_desc}

== 目标人群 ==
{target_audience or '未指定'}

== 使用场景 ==
{used_scene or '未指定'}
{market_section}
== 我方现有模块库 ==
{our_table}

{products_summary}

== 参考产品/竞品模块（来自图片拆解） ==
{competitor_table}

== 立项产品模块信息 ==
{project_info}

【分析要求】
1. 市场空白评估：该品类是否真的存在市场机会？结合品类市场概况数据（品牌分布、销量数据）给出判断
2. 竞争格局分析：品类内有哪些主要品牌？它们的份额如何？我方进入的竞争压力多大？
3. 模块组合方案：基于我方现有模块库，如何组合出新品？哪些可直接复用，哪些需新建？每个模块给出详细建议
4. 竞品对标：参考产品有哪些模块设计值得学习？具体分析每个模块的优劣
5. 风险评估：进入新品类的风险（市场接受度、供应链、竞争格局、渠道能力）
6. 优先级排序：模块开发/采购的优先级，并给出时间线建议

【输出格式】严格按此JSON结构输出（module_comparison至少4条，summary各字段2-4条且内容详细，upgrade_roadmap至少3条）：
{{
    "module_comparison": [
        {{
            "module_name": "绑带",
            "our_status": "已有可复用",
            "competitor_status": "参考产品采用X型交叉",
            "gap_level": "低",
            "user_perception": "高",
            "upgrade_priority": "P2",
            "suggestion": "直接复用现有双绑带设计",
            "source": "复用自XX产品"
        }},
        {{
            "module_name": "减震垫",
            "our_status": "需新建",
            "competitor_status": "加厚半透硅胶垫",
            "gap_level": "高",
            "user_perception": "高",
            "upgrade_priority": "P0",
            "suggestion": "新建硅胶垫模块",
            "source": "全新开发"
        }}
    ],
    "summary": {{
        "market_opportunity": "市场机会评估（150-300字，结合品类数据给出具体分析）",
        "competitive_landscape": "竞争格局分析（150-300字，各品牌份额和竞争策略）",
        "our_strengths": ["我方优势1（具体说明）", "我方优势2", "我方优势3"],
        "our_weaknesses": ["我方不足1（具体说明）", "我方不足2", "我方不足3"],
        "reuse_modules": ["可复用模块1 (来源产品货号)", "可复用模块2 (来源产品货号)"],
        "new_modules_needed": ["需新建模块1 (具体说明需要什么)", "需新建模块2"],
        "risk_factors": ["风险1（具体说明影响）", "风险2", "风险3"],
        "entry_strategy": "进入策略建议（100-200字，分阶段建议）",
        "overall_assessment": "整体评价（150-300字，综合市场、竞争、技术三维度给出结论）"
    }},
    "upgrade_roadmap": [
        {{
            "priority": "P0",
            "module": "模块名",
            "action": "具体动作",
            "source": "复用/改造/新建",
            "expected_impact": "预期效果",
            "timeline": "建议时间线"
        }}
    ]
}}"""

    original_max_tokens = llm.max_tokens
    llm.max_tokens = 8192

    try:
        result = await llm.acall_text(
            [
                {"role": "system", "content": "你是品类拓展专家，擅长市场分析、竞争格局评估和模块化产品策略。严格只返回JSON对象，禁止输出思考过程、解释文字或markdown代码块。分析必须基于提供的数据，给出具体可执行的结论。"},
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
                    "overall_assessment": "品类分析返回格式异常",
                },
                "upgrade_roadmap": [],
                "_error": "返回格式异常",
            }

    except Exception as e:
        logger.error(f"[品类缺失对比] 异常: {e}")
        return {
            "module_comparison": [],
            "summary": {
                "our_strengths": [],
                "our_weaknesses": [],
                "reuse_modules": [],
                "new_modules_needed": [],
                "overall_assessment": f"品类分析异常: {e}",
            },
            "upgrade_roadmap": [],
            "_error": str(e),
        }

    finally:
        llm.max_tokens = original_max_tokens
