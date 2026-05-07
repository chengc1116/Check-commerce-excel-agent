# -*- coding: utf-8 -*-
"""
🗺️ 品类地图缺失分析器（V2重构）

两种场景：
  A. 品牌缺失：公司有该品类产品，但立项品牌下没有 → VL对比（自家其他品牌 vs 竞品）
  B. 品类缺失：公司完全没有该品类产品 → VL单拆（仅竞品），CBB库做间接复用评估

流程：
  1. 数据库检索 → 判断场景A/B
  2. 图片检索（自家产品图 + 竞品图）
  3. VL拆解（compare/single）
  4. GLM专项推理（模块组合+人群场景+风险评估）
  5. 量化评分

量化打分（100分制）：
  - 模块复用度 (40分): 面料/版型权重1.5x，相似=高分，相同极罕见
  - 市场机会评估 (20分): 品类市场容量+竞品验证度+品牌空缺程度
  - 价格竞争力 (20分): 定价偏差率+毛利率
  - 进入门槛 (20分): 面料/版型壁垒+供应链+是否跨品类
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from product_review_agent.agents.llm_client import LLMClient, get_llm_client
from product_review_agent.product_db.product_query import ProductQuery
from product_review_agent.vl_module_splitter import (
    ModuleSplitter,
    find_product_images,
)
from product_review_agent.analyzers.base import (
    BaseAnalyzer,
    AnalyzerScore,
    DimensionScore,
    format_product_detail,
)

logger = logging.getLogger(__name__)


# ============================================================
# 模块复用度评分表（面料版型加权）
# ============================================================

# 模块类型 → 权重 + 各匹配级别的得分
MODULE_REUSE_TABLE = {
    # (权重, 完全相同, 相似, 需改造, 需新建)
    "面料/材质": (1.5, 15, 13, 7, 3),
    "版型/结构": (1.5, 15, 13, 7, 3),
    "功能组件":   (1.0, 15, 11, 6, 3),
    "外观/配色":  (0.8, 15, 13, 8, 5),
    "配件/辅材":  (0.5, 15, 11, 6, 5),
}

# 面料/版型相关关键词 → 映射到模块类型
FABRIC_KEYWORDS = {"面料", "布料", "材质", "织物", "内衬", "外层", "包布", "针织", "网布", "弹力布", "面料组合"}
PATTERN_KEYWORDS = {"版型", "结构", "形态", "裁剪", "套筒", "包裹", "支撑结构", "骨架"}
APPEARANCE_KEYWORDS = {"配色", "颜色", "外观", "图案", "印花", "色系", "色彩"}
ACCESSORY_KEYWORDS = {"配件", "辅材", "绑带", "扣件", "魔术贴", "拉链", "搭扣", "织带", "标签", "包装"}


def _classify_module_type(module_name: str) -> str:
    """根据模块名称判断模块类型（用于加权复用评分）"""
    name_lower = module_name.lower()
    if any(kw in name_lower for kw in FABRIC_KEYWORDS):
        return "面料/材质"
    if any(kw in name_lower for kw in PATTERN_KEYWORDS):
        return "版型/结构"
    if any(kw in name_lower for kw in APPEARANCE_KEYWORDS):
        return "外观/配色"
    if any(kw in name_lower for kw in ACCESSORY_KEYWORDS):
        return "配件/辅材"
    return "功能组件"


def _match_level_to_index(match_level: str) -> int:
    """匹配级别 → 得分索引: same=0, similar=1, adapt=2, new=3"""
    mapping = {
        "相同": 0, "same": 0,
        "相似": 1, "similar": 1,
        "需改造": 2, "adapt": 2, "改造": 2,
        "需新建": 3, "new": 3, "新建": 3,
    }
    return mapping.get(match_level, 2)


class CategoryGapAnalyzer(BaseAnalyzer):
    """🗺️ 品类地图缺失分析器（V2）"""

    analysis_type = "category_gap"
    display_name = "品类地图缺失"
    emoji = "🗺️"

    SCORING_DIMENSIONS = [
        ("模块复用度", 40, "面料/版型权重1.5x，相似=高分"),
        ("市场机会评估", 20, "品类市场容量+竞品验证度+品牌空缺程度"),
        ("价格竞争力", 20, "定价偏差率+毛利率"),
        ("进入门槛", 20, "面料/版型壁垒+供应链+是否跨品类"),
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
        upgrade_direction = project_data.get("upgrade_direction") or project_data.get("product_feature", "")

        # Step 1: 数据库检索 → 判断场景
        gap_info = {"has_gap": True, "brand": brand, "category_l2": category_l2,
                    "category_l3": category_l3, "gap_description": "", "gap_type": ""}
        same_brand_products = []   # 同品牌同品类产品
        other_brand_products = []  # 其他自有品牌同品类产品
        nearby_products = []       # 同二级品类所有产品
        market_overview = {}
        category_sales_summary = {}
        all_cbb_modules = []       # CBB模块库（用于场景B间接匹配）

        try:
            with ProductQuery() as pq:
                own_brands = pq.get_own_brands()
                _, is_competitor = pq.resolve_brand(brand)

                # 查询同品牌同品类产品
                same_brand_products = pq.get_products_with_modules(
                    category_l2=category_l2, category_l3=category_l3, brand=brand,
                )

                # 查询其他自有品牌同品类产品
                if not is_competitor:
                    for ob in own_brands:
                        if ob == brand:
                            continue
                        prods = pq.get_products_with_modules(
                            category_l2=category_l2, category_l3=category_l3, brand=ob,
                        )
                        other_brand_products.extend(prods)
                else:
                    # 项目书品牌是竞品品牌，查所有自有品牌
                    for ob in own_brands:
                        prods = pq.get_products_with_modules(
                            category_l2=category_l2, category_l3=category_l3, brand=ob,
                        )
                        other_brand_products.extend(prods)

                # 场景判断
                if same_brand_products:
                    gap_info["has_gap"] = False
                    gap_info["gap_type"] = "no_gap"
                    gap_info["gap_description"] = (
                        f"品牌「{brand}」在品类「{category_l3 or category_l2}」下"
                        f"已有 {len(same_brand_products)} 个产品"
                    )
                elif other_brand_products:
                    gap_info["has_gap"] = True
                    gap_info["gap_type"] = "brand_gap"
                    other_names = ", ".join(set(p.get("brand", "") for p in other_brand_products))
                    gap_info["gap_description"] = (
                        f"品牌「{brand}」在品类「{category_l3 or category_l2}」下无产品，"
                        f"但自有品牌「{other_names}」有 {len(other_brand_products)} 个产品"
                    )
                else:
                    gap_info["has_gap"] = True
                    gap_info["gap_type"] = "category_gap"
                    gap_info["gap_description"] = (
                        f"公司所有品牌在品类「{category_l3 or category_l2}」下均无产品，属于全新品类缺失"
                    )

                # 品类市场概况
                market_overview = pq.get_category_market_overview(
                    category_l2=category_l2, category_l3=category_l3,
                )

                # 同二级品类所有产品
                nearby_products = pq.get_products_with_modules(category_l2=category_l2)

                # 批量获取销量数据
                all_skus = [p.get("product_code", "") for p in same_brand_products + other_brand_products]
                sales_data = pq.get_products_sales(all_skus)

                # 品类销量汇总
                if market_overview.get("brand_distribution"):
                    category_sales_summary = {
                        "total_products": market_overview["total_products"],
                        "total_sales": market_overview["total_category_sales"],
                        "brand_count": len(market_overview["brand_distribution"]),
                        "top_brand": market_overview["brand_distribution"][0]["brand"] if market_overview["brand_distribution"] else "",
                        "top_brand_sales": market_overview["brand_distribution"][0]["total_sales"] if market_overview["brand_distribution"] else 0,
                    }

                # CBB模块库（场景B间接匹配用）
                all_cbb_modules = pq.get_all_cbb_modules(category_l2=category_l2)

        except Exception as e:
            logger.error(f"[品类缺失] 数据库查询异常: {e}")

        # Step 2: 图片检索
        own_images_bytes = []  # 自家产品图片bytes
        own_product_code = ""  # 用于VL参照的自家产品货号
        competitor_images_bytes = []  # 竞品图片bytes

        # 竞品图片：来自Excel提取
        if images:
            competitor_images_bytes = list(images)

        # 自家产品图片：场景A从其他品牌产品检索
        gap_type = gap_info.get("gap_type", "category_gap")
        reference_products = other_brand_products if gap_type == "brand_gap" else []

        if reference_products:
            # 取销量最高的产品作为参照
            ref_sorted = sorted(
                reference_products,
                key=lambda p: max((s.get("sales_volume", 0) for s in sales_data.get(p.get("product_code", ""), [])), default=0),
                reverse=True,
            )
            for ref_p in ref_sorted[:3]:
                ref_code = ref_p.get("product_code", "")
                ref_imgs = find_product_images(ref_code)
                if ref_imgs:
                    own_images_bytes = [img[1] for img in ref_imgs[:2]]  # 最多2张
                    own_product_code = ref_code
                    logger.info(f"[品类缺失] 找到参照产品图片: {ref_code} ({len(own_images_bytes)}张)")
                    break

        # Step 3: VL拆解
        splitter = ModuleSplitter()
        vl_report = {}
        category_info = {
            "category1": category_l1,
            "category2": category_l2,
            "category3": category_l3,
            "brand": brand,
        }

        if gap_type == "brand_gap" and own_images_bytes and competitor_images_bytes:
            # 场景A: 有自家产品图 + 竞品图 → 对比拆解
            logger.info(f"[品类缺失] 场景A(品牌缺失): VL对比拆解 自家{own_product_code} vs 竞品")
            vl_report = await splitter.analyze_compare(
                own_images=own_images_bytes,
                competitor_images=competitor_images_bytes,
                product_code=own_product_code,
                category_info=category_info,
                competitor_desc=competitor_name or "竞品",
                upgrade_direction=upgrade_direction,
                project_data=project_data,
            )
        elif competitor_images_bytes:
            # 场景B: 只有竞品图 → 单商品拆解
            logger.info(f"[品类缺失] 场景B(品类缺失): VL单拆竞品")
            vl_report = await splitter.analyze_single(
                images=competitor_images_bytes,
                product_code=competitor_name or "参考产品",
                category_info={
                    **category_info,
                    "brand": "竞品",
                },
            )
        else:
            logger.warning("[品类缺失] 无可用图片，跳过VL拆解")

        # Step 4: GLM专项推理
        comparison = await _category_gap_analysis(
            llm,
            vl_report,
            project_data,
            gap_info,
            same_brand_products,
            other_brand_products,
            nearby_products,
            market_overview,
            all_cbb_modules,
        )

        # Step 5: 组装结果
        return {
            "analysis_type": "category_gap",
            "gap_info": gap_info,
            "gap_type": gap_type,
            "product_comparison": project_data.get("product_comparison", {}),
            "pricing": project_data.get("pricing", ""),
            "erp_cost": project_data.get("erp_cost") or project_data.get("ERP_price", ""),
            "competitor_price": (project_data.get("product_comparison") or {}).get("competitor_price", ""),
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
                for p in same_brand_products
            ],
            "other_brand_products": [
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
                for p in other_brand_products
            ],
            "nearby_category_products": [
                {
                    "product_code": p.get("product_code", ""),
                    "brand": p.get("brand", ""),
                    "category_l2": p.get("category_l2", category_l2),
                    "category_l3": p.get("category_l3", ""),
                }
                for p in nearby_products[:10]
            ],
            "market_overview": market_overview,
            "category_sales_summary": category_sales_summary,
            "vl_report": vl_report,
            "comparison": comparison,
            "reference_product_code": own_product_code,
        }

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """品类地图缺失量化打分（V2：复用40+人群20+价格20+门槛20）"""
        gap_type = analysis_result.get("gap_type", "category_gap")
        gap_info = analysis_result.get("gap_info", {})
        vl_report = analysis_result.get("vl_report", {})
        comparison = analysis_result.get("comparison", {})
        summary = comparison.get("summary", {})
        module_reuse_detail = comparison.get("module_reuse_detail", [])
        market_overview = analysis_result.get("market_overview", {})
        category_sales_summary = analysis_result.get("category_sales_summary", {})

        # ── 维度1: 模块复用度 (40分) ──
        reuse_score, reuse_reason = self._score_module_reuse(
            gap_type, vl_report, module_reuse_detail, summary
        )

        # ── 维度2: 市场机会评估 (20分) ──
        market_score, market_reason = self._score_market_opportunity(
            gap_type, comparison, analysis_result, market_overview, category_sales_summary
        )

        # ── 维度3: 价格竞争力 (20分) ──
        price_score, price_reason = self._score_price(
            analysis_result, comparison, summary
        )

        # ── 维度4: 进入门槛 (20分) ──
        entry_score, entry_reason = self._score_entry_barrier(
            gap_type, vl_report, module_reuse_detail, summary, comparison
        )

        # ── 汇总 ──
        dimensions = [
            DimensionScore("模块复用度", reuse_score, 40, reuse_reason),
            DimensionScore("市场机会评估", market_score, 20, market_reason),
            DimensionScore("价格竞争力", price_score, 20, price_reason),
            DimensionScore("进入门槛", entry_score, 20, entry_reason),
        ]

        total = sum(d.score for d in dimensions)
        strengths = summary.get("our_strengths", [])
        weaknesses = summary.get("our_weaknesses", [])

        suggestions = []
        if reuse_score < 20:
            suggestions.append("模块复用度偏低，建议优先评估面料/版型的复用可能性，降低开发成本")
        if market_score < 12:
            suggestions.append("市场机会不足，建议重新评估品类容量和竞品格局，确认是否值得进入")
        if price_score < 12:
            suggestions.append("价格竞争力不足，建议调整定价策略或增加差异化卖点支撑溢价")
        if entry_score < 12:
            suggestions.append("进入门槛较高，建议分阶段进入：先复用再创新，降低供应链风险")
        total_sales = category_sales_summary.get("total_sales", 0)
        if total_sales > 0:
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

    # ────────────────────────────────────────────
    # 评分子方法
    # ────────────────────────────────────────────

    def _score_module_reuse(
        self,
        gap_type: str,
        vl_report: dict,
        module_reuse_detail: list[dict],
        summary: dict,
    ) -> tuple[int, str]:
        """模块复用度评分 (40分)

        基于VL拆解的模块对比结果 + 加权评分表。
        场景A: 有VL对比→用section3的same/competitor_only
        场景B: 无自家产品→用CBB库间接匹配+GLM推理的module_reuse_detail
        """
        # 尝试从VL报告获取复用率
        section3 = vl_report.get("section3_module_comparison", {})
        section5 = vl_report.get("section5_reuse_analysis", {})

        # 优先使用 module_reuse_detail（GLM推理的逐模块复用评估）
        if module_reuse_detail:
            return self._calc_weighted_reuse_score(module_reuse_detail, gap_type)

        # 降级：从VL section3/5 计算
        if section3:
            same_count = len(section3.get("same_modules", []))
            comp_only = len(section3.get("competitor_only", []))
            own_only = len(section3.get("own_only", []))
            total = same_count + comp_only + own_only

            if total > 0:
                reuse_rate = section3.get("overall_reuse_rate", 0)
                if isinstance(reuse_rate, (int, float)) and reuse_rate > 0:
                    # 有精确复用率
                    if reuse_rate >= 80:
                        score = 35
                    elif reuse_rate >= 60:
                        score = 28
                    elif reuse_rate >= 40:
                        score = 20
                    else:
                        score = 12
                    return score, f"VL对比复用率{reuse_rate}%，相同{same_count}/竞品独有{comp_only}/自家独有{own_only}"

                # 无精确复用率，按same占比估算
                same_ratio = same_count / total
                if same_ratio >= 0.6:
                    score = 30
                elif same_ratio >= 0.4:
                    score = 22
                elif same_ratio >= 0.2:
                    score = 15
                else:
                    score = 10
                return score, f"VL对比: 相同{same_count}/竞品独有{comp_only}/自家独有{own_only}，相同占比{same_ratio:.0%}"

        # 场景B无VL对比：从summary取复用/新建
        reuse_count = len(summary.get("reuse_modules", []))
        new_count = len(summary.get("new_modules_needed", []))
        total = reuse_count + new_count

        if total > 0:
            reuse_ratio = reuse_count / total
            # 场景B天然偏低，加一个基础分
            base = 8 if gap_type == "category_gap" else 0
            score = base + int(32 * reuse_ratio)
            score = min(40, score)
            return score, f"{'品类缺失间接评估' if gap_type == 'category_gap' else '品牌缺失'}，复用{reuse_count}/新建{new_count}，复用率{reuse_ratio:.0%}"

        # 完全无数据
        if gap_type == "category_gap":
            return 8, "品类缺失，无复用数据参考，默认低分"
        return 15, "无模块复用数据，默认中等偏低分"

    def _calc_weighted_reuse_score(
        self, module_reuse_detail: list[dict], gap_type: str
    ) -> tuple[int, str]:
        """基于加权评分表计算模块复用度"""
        total_weighted_score = 0.0
        total_weight = 0.0
        detail_counts = {"面料/材质": {}, "版型/结构": {}, "功能组件": {}, "外观/配色": {}, "配件/辅材": {}}

        for m in module_reuse_detail:
            module_name = m.get("module_name", "")
            match_level = m.get("match_level", "需新建")  # 相同/相似/需改造/需新建
            module_type = m.get("module_type", "") or _classify_module_type(module_name)

            # 获取评分表
            if module_type in MODULE_REUSE_TABLE:
                weight, *scores = MODULE_REUSE_TABLE[module_type]
            else:
                weight, *scores = MODULE_REUSE_TABLE["功能组件"]

            idx = _match_level_to_index(match_level)
            score = scores[idx]
            total_weighted_score += score * weight
            total_weight += weight

            # 统计详情
            if module_type in detail_counts:
                detail_counts[module_type][match_level] = detail_counts[module_type].get(match_level, 0) + 1

        if total_weight == 0:
            return 10, "无有效模块复用数据"

        # 归一化到40分制（满分15分×权重之和 → 40分）
        avg_score = total_weighted_score / total_weight
        final_score = int(avg_score / 15 * 40)
        final_score = max(0, min(40, final_score))

        # 场景B（品类缺失）间接评估不确定度高，适当保守
        if gap_type == "category_gap":
            final_score = int(final_score * 0.85)

        # 构建原因
        reason_parts = []
        for mt, counts in detail_counts.items():
            if counts:
                parts = [f"{k}{v}个" for k, v in counts.items()]
                reason_parts.append(f"{mt}({', '.join(parts)})")

        reason = "加权复用评估: " + "；".join(reason_parts) if reason_parts else "模块复用评估完成"
        return final_score, reason

    def _score_market_opportunity(
        self,
        gap_type: str,
        comparison: dict,
        analysis_result: dict,
        market_overview: dict,
        category_sales_summary: dict,
    ) -> tuple[int, str]:
        """市场机会评估 (20分)

        评估品类本身的商业可行性，与公共分析的人群场景描述质量不重叠。
        - 品类市场容量 (7分): 市场够大才值得进
        - 竞品验证度 (7分): 竞品已有稳定销量=需求已被验证
        - 品牌空缺程度 (6分): 空缺越大→机会越大
        """
        market_analysis = comparison.get("market_opportunity_analysis", {})
        reason_parts = []

        # ── 品类市场容量 (7分) ──
        capacity_score = 3  # 默认中等
        total_sales = category_sales_summary.get("total_sales", 0)
        total_products = category_sales_summary.get("total_products", 0)

        if market_analysis:
            capacity_level = market_analysis.get("market_capacity", "")
        else:
            # 无LLM分析，从数据估算
            if total_sales > 50000:
                capacity_level = "大"
            elif total_sales > 10000:
                capacity_level = "中"
            elif total_sales > 0:
                capacity_level = "小"
            else:
                capacity_level = "未知"

        if "大" in capacity_level or "高" in capacity_level:
            capacity_score = 7
        elif "中" in capacity_level:
            capacity_score = 5
        elif "小" in capacity_level or "低" in capacity_level:
            capacity_score = 3
        elif "未知" in capacity_level:
            capacity_score = 2
        else:
            # 从实际数据校准
            if total_sales > 50000:
                capacity_score = 7
            elif total_sales > 20000:
                capacity_score = 5
            elif total_sales > 5000:
                capacity_score = 4
            elif total_sales > 0:
                capacity_score = 3

        reason_parts.append(f"品类容量{capacity_level}(总销量{total_sales:,})")

        # ── 竞品验证度 (7分) ──
        verify_score = 3  # 默认
        brand_count = category_sales_summary.get("brand_count", 0)
        top_brand_sales = category_sales_summary.get("top_brand_sales", 0)

        if market_analysis:
            verify_level = market_analysis.get("competitor_validation", "")
        else:
            if top_brand_sales > 5000:
                verify_level = "强验证"
            elif top_brand_sales > 1000:
                verify_level = "有验证"
            elif top_brand_sales > 0:
                verify_level = "弱验证"
            else:
                verify_level = "未验证"

        if "强" in verify_level or "充分" in verify_level:
            verify_score = 7
        elif "有" in verify_level or "中等" in verify_level:
            verify_score = 5
        elif "弱" in verify_level:
            verify_score = 3
        elif "未" in verify_level or "无" in verify_level:
            verify_score = 1

        reason_parts.append(f"竞品验证{verify_level}({brand_count}品牌/TOP{top_brand_sales:,})")

        # ── 品牌空缺程度 (6分) ──
        gap_score = 3  # 默认
        if market_analysis:
            gap_level = market_analysis.get("brand_gap_level", "")
        else:
            gap_level = ""

        if gap_type == "category_gap":
            # 品类缺失=全公司没有，空缺最大
            if "拥挤" in gap_level or "饱和" in gap_level:
                gap_score = 2
            elif "竞争" in gap_level:
                gap_score = 3
            else:
                gap_score = 6  # 空白市场机会大
        elif gap_type == "brand_gap":
            # 品牌缺失=公司有但品牌没有，需突围
            if "拥挤" in gap_level or "饱和" in gap_level:
                gap_score = 2
            elif "竞争" in gap_level:
                gap_score = 3
            else:
                gap_score = 4  # 有参照但需突围
        elif gap_type == "no_gap":
            gap_score = 1  # 同品牌已有，空间有限

        gap_labels = {
            "category_gap": "品类缺失(机会大)",
            "brand_gap": "品牌缺失(需突围)",
            "no_gap": "无空缺",
        }
        reason_parts.append(f"空缺程度: {gap_labels.get(gap_type, gap_type)}")

        total = capacity_score + verify_score + gap_score
        reason = "；".join(reason_parts)
        return total, reason

    def _score_price(
        self, analysis_result: dict, comparison: dict, summary: dict
    ) -> tuple[int, str]:
        """价格竞争力评分 (20分)"""
        # 我方定价：优先从 analysis_result 取（analyze() 保存的），再从 summary 取
        pricing = analysis_result.get("pricing", "") or summary.get("pricing", 0) or 0
        erp_price = analysis_result.get("erp_cost", "") or summary.get("erp_price", 0) or 0

        # 竞品价格优先级：Excel解析结果 > summary > vl_report
        competitor_price = summary.get("competitor_price", 0) or 0
        if not competitor_price:
            # 从 analysis_result 取（analyze() 已保存 product_comparison）
            pc = analysis_result.get("product_comparison", {})
            if isinstance(pc, dict):
                competitor_price = pc.get("competitor_price", 0) or 0
        if not competitor_price:
            competitor_price = analysis_result.get("competitor_price", 0) or 0

        # 尝试从vl_report获取竞品价格
        vl_report = analysis_result.get("vl_report", {})
        if not competitor_price and vl_report:
            section6 = vl_report.get("section6_incremental_value", {})
            comp_price_info = section6.get("competitor_price", "")
            if comp_price_info:
                try:
                    competitor_price = float(str(comp_price_info).replace("元", "").replace("￥", "").strip())
                except (ValueError, TypeError):
                    pass

        if pricing and competitor_price:
            try:
                our_price = float(str(pricing).replace("元", "").replace("￥", "").strip())
                comp_price = float(str(competitor_price).replace("元", "").replace("￥", "").strip())

                if comp_price > 0:
                    deviation = (our_price - comp_price) / comp_price

                    if -0.10 <= deviation <= 0.10:
                        price_score = 18
                        reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，价格接近"
                    elif -0.20 <= deviation < -0.10:
                        price_score = 15
                        reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，略低于竞品"
                    elif 0.10 < deviation <= 0.20:
                        price_score = 13
                        reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，略高于竞品"
                    elif -0.30 <= deviation < -0.20:
                        price_score = 10
                        reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，偏低较多"
                    elif 0.20 < deviation <= 0.30:
                        price_score = 8
                        reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，偏高较多"
                    elif deviation > 0.30:
                        price_score = 4
                        reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，价格过高"
                    else:
                        price_score = 6
                        reason = f"定价{our_price:.0f}元 vs 竞品{comp_price:.0f}元，偏差{deviation:+.1%}，价格过低"

                    # 毛利空间修正
                    if erp_price and our_price > 0:
                        try:
                            erp_val = float(str(erp_price).replace("元", "").replace("￥", "").strip())
                            margin = (our_price - erp_val) / our_price
                            if margin < 0.20:
                                price_score = max(3, price_score - 4)
                                reason += f"，毛利率{margin:.0%}偏低"
                            elif margin < 0.40:
                                price_score = max(3, price_score - 2)
                                reason += f"，毛利率{margin:.0%}偏紧"
                        except (ValueError, TypeError):
                            pass

                    return price_score, reason

            except (ValueError, TypeError):
                pass

        if pricing and not competitor_price:
            return 10, f"有定价{pricing}，但无竞品价格参考"
        if not pricing and competitor_price:
            return 8, f"竞品价格{competitor_price}，但我方未定价"
        return 6, "价格信息缺失，无法评估"

    def _score_entry_barrier(
        self,
        gap_type: str,
        vl_report: dict,
        module_reuse_detail: list[dict],
        summary: dict,
        comparison: dict,
    ) -> tuple[int, str]:
        """进入门槛评分 (20分)

        面料/版型需新建 → 扣分重（1.5x）
        其他模块需新建 → 扣分轻
        工厂有相关产品 → 加分
        """
        # 从 module_reuse_detail 计算加权新建比例
        if module_reuse_detail:
            weighted_new = 0.0
            weighted_total = 0.0
            fabric_new = 0
            pattern_new = 0

            for m in module_reuse_detail:
                module_name = m.get("module_name", "")
                match_level = m.get("match_level", "需新建")
                module_type = m.get("module_type", "") or _classify_module_type(module_name)

                if module_type in MODULE_REUSE_TABLE:
                    weight = MODULE_REUSE_TABLE[module_type][0]
                else:
                    weight = 1.0

                weighted_total += weight
                if match_level in ("需新建", "新建", "new"):
                    weighted_new += weight
                    if module_type == "面料/材质":
                        fabric_new += 1
                    elif module_type == "版型/结构":
                        pattern_new += 1

            if weighted_total > 0:
                weighted_new_ratio = weighted_new / weighted_total
            else:
                weighted_new_ratio = 0.5

            # 门槛得分：新建占比越低 → 门槛越低 → 得分越高
            entry_score = max(3, int(20 * (1 - weighted_new_ratio * 0.8)))

            reason_parts = []
            if fabric_new:
                reason_parts.append(f"面料需新建{fabric_new}项")
            if pattern_new:
                reason_parts.append(f"版型需新建{pattern_new}项")
            reason_parts.append(f"加权新建比{weighted_new_ratio:.0%}")
            reason = "，".join(reason_parts)

        else:
            # 无模块复用详情，从summary估算
            reuse_count = len(summary.get("reuse_modules", []))
            new_count = len(summary.get("new_modules_needed", []))
            total = reuse_count + new_count

            if total > 0:
                new_ratio = new_count / total
                entry_score = max(3, int(20 * (1 - new_ratio * 0.7)))
                reason = f"新建{new_count}项/复用{reuse_count}项，新建比{new_ratio:.0%}"
            else:
                entry_score = 8
                reason = "无模块操作数据"

        # 场景B（品类缺失）天然门槛高
        if gap_type == "category_gap":
            entry_score = int(entry_score * 0.8)
            reason += "，品类缺失天然门槛高"

        # 工厂有相关产品可做 → 加分
        factory_capability = comparison.get("factory_capability", False)
        if factory_capability:
            entry_score = min(20, entry_score + 3)
            reason += "，工厂有相关产品可做"

        return entry_score, reason

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

        # 场景判断
        gap_info = analysis_result.get("gap_info", {})
        gap_type = analysis_result.get("gap_type", "")
        gap_labels = {
            "brand_gap": "🔲 品牌缺失（公司有该品类产品，但立项品牌下没有）",
            "category_gap": "🆕 品类缺失（公司完全没有该品类产品）",
            "no_gap": "📦 品类补全（同品牌下已有产品）",
        }
        lines.append(f"  场景: {gap_labels.get(gap_type, gap_type)}")
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

        # 其他品牌同品类产品（场景A）
        other_brand = analysis_result.get("other_brand_products", [])
        if other_brand:
            lines.append("")
            lines.append(f"  ── 其他自有品牌同品类产品 ({len(other_brand)}个) ──")
            for p in other_brand[:5]:
                lines.extend(format_product_detail(p, indent="    "))
            if len(other_brand) > 5:
                lines.append(f"    ... 还有{len(other_brand)-5}个产品")

        # 模块复用详情
        comparison = analysis_result.get("comparison", {})
        module_reuse_detail = comparison.get("module_reuse_detail", [])
        if module_reuse_detail:
            lines.append("")
            lines.append("  ── 逐模块复用评估（加权） ──")
            lines.append(f"  {'模块':<12} {'类型':<8} {'匹配度':<8} {'得分':<6} {'说明'}")
            lines.append(f"  {'-'*60}")
            for m in module_reuse_detail:
                lines.append(f"  {m.get('module_name', ''):<12} {m.get('module_type', ''):<8} "
                            f"{m.get('match_level', ''):<8} {m.get('score', ''):<6} {m.get('note', '')}")

        # 市场机会评估
        market_analysis = comparison.get("market_opportunity_analysis", {})
        if market_analysis:
            lines.append("")
            lines.append("  ── 市场机会评估 ──")
            if market_analysis.get("market_capacity"):
                lines.append(f"  品类市场容量: {market_analysis['market_capacity']}")
            if market_analysis.get("capacity_detail"):
                lines.append(f"    {market_analysis['capacity_detail']}")
            if market_analysis.get("competitor_validation"):
                lines.append(f"  竞品验证度: {market_analysis['competitor_validation']}")
            if market_analysis.get("validation_detail"):
                lines.append(f"    {market_analysis['validation_detail']}")
            if market_analysis.get("brand_gap_level"):
                lines.append(f"  品牌空缺程度: {market_analysis['brand_gap_level']}")
            if market_analysis.get("gap_detail"):
                lines.append(f"    {market_analysis['gap_detail']}")
            if market_analysis.get("overall_opportunity"):
                lines.append(f"  综合评估: {market_analysis['overall_opportunity']}")

        # VL报告摘要
        vl_report = analysis_result.get("vl_report", {})
        if vl_report:
            mode = vl_report.get("_mode", "")
            step1_time = vl_report.get("_step1_time", "")
            step2_time = vl_report.get("_step2_time", "")
            lines.append("")
            lines.append(f"  ── VL拆解报告 ({mode}, VL:{step1_time} + GLM:{step2_time}) ──")

            section3 = vl_report.get("section3_module_comparison", {})
            if section3:
                same = len(section3.get("same_modules", []))
                comp_only = len(section3.get("competitor_only", []))
                own_only = len(section3.get("own_only", []))
                reuse_rate = section3.get("overall_reuse_rate", "N/A")
                lines.append(f"  模块对比: 相同{same} / 竞品独有{comp_only} / 自家独有{own_only}, 复用率{reuse_rate}%")

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

        if score and score.suggestions:
            lines.append("")
            lines.append("  改进建议:")
            for s in score.suggestions:
                lines.append(f"    > {s}")

        return "\n".join(lines)


# ============================================================
# GLM专项推理
# ============================================================

async def _category_gap_analysis(
    llm: LLMClient,
    vl_report: dict,
    project_data: dict,
    gap_info: dict,
    same_brand_products: list[dict],
    other_brand_products: list[dict],
    nearby_products: list[dict],
    market_overview: dict,
    all_cbb_modules: list[dict],
) -> dict:
    """品类缺失GLM专项推理：模块复用+人群场景+风险评估"""
    if not llm.is_available:
        return {
            "module_reuse_detail": [],
            "market_opportunity_analysis": {},
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

    gap_type = gap_info.get("gap_type", "category_gap")
    category_l2 = project_data.get("category_l2") or project_data.get("categoryl2", "")
    category_l3 = project_data.get("category_l3") or project_data.get("categoryl3", "")
    brand = project_data.get("brand", "")
    target_audience = project_data.get("used_people") or project_data.get("target_audience", "")
    used_scene = project_data.get("used_scene") or project_data.get("target_scene", "")
    upgrade_direction = project_data.get("upgrade_direction") or project_data.get("product_feature", "")

    # ── 构建上下文 ──

    # 场景描述
    gap_labels = {
        "brand_gap": "品牌缺失（公司有该品类产品，但立项品牌下没有）",
        "category_gap": "品类缺失（公司完全没有该品类产品）",
    }
    gap_desc = gap_labels.get(gap_type, gap_info.get("gap_description", ""))

    # VL拆解结果摘要
    vl_summary = ""
    if vl_report:
        section1 = vl_report.get("section1_visual_analysis", {})
        section2 = vl_report.get("section2_abc_modules", {})
        section3 = vl_report.get("section3_module_comparison", {})
        section5 = vl_report.get("section5_reuse_analysis", {})

        if section1:
            vl_summary += f"\n== VL视觉分析 ==\n"
            vl_summary += f"产品类型: {section1.get('product_type', '')}\n"
            vl_summary += f"结构形态: {section1.get('structure_form', '')}\n"
            vl_summary += f"材料质感: {section1.get('material_texture', '')}\n"

        if section2:
            b_level = section2.get("b_level", [])
            if b_level:
                vl_summary += f"\n== 自家产品模块拆解 ({len(b_level)}个B级模块) ==\n"
                for m in b_level:
                    vl_summary += f"  - {m.get('id', '')} {m.get('name', '')}: {m.get('core_function', '')}\n"

        comp_modules = vl_report.get("competitor_modules", [])
        if comp_modules:
            vl_summary += f"\n== 竞品模块拆解 ({len(comp_modules)}个模块) ==\n"
            for m in comp_modules:
                vl_summary += f"  - {m.get('id', '')} {m.get('name', '')}: {m.get('core_function', '')}\n"

        if section3:
            same = section3.get("same_modules", [])
            comp_only = section3.get("competitor_only", [])
            own_only = section3.get("own_only", [])
            reuse_rate = section3.get("overall_reuse_rate", "N/A")

            vl_summary += f"\n== VL模块对比结果 ==\n"
            vl_summary += f"复用率: {reuse_rate}%\n"
            if same:
                vl_summary += f"相同模块: {', '.join(m.get('module_name', '') for m in same)}\n"
            if comp_only:
                vl_summary += f"竞品独有: {', '.join(m.get('module_name', '') for m in comp_only)}\n"
            if own_only:
                vl_summary += f"自家独有: {', '.join(m.get('module_name', '') for m in own_only)}\n"

        if section5:
            vl_summary += f"\n== 复用分析 ==\n"
            vl_summary += f"整体复用率: {section5.get('overall_reuse_rate', 'N/A')}\n"
            vl_summary += f"核心模块复用率: {section5.get('core_reuse_rate', 'N/A')}\n"
            new_needed = section5.get("new_modules_needed", [])
            if new_needed:
                vl_summary += f"需新建模块: {', '.join(str(m) for m in new_needed)}\n"

    # 其他品牌产品信息
    other_brand_summary = ""
    if other_brand_products:
        other_brand_summary = f"\n== 其他自有品牌同品类产品 ({len(other_brand_products)}个) ==\n"
        for p in other_brand_products[:8]:
            code = p.get("product_code", "?")
            pbrand = p.get("brand", "?")
            cat3 = p.get("category_l3", "")
            mods = p.get("modules", [])
            other_brand_summary += f"  - {code} ({pbrand}, {cat3}), {len(mods)}个模块\n"
            for m in mods[:5]:
                other_brand_summary += f"    • {m.get('cbb_code', '')} {m.get('cbb_name', '')} [{m.get('sub_type', '')}]\n"

    # CBB模块库摘要
    cbb_summary = ""
    if all_cbb_modules:
        cbb_summary = f"\n== CBB模块库（可复用模块，{len(all_cbb_modules)}个） ==\n"
        for m in all_cbb_modules[:15]:
            cbb_summary += f"  - {m.get('cbb_code', '')} {m.get('cbb_name', '')} [{m.get('category', '')}/{m.get('sub_type', '')}]\n"

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

    # 各分区其它信息（Excel解析中每个分组的other字段）
    base_other = project_data.get("base_extra_text") or project_data.get("base_other", "")
    group_other = project_data.get("group_extra_text") or project_data.get("group_other", "")
    product_comparison = project_data.get("product_comparison", {})
    competitor_other = product_comparison.get("competitor_other", "") if isinstance(product_comparison, dict) else ""
    design_require = project_data.get("design_require", {})
    design_other = design_require.get("design_other", "") if isinstance(design_require, dict) else ""

    other_info_section = ""
    other_parts = []
    if base_other:
        other_parts.append(f"基础信息补充: {base_other}")
    if group_other:
        other_parts.append(f"群体分析补充: {group_other}")
    if competitor_other:
        other_parts.append(f"竞品分析补充: {competitor_other}")
    if design_other:
        other_parts.append(f"设计要求补充: {design_other}")
    if other_parts:
        other_info_section = "\n== 补充信息（各分区其它内容） ==\n" + "\n".join(other_parts) + "\n"

    # 场景B专用：无自家产品，需CBB库做间接匹配说明
    indirect_match_note = ""
    if gap_type == "category_gap":
        indirect_match_note = """
【重要】这是品类缺失场景（公司完全没有该品类产品），无自家产品可做视觉对比。
请基于VL拆解的竞品模块 + CBB模块库 + 其他品类产品的模块，做间接复用评估：
- 面料/材质: 如果CBB库或其他品类产品有同类面料 → "相似"
- 版型/结构: 如果CBB库有同类版型 → "相似"，不同但可改 → "需改造"
- 功能组件: 如果CBB库有同类组件 → "相似"
- 外观/配色: 同品类下配色风格相似 → "相似"
- 完全找不到类似 → "需新建"
"""

    prompt = f"""【任务】深入分析品类地图空白，输出模块复用评估、人群场景分析和风险评估。
【规则】只返回一个合法的JSON对象，不要输出任何其他文字、解释或markdown格式。

== 品类缺失情况 ==
品牌: {brand}
品类: {category_l2} > {category_l3}
缺失类型: {gap_desc}
{gap_info.get('gap_description', '')}

== 目标人群 ==
{target_audience or '未指定'}

== 使用场景 ==
{used_scene or '未指定'}

== 升级/产品方向 ==
{upgrade_direction or '未指定'}
{other_info_section}{indirect_match_note}
{market_section}
{other_brand_summary}
{cbb_summary}
{vl_summary}

【分析要求】
1. 模块复用评估：逐个模块评估复用可能性和匹配级别（相同/相似/需改造/需新建），面料和版型权重最高
2. 市场机会评估：品类市场容量（大/中/小/未知）、竞品验证度（强验证/有验证/弱验证/未验证）、品牌空缺程度
3. 风险评估：进入新品类/新品牌的风险
4. 模块组合路线图：优先级排序+时间线建议

【输出格式】严格按此JSON结构输出（module_reuse_detail至少5条，market_opportunity_analysis必填，upgrade_roadmap至少3条）：
{{
    "module_reuse_detail": [
        {{
            "module_name": "针织面料",
            "module_type": "面料/材质",
            "match_level": "相似",
            "score": 13,
            "source": "CBB库PK001 / 参照产品HY63",
            "note": "CBB库有同类针织面料，材质相似可复用"
        }},
        {{
            "module_name": "套筒版型",
            "module_type": "版型/结构",
            "match_level": "需改造",
            "score": 7,
            "source": "改造自护膝版型",
            "note": "有类似版型但需要调整尺寸和支撑结构"
        }}
    ],
    "market_opportunity_analysis": {{
        "market_capacity": "大/中/小/未知",
        "capacity_detail": "品类市场容量详细说明（100-150字，结合品类销量数据给出具体分析）",
        "competitor_validation": "强验证/有验证/弱验证/未验证",
        "validation_detail": "竞品验证度详细说明（100-150字，分析竞品销量和品牌分布是否验证了需求）",
        "brand_gap_level": "空白/竞争/拥挤",
        "gap_detail": "品牌空缺程度详细说明（100-150字，分析公司品牌在该品类的空缺情况和突围机会）",
        "overall_opportunity": "市场机会综合评估（150-200字，结合容量+验证+空缺给出结论）"
    }},
    "summary": {{
        "market_opportunity": "市场机会评估（150-300字，结合品类数据给出具体分析）",
        "our_strengths": ["我方优势1（具体说明）", "我方优势2", "我方优势3"],
        "our_weaknesses": ["我方不足1（具体说明）", "我方不足2", "我方不足3"],
        "reuse_modules": ["可复用模块1 (来源)", "可复用模块2 (来源)"],
        "new_modules_needed": ["需新建模块1 (具体说明)", "需新建模块2"],
        "risk_factors": ["风险1（具体说明影响）", "风险2", "风险3"],
        "factory_capability": false,
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
}}

规则：
1. module_type只能是：面料/材质、版型/结构、功能组件、外观/配色、配件/辅材
2. match_level只能是：相同、相似、需改造、需新建
3. score按照评分表：面料/版型(相同15/相似13/需改造7/需新建3)，功能组件(相同15/相似11/需改造6/需新建3)，外观/配色(相同15/相似13/需改造8/需新建5)，配件/辅材(相同15/相似11/需改造6/需新建5)
4. market_opportunity_analysis必填，不能为空
5. 面料和版型是最关键的复用维度，需要重点分析
6. 相同极罕见，相似才是常态高分
7. market_capacity/competitor_validation/brand_gap_level必须从提供的枚举值中选择"""

    original_max_tokens = llm.max_tokens
    llm.max_tokens = 12000

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
            return _empty_result("品类分析返回格式异常")

    except Exception as e:
        logger.error(f"[品类缺失分析] 异常: {e}")
        return _empty_result(f"品类分析异常: {e}")

    finally:
        llm.max_tokens = original_max_tokens


def _empty_result(reason: str) -> dict:
    """返回空的分析结果"""
    return {
        "module_reuse_detail": [],
        "market_opportunity_analysis": {},
        "summary": {
            "our_strengths": [],
            "our_weaknesses": [],
            "reuse_modules": [],
            "new_modules_needed": [],
            "overall_assessment": reason,
        },
        "upgrade_roadmap": [],
        "_error": reason,
    }
