# -*- coding: utf-8 -*-
"""
🗺️ 品类地图缺失分析器（V3 — CBBMatcher + 双场景评分）

两种场景：
  A. 品牌缺失（is_new_category=False）：复用爆品升级5维度评分
  B. 品类缺失（is_new_category=True）：3维度评分（模块可行性50 + 设计合理性30 + 价格市场20）

流程：
  1. 数据库检索 → 判断场景A/B
  2. 图片检索（自家产品图 + 竞品图）
  3. VL拆解（compare/single）
  4. CBBMatcher: 竞品模块 → sub_type级别匹配
  5. LLM评分（场景A: 5维度 / 场景B: 3维度）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from product_review_agent.agents.llm_client import LLMClient, get_llm_client
from product_review_agent.product_db.product_query import ProductQuery
from product_review_agent.product_db.cbb_matcher import CBBMatcher, MatchResult
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


# 场景A评分维度（复用爆品升级）
SCENARIO_A_DIMENSIONS = [
    ("模块复用", 48, "CBB sub_type匹配 + VL对比覆盖率"),
    ("模块升级合理性", 22, "目标-动作一致性 + 卖点保留度 + 升级必要性"),
    ("价格分析", 10, "定价竞争力 + 成本-定价匹配度"),
    ("营销分析", 10, "升级卖点的营销价值"),
    ("可行性分析", 10, "供应链/打样可行性"),
]

# 场景B评分维度（新品类进入）
SCENARIO_B_DIMENSIONS = [
    ("模块可行性", 50, "CBB直接复用 + 模块可获取 + 开模难度"),
    ("产品设计合理性", 30, "设计目的明确性 + 升级方向合理性 + 差异化价值"),
    ("价格与市场", 20, "价格竞争力 + 市场验证"),
]


class CategoryGapAnalyzer(BaseAnalyzer):
    """🗺️ 品类地图缺失分析器（V3）"""

    analysis_type = "category_gap"
    display_name = "品类地图缺失"
    emoji = "🗺️"

    SCORING_DIMENSIONS = SCENARIO_B_DIMENSIONS  # 默认，实际按场景动态调整

    async def analyze(self, project_data: dict, images: list = None) -> dict:
        """品类地图缺失模块对比分析。"""
        llm = get_llm_client()
        category_l3 = project_data.get("category_l3") or project_data.get("categoryl3", "")
        category_l2 = project_data.get("category_l2") or project_data.get("categoryl2", "")
        category_l1 = project_data.get("category_l1") or project_data.get("categoryl1", "")
        brand = project_data.get("brand", "")
        competitor_name = project_data.get("competitor_name", "")
        product_name = project_data.get("product_name") or project_data.get("project_name", "")
        upgrade_direction = project_data.get("upgrade_direction") or project_data.get("product_feature", "")
        is_new_category = project_data.get("is_new_category", "")

        # 判断场景：is_new_category=True → 场景B（品类缺失），否则 → 场景A（品牌缺失）
        # 兼容 "是/否"、"True/False"、"true/false"
        if isinstance(is_new_category, bool):
            scenario_b = is_new_category
        else:
            scenario_b = str(is_new_category).strip().lower() in ("true", "是", "1")

        # Step 1: 数据库检索 → 判断场景
        gap_info = {"has_gap": True, "brand": brand, "category_l2": category_l2,
                    "category_l3": category_l3, "gap_description": "", "gap_type": ""}
        same_brand_products = []
        other_brand_products = []
        nearby_products = []
        market_overview = {}
        category_sales_summary = {}

        try:
            with ProductQuery() as pq:
                own_brands = pq.get_own_brands()
                _, is_competitor = pq.resolve_brand(brand)

                same_brand_products = pq.get_products_with_modules(
                    category_l2=category_l2, category_l3=category_l3, brand=brand,
                )

                if not is_competitor:
                    for ob in own_brands:
                        if ob == brand:
                            continue
                        prods = pq.get_products_with_modules(
                            category_l2=category_l2, category_l3=category_l3, brand=ob,
                        )
                        other_brand_products.extend(prods)
                else:
                    for ob in own_brands:
                        prods = pq.get_products_with_modules(
                            category_l2=category_l2, category_l3=category_l3, brand=ob,
                        )
                        other_brand_products.extend(prods)

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

                market_overview = pq.get_category_market_overview(
                    category_l2=category_l2, category_l3=category_l3,
                )
                nearby_products = pq.get_products_with_modules(category_l2=category_l2)

                all_skus = [p.get("product_code", "") for p in same_brand_products + other_brand_products]
                sales_data = pq.get_products_sales(all_skus)

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
            sales_data = {}

        # Step 2: 图片检索
        own_images_bytes = []
        own_product_code = ""
        competitor_images_bytes = []

        if images:
            competitor_images_bytes = list(images)

        gap_type = gap_info.get("gap_type", "category_gap")
        reference_products = other_brand_products if gap_type == "brand_gap" else []

        if reference_products:
            ref_sorted = sorted(
                reference_products,
                key=lambda p: max((s.get("sales_volume", 0) for s in sales_data.get(p.get("product_code", ""), [])), default=0),
                reverse=True,
            )
            for ref_p in ref_sorted[:3]:
                ref_code = ref_p.get("product_code", "")
                ref_imgs = find_product_images(ref_code)
                if ref_imgs:
                    own_images_bytes = [img[1] for img in ref_imgs[:2]]
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
            logger.info(f"[品类缺失] 场景B(品类缺失): VL单拆竞品")
            vl_report = await splitter.analyze_single(
                images=competitor_images_bytes,
                product_code=competitor_name or "参考产品",
                category_info={**category_info, "brand": "竞品"},
            )
        else:
            logger.warning("[品类缺失] 无可用图片，跳过VL拆解")

        # Step 4: CBB模块匹配
        cbb_match = MatchResult()
        cbb_summary = ""
        self_cbb_modules = []

        comparison = vl_report.get("section3_module_comparison", {})
        competitor_only = comparison.get("competitor_only", [])
        all_vl_modules = []

        if scenario_b:
            # 场景B：匹配所有竞品模块
            comp_modules = vl_report.get("competitor_modules", [])
            all_vl_modules = [m.get("name", "") for m in comp_modules if m.get("name")]
            if not all_vl_modules:
                all_vl_modules = [m.get("module_name", "") for m in competitor_only if m.get("module_name")]
        else:
            # 场景A：只匹配竞品独有模块
            all_vl_modules = [m.get("module_name", "") for m in competitor_only if m.get("module_name")]

        try:
            with CBBMatcher() as matcher:
                if all_vl_modules:
                    cbb_match = await matcher.match_modules(
                        all_vl_modules, llm, product_category=category_l2,
                    )
                    logger.info(f"[品类缺失] CBB匹配: {cbb_match.matched}/{cbb_match.total} 匹配率{cbb_match.match_rate}%")
                cbb_summary = matcher.get_cbb_summary()
        except Exception as e:
            logger.error(f"[品类缺失] CBB匹配异常: {e}")

        # 场景A：获取自家产品CBB模块
        if not scenario_b and own_product_code:
            self_cbb_modules = self._get_self_cbb_modules(own_product_code)

        # Step 5: LLM评分
        if scenario_b:
            llm_scoring = await self._llm_scoring_scenario_b(
                vl_report, project_data, gap_info, cbb_summary, cbb_match,
                market_overview, category_sales_summary,
            )
        else:
            llm_scoring = await self._llm_scoring_scenario_a(
                vl_report, project_data, gap_info, cbb_summary, cbb_match, self_cbb_modules,
            )

        return {
            "analysis_type": "category_gap",
            "scenario": "B" if scenario_b else "A",
            "gap_info": gap_info,
            "gap_type": gap_type,
            "product_code": own_product_code,
            "brand": brand,
            "category_info": category_info,
            "project_data": project_data,
            "vl_report": vl_report,
            "market_overview": market_overview,
            "category_sales_summary": category_sales_summary,
            "cbb_match": cbb_match,
            "self_cbb_modules": self_cbb_modules,
            "llm_scoring": llm_scoring,
            "existing_products": [
                {
                    "product_code": p.get("product_code", ""),
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
                    "brand": p.get("brand", ""),
                    "category_l2": p.get("category_l2", category_l2),
                    "category_l3": p.get("category_l3", ""),
                    "sales_data": sales_data.get(p.get("product_code", ""), []),
                    "module_count": len(p.get("modules", [])),
                    "modules": p.get("modules", []),
                }
                for p in other_brand_products
            ],
        }

    def _get_self_cbb_modules(self, product_code: str) -> list[dict]:
        """获取自家产品已关联的 CBB 模块"""
        if not product_code:
            return []
        try:
            from product_review_agent.product_db.database import ProductDB
            db = ProductDB()
            modules = db._get_product_modules(product_code)
            db.close()
            return modules
        except Exception as e:
            logger.error(f"[品类缺失] 自家CBB模块查询异常: {e}")
            return []

    # ============================================================
    # 场景A：复用爆品升级5维度评分
    # ============================================================

    async def _llm_scoring_scenario_a(
        self, vl_report: dict, project_data: dict, gap_info: dict,
        cbb_summary: str, cbb_match: MatchResult, self_cbb_modules: list,
    ) -> dict:
        """场景A LLM评分：复用爆品升级的5维度（模块复用48 + 升级合理性22 + 价格10 + 营销10 + 可行性10）"""
        llm = get_llm_client()
        if not llm.is_available:
            return {"_error": "LLM不可用"}

        comparison = vl_report.get("section3_module_comparison", {})
        same_modules = comparison.get("same_modules", [])
        competitor_only = comparison.get("competitor_only", [])
        own_only = comparison.get("own_only", [])

        def _fmt_modules(mods, max_n=8):
            lines = []
            for m in mods[:max_n]:
                name = m.get("module_name", "")
                detail = m.get("detail", "") or m.get("own_detail", "") or m.get("competitor_detail", "")
                lines.append(f"  - {name}: {detail[:80]}")
            return "\n".join(lines) if lines else "  (无)"

        vl_summary = f"""【自家+竞品相同模块】({len(same_modules)}个)
{_fmt_modules(same_modules)}

【竞品独有模块】({len(competitor_only)}个)
{_fmt_modules(competitor_only)}

【自家独有模块】({len(own_only)}个)
{_fmt_modules(own_only)}"""

        self_cbb_str = ""
        if self_cbb_modules:
            self_cbb_str = "\n".join(
                f"  - {m.get('cbb_name','')} ({m.get('category','')}/{m.get('sub_type','')})"
                for m in self_cbb_modules
            )
        else:
            self_cbb_str = "  (自家产品无CBB模块数据)"

        cbb_match_str = self._format_cbb_match(cbb_match)

        design_purpose = project_data.get("design_purpose", "")
        design_content = project_data.get("design_content", "") or project_data.get("upgrade_modules", "")
        feasibility = project_data.get("feasibility_analysis", "") or project_data.get("upgrade_valiable", "")
        pricing = project_data.get("pricing", "")
        erp_cost = project_data.get("erp_cost", "")
        competitor_price = project_data.get("competitor_price", "")
        selling_point = project_data.get("similar_product_selling_point", "") or project_data.get("product_hotpoint", "")

        prompt = f"""你是资深的电商产品立项审核专家，精通模块化产品分析和供应链评估。

## 任务
这是品牌缺失场景（公司有该品类产品，但立项品牌下没有）。请对该项目进行5个维度的评分分析。

## 品类缺失信息
{gap_info.get('gap_description', '')}

## CBB模块库分类体系（供参考）
{cbb_summary[:2000]}

## 自家产品已关联的CBB模块
{self_cbb_str}

## 竞品独有模块的CBB匹配结果（sub_type级别）
{cbb_match_str}

## VL对比拆解结果（自家 vs 竞品）
{vl_summary}

## 项目信息
- 设计目的: {design_purpose or '(未填写)'}
- 设计内容: {design_content or '(未填写)'}
- 可行性分析: {feasibility or '(未填写)'}
- 类似产品卖点: {selling_point or '(未填写)'}
- 定价: {pricing or '(未填写)'}
- ERP成本: {erp_cost or '(未填写)'}
- 竞品价格: {competitor_price or '(未填写)'}

---

请严格按以下JSON格式返回，不要加```json```包裹：

{{
  "module_reuse": {{
    "score": 0-48,
    "vl_to_cbb_mapping": [
      {{"vl_module": "VL模块名", "cbb_category": "FABRIC/PAD/...", "cbb_sub_type": "具体sub_type", "side": "same/competitor_only/own_only", "matched": true/false}}
    ],
    "core_categories": ["核心CBB分类名"],
    "reuse_categories": ["自家和竞品都有的CBB分类"],
    "missing_categories": ["竞品有但自家没有的CBB分类"],
    "unique_categories": ["自家有竞品没有的CBB分类"],
    "reason": "模块复用总评语"
  }},
  "upgrade_rationality": {{
    "score": 0-22,
    "goal_action_score": 0-8,
    "goal_action_reason": "目标-动作一致性分析",
    "hotpoint_preserve_score": 0-8,
    "hotpoint_preserve_reason": "原有卖点保留度分析",
    "necessity_score": 0-6,
    "necessity_reason": "升级必要性分析"
  }},
  "price_analysis": {{"score": 0-10, "reason": "价格分析"}},
  "marketing": {{"score": 0-10, "reason": "营销分析"}},
  "feasibility": {{"score": 0-10, "reason": "可行性分析"}}
}}

## 评分标准
### 模块复用（48分）
- 参考CBB匹配结果，已匹配到sub_type的模块=可复用，未匹配=需新建
- 同时考虑VL对比中"相同模块"=直接复用
- 核心分类覆盖率高=高分，按覆盖率比例打分

### 升级合理性（22分）
- 目标-动作一致性(8分): 设计目标是否明确、动作能否支撑
- 卖点保留度(8分): 是否保留原有产品卖点
- 升级必要性(6分): 目标→动作逻辑链是否合理

### 价格分析（10分）、营销分析（10分）、可行性分析（10分）
- 结合CBB匹配结果：已匹配的模块可获取性高"""

        return await self._call_llm(llm, prompt)

    # ============================================================
    # 场景B：3维度评分
    # ============================================================

    async def _llm_scoring_scenario_b(
        self, vl_report: dict, project_data: dict, gap_info: dict,
        cbb_summary: str, cbb_match: MatchResult,
        market_overview: dict, category_sales_summary: dict,
    ) -> dict:
        """场景B LLM评分：3维度（模块可行性50 + 设计合理性30 + 价格市场20）"""
        llm = get_llm_client()
        if not llm.is_available:
            return {"_error": "LLM不可用"}

        # VL单拆结果
        comp_modules = vl_report.get("competitor_modules", [])
        section1 = vl_report.get("section1_visual_analysis", {})
        section3 = vl_report.get("section3_module_comparison", {})

        vl_summary = ""
        if section1:
            vl_summary += f"产品类型: {section1.get('product_type', '')}\n"
            vl_summary += f"结构形态: {section1.get('structure_form', '')}\n"
            vl_summary += f"材料质感: {section1.get('material_texture', '')}\n"

        if comp_modules:
            vl_summary += f"\n竞品模块拆解 ({len(comp_modules)}个):\n"
            for m in comp_modules[:10]:
                vl_summary += f"  - {m.get('name', '')}: {m.get('core_function', '')}\n"

        if section3:
            vl_summary += f"\n复用率: {section3.get('overall_reuse_rate', 'N/A')}\n"

        cbb_match_str = self._format_cbb_match(cbb_match)

        # 市场概况
        market_str = ""
        if market_overview:
            total_prods = market_overview.get("total_products", 0)
            total_sales = market_overview.get("total_category_sales", 0)
            market_str = f"品类产品总数: {total_prods}个, 累计销量: {total_sales:,}\n"
            brand_dist = market_overview.get("brand_distribution", [])
            if brand_dist:
                market_str += "品牌分布:\n"
                for bd in brand_dist[:5]:
                    pct = (bd["total_sales"] / total_sales * 100) if total_sales > 0 else 0
                    market_str += f"  - {bd['brand']}: {bd['product_count']}个产品, 销量{bd['total_sales']:,} ({pct:.1f}%)\n"

        # 项目数据
        design_purpose = project_data.get("design_purpose", "")
        design_content = project_data.get("design_content", "") or project_data.get("upgrade_modules", "")
        feasibility = project_data.get("feasibility_analysis", "") or project_data.get("upgrade_valiable", "")
        pricing = project_data.get("pricing", "")
        erp_cost = project_data.get("erp_cost", "")
        competitor_price = project_data.get("competitor_price", "")
        audience_consistent = project_data.get("audience_consistent", "")
        market_size = project_data.get("market_size", "")
        estimated_sales = project_data.get("estimated_sales", "")

        prompt = f"""你是资深的电商产品立项审核专家，精通模块化产品分析和供应链评估。

## 任务
这是品类缺失场景（公司完全没有该品类产品）。请对该项目进行3个维度的评分分析。

## 品类缺失信息
{gap_info.get('gap_description', '')}

## CBB模块库分类体系（供参考）
{cbb_summary[:2000]}

## 竞品模块的CBB匹配结果（sub_type级别）
{cbb_match_str}

## VL拆解结果（仅竞品）
{vl_summary}

## 品类市场概况
{market_str or '(无市场数据)'}

## 项目信息
- 市场大小: {market_size or '(未填写)'}
- 目标销售额: {estimated_sales or '(未填写)'}
- 二级品类人群是否一致: {audience_consistent or '(未填写)'}
- 设计目的: {design_purpose or '(未填写)'}
- 设计内容: {design_content or '(未填写)'}
- 可行性分析: {feasibility or '(未填写)'}
- 定价: {pricing or '(未填写)'}
- ERP成本: {erp_cost or '(未填写)'}
- 竞品价格: {competitor_price or '(未填写)'}

---

请严格按以下JSON格式返回，不要加```json```包裹：

{{
  "module_feasibility": {{
    "score": 0-50,
    "cbb_reuse_score": 0-25,
    "cbb_reuse_reason": "CBB直接复用评估：竞品模块在CBB库中能找到sub_type匹配的占比",
    "acquisition_score": 0-15,
    "acquisition_reason": "模块可获取性评估：可行性分析中提到可直接获取的模块",
    "tooling_score": 0-10,
    "tooling_reason": "开模难度评估：需开模模块的数量和复杂度"
  }},
  "design_rationality": {{
    "score": 0-30,
    "purpose_score": 0-10,
    "purpose_reason": "设计目的明确性：ABC分类是否明确、设计目的能否支撑产品定位",
    "direction_score": 0-10,
    "direction_reason": "设计方向合理性：参照竞品，设计方向是否明确",
    "differentiation_score": 0-10,
    "differentiation_reason": "差异化价值：相比竞品的差异化是否成立"
  }},
  "price_market": {{
    "score": 0-20,
    "price_score": 0-10,
    "price_reason": "价格竞争力：定价vs竞品偏差分析",
    "market_score": 0-10,
    "market_reason": "市场验证：竞品销量+品牌数验证需求存在性"
  }}
}}

## 评分标准

### 模块可行性（50分）
- CBB直接复用（25分）：参考上方CBB匹配结果，已匹配到sub_type的模块占比越高分越高
- 模块可获取（15分）：可行性分析中明确提到可直接获取（供应商供货、现有产线）的模块
- 开模难度（10分）：需开模的模块越少、越简单，分越高

### 产品设计合理性（30分）
- 设计目的明确性（10分）：ABC分类明确、设计目的清晰
- 设计方向合理性（10分）：参照竞品，设计方向有依据
- 差异化价值（10分）：相比竞品有实际的差异化点

### 价格与市场（20分）
- 价格竞争力（10分）：定价vs竞品偏差在合理范围内（±10%内=10分，±20%=7分，超20%=3分）
- 市场验证（10分）：品类有竞品销量验证=高分，无验证=低分"""

        return await self._call_llm(llm, prompt)

    # ============================================================
    # 公共方法
    # ============================================================

    def _format_cbb_match(self, cbb_match: MatchResult) -> str:
        """格式化CBB匹配结果为文本"""
        if not cbb_match or not cbb_match.module_matches:
            return "  (无CBB匹配数据)"
        lines = [f"匹配率: {cbb_match.match_rate}% ({cbb_match.matched}/{cbb_match.total})"]
        for mm in cbb_match.module_matches:
            modules_info = ""
            if mm.cbb_modules:
                modules_info = " → " + ", ".join(
                    f"{m['cbb_name']}({m['cbb_code']})" for m in mm.cbb_modules[:3]
                )
            lines.append(f"  - {mm.vl_module} → {mm.cbb_category}/{mm.cbb_sub_type} [{mm.match_level}]{modules_info}")
        return "\n".join(lines)

    async def _call_llm(self, llm, prompt: str) -> dict:
        """调用LLM并解析返回的JSON"""
        try:
            result = await llm.acall_text(
                messages=[
                    {"role": "system", "content": "你是电商产品立项审核专家，精通模块化产品分析。只返回JSON。"},
                    {"role": "user", "content": prompt},
                ],
            )
            if isinstance(result, dict) and not result.get("_parse_error"):
                return result
            if isinstance(result, str):
                text = result.strip()
                if text.startswith("```"):
                    lines = text.split("\n")
                    json_lines = []
                    in_block = False
                    for line in lines:
                        if line.strip().startswith("```") and not in_block:
                            in_block = True
                            continue
                        elif line.strip() == "```" and in_block:
                            break
                        elif in_block:
                            json_lines.append(line)
                    if json_lines:
                        text = "\n".join(json_lines)
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and end > start:
                    try:
                        parsed = json.loads(text[start:end + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        pass
            return {"_error": f"LLM返回异常: {str(result)[:200]}"}
        except Exception as e:
            logger.error(f"[品类缺失] LLM评分异常: {e}")
            return {"_error": str(e)}

    # ============================================================
    # 评分
    # ============================================================

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """品类地图缺失量化打分"""
        scenario = analysis_result.get("scenario", "B")
        llm_scoring = analysis_result.get("llm_scoring", {})

        if llm_scoring.get("_error"):
            return self._default_score(scenario, llm_scoring["_error"])

        if scenario == "A":
            return self._score_scenario_a(llm_scoring, analysis_result)
        else:
            return self._score_scenario_b(llm_scoring, analysis_result)

    def _score_scenario_a(self, llm_scoring: dict, analysis_result: dict) -> AnalyzerScore:
        """场景A评分：5维度（和爆品升级一致）"""
        vl_report = analysis_result.get("vl_report", {})
        comparison = vl_report.get("section3_module_comparison", {})
        own_only = comparison.get("own_only", [])
        competitor_only = comparison.get("competitor_only", [])

        mr = llm_scoring.get("module_reuse", {})
        reuse_score = min(max(mr.get("score", 24), 0), 48)
        reuse_reason = mr.get("reason", "")
        mapping = mr.get("vl_to_cbb_mapping", [])
        if mapping:
            mapping_summary = ", ".join(f"{m.get('vl_module','')}→{m.get('cbb_category','')}/{m.get('cbb_sub_type','')}" for m in mapping[:6])
            reuse_reason = f"模块映射: {mapping_summary}\n{reuse_reason}"
        missing = mr.get("missing_categories", [])
        if missing:
            reuse_reason += f"\n缺失分类: {', '.join(missing)}"

        ur = llm_scoring.get("upgrade_rationality", {})
        if ur and not ur.get("_error"):
            ga = ur.get("goal_action_score", 4)
            hp = ur.get("hotpoint_preserve_score", 4)
            ne = ur.get("necessity_score", 3)
            upgrade_score = ga + hp + ne
            upgrade_reason = (f"目标-动作{ga}/8: {ur.get('goal_action_reason','')[:60]}\n"
                              f"    卖点保留{hp}/8: {ur.get('hotpoint_preserve_reason','')[:60]}\n"
                              f"    升级必要性{ne}/6: {ur.get('necessity_reason','')[:60]}")
        else:
            upgrade_score = 11
            upgrade_reason = "LLM评分不可用"

        pa = llm_scoring.get("price_analysis", {})
        price_score = min(max(pa.get("score", 5), 0), 10)
        price_reason = pa.get("reason", "")[:120]

        mk = llm_scoring.get("marketing", {})
        marketing_score = min(max(mk.get("score", 5), 0), 10)
        marketing_reason = mk.get("reason", "")[:120]

        fe = llm_scoring.get("feasibility", {})
        feasibility_score = min(max(fe.get("score", 5), 0), 10)
        feasibility_reason = fe.get("reason", "")[:120]

        dimensions = [
            DimensionScore("模块复用", reuse_score, 48, reuse_reason),
            DimensionScore("模块升级合理性", upgrade_score, 22, upgrade_reason),
            DimensionScore("价格分析", price_score, 10, price_reason),
            DimensionScore("营销分析", marketing_score, 10, marketing_reason),
            DimensionScore("可行性分析", feasibility_score, 10, feasibility_reason),
        ]

        total = sum(d.score for d in dimensions)
        suggestions = self._generate_suggestions_a(reuse_score, upgrade_score, price_score, marketing_score, feasibility_score, missing)

        strengths = []
        weaknesses = []
        if reuse_score >= 38:
            strengths.append("模块复用覆盖率高，开发成本低")
        if upgrade_score >= 18:
            strengths.append("升级逻辑清晰，原有卖点保留完好")
        if price_score >= 8:
            strengths.append("价格定位合理")
        if reuse_score < 16:
            weaknesses.append("模块复用覆盖率严重不足")
        if missing:
            weaknesses.append(f"缺失CBB分类: {', '.join(missing[:3])}")
        for m in own_only:
            if isinstance(m, dict) and m.get("is_advantage"):
                strengths.append(f"自家独有优势: {m.get('module_name', '')}")
        for m in competitor_only:
            if isinstance(m, dict):
                weaknesses.append(f"竞品独有: {m.get('module_name', '')}")

        return AnalyzerScore(
            analysis_type=self.analysis_type,
            dimensions=dimensions,
            total_score=total,
            max_score=100,
            strengths=strengths[:5],
            weaknesses=weaknesses[:5],
            suggestions=suggestions,
        )

    def _score_scenario_b(self, llm_scoring: dict, analysis_result: dict) -> AnalyzerScore:
        """场景B评分：3维度"""
        mf = llm_scoring.get("module_feasibility", {})
        if mf and not mf.get("_error"):
            cbb_reuse = min(max(mf.get("cbb_reuse_score", 12), 0), 25)
            acquisition = min(max(mf.get("acquisition_score", 7), 0), 15)
            tooling = min(max(mf.get("tooling_score", 5), 0), 10)
            feasibility_score = cbb_reuse + acquisition + tooling
            feasibility_reason = (f"CBB复用{cbb_reuse}/25: {mf.get('cbb_reuse_reason', '')[:60]}\n"
                                  f"    模块可获取{acquisition}/15: {mf.get('acquisition_reason', '')[:60]}\n"
                                  f"    开模难度{tooling}/10: {mf.get('tooling_reason', '')[:60]}")
        else:
            feasibility_score = 25
            feasibility_reason = "LLM评分不可用"

        dr = llm_scoring.get("design_rationality", {})
        if dr and not dr.get("_error"):
            purpose = min(max(dr.get("purpose_score", 5), 0), 10)
            direction = min(max(dr.get("direction_score", 5), 0), 10)
            diff = min(max(dr.get("differentiation_score", 5), 0), 10)
            design_score = purpose + direction + diff
            design_reason = (f"设计目的{purpose}/10: {dr.get('purpose_reason', '')[:60]}\n"
                             f"    设计方向{direction}/10: {dr.get('direction_reason', '')[:60]}\n"
                             f"    差异化{diff}/10: {dr.get('differentiation_reason', '')[:60]}")
        else:
            design_score = 15
            design_reason = "LLM评分不可用"

        pm = llm_scoring.get("price_market", {})
        if pm and not pm.get("_error"):
            price_s = min(max(pm.get("price_score", 5), 0), 10)
            market_s = min(max(pm.get("market_score", 5), 0), 10)
            price_market_score = price_s + market_s
            price_market_reason = (f"价格竞争力{price_s}/10: {pm.get('price_reason', '')[:60]}\n"
                                   f"    市场验证{market_s}/10: {pm.get('market_reason', '')[:60]}")
        else:
            price_market_score = 10
            price_market_reason = "LLM评分不可用"

        dimensions = [
            DimensionScore("模块可行性", feasibility_score, 50, feasibility_reason),
            DimensionScore("产品设计合理性", design_score, 30, design_reason),
            DimensionScore("价格与市场", price_market_score, 20, price_market_reason),
        ]

        total = sum(d.score for d in dimensions)

        suggestions = []
        if feasibility_score < 25:
            suggestions.append("模块可行性偏低，建议优先评估CBB库可复用模块，降低开发成本")
        if design_score < 18:
            suggestions.append("产品设计合理性不足，建议明确设计目的和差异化方向")
        if price_market_score < 12:
            suggestions.append("价格或市场验证不足，建议参考竞品定价并验证市场需求")

        strengths = []
        weaknesses = []
        if feasibility_score >= 40:
            strengths.append("模块可行性高，CBB复用率高")
        if design_score >= 24:
            strengths.append("产品设计合理，差异化明确")
        if price_market_score >= 16:
            strengths.append("价格竞争力强，市场有验证")
        if feasibility_score < 20:
            weaknesses.append("模块可行性低，需大量新建")
        if design_score < 12:
            weaknesses.append("产品设计目的不清晰")

        return AnalyzerScore(
            analysis_type=self.analysis_type,
            dimensions=dimensions,
            total_score=total,
            max_score=100,
            strengths=strengths[:5],
            weaknesses=weaknesses[:5],
            suggestions=suggestions,
        )

    def _default_score(self, scenario: str, error: str) -> AnalyzerScore:
        """LLM不可用时的默认评分"""
        if scenario == "A":
            dims = [
                DimensionScore("模块复用", 24, 48, f"LLM不可用: {error}"),
                DimensionScore("模块升级合理性", 11, 22, "LLM不可用"),
                DimensionScore("价格分析", 5, 10, "LLM不可用"),
                DimensionScore("营销分析", 5, 10, "LLM不可用"),
                DimensionScore("可行性分析", 5, 10, "LLM不可用"),
            ]
        else:
            dims = [
                DimensionScore("模块可行性", 25, 50, f"LLM不可用: {error}"),
                DimensionScore("产品设计合理性", 15, 30, "LLM不可用"),
                DimensionScore("价格与市场", 10, 20, "LLM不可用"),
            ]
        return AnalyzerScore(
            analysis_type=self.analysis_type,
            dimensions=dims,
            total_score=sum(d.score for d in dims),
            max_score=100,
            weaknesses=["LLM评分不可用"],
            suggestions=["建议检查LLM服务状态后重新评估"],
        )

    def _generate_suggestions_a(self, reuse, upgrade, price, mk, fe, missing) -> list[str]:
        suggestions = []
        if reuse < 24:
            suggestions.append("模块复用覆盖率低，需评估新建模块的成本和周期")
        if missing:
            suggestions.append(f"缺失CBB分类「{'、'.join(missing[:3])}」，建议补齐")
        if upgrade < 14:
            suggestions.append("升级合理性存疑，建议重新审视设计目标与动作的逻辑链")
        if price < 6:
            suggestions.append("价格与成本匹配度不足，建议重新评估定价策略")
        if mk < 6:
            suggestions.append("升级卖点营销价值不明显，建议提炼差异化营销话术")
        if fe < 6:
            suggestions.append("供应链可行性存疑，建议提前确认模块可获取性")
        return suggestions

    # ============================================================
    # 报告格式化
    # ============================================================

    def format_report(self, analysis_result: dict, score: AnalyzerScore = None) -> str:
        """格式化品类地图缺失分析结果"""
        if not analysis_result:
            return "  (品类地图缺失分析不可用)"

        scenario = analysis_result.get("scenario", "B")
        lines = []
        lines.append("[🗺️ 品类地图缺失分析]")

        # 评分总览
        if score:
            lines.append(f"  场景: {'品牌缺失（复用爆品升级评分）' if scenario == 'A' else '品类缺失'}")
            lines.append(f"  评分: {score.total_score}/100 {'★' * score.star_rating}{'☆' * (5 - score.star_rating)} 风险: {score.risk_level}")
            for d in score.dimensions:
                lines.append(f"    {d.name}: {d.score}/{d.max_score} - {d.reason[:80]}")
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

        # CBB匹配详情
        cbb_match = analysis_result.get("cbb_match")
        if cbb_match and hasattr(cbb_match, "module_matches") and cbb_match.module_matches:
            lines.append("")
            lines.append("  ── CBB模块匹配（sub_type级别） ──")
            lines.append(f"  匹配率: {cbb_match.match_rate}% ({cbb_match.matched}/{cbb_match.total})")
            for mm in cbb_match.module_matches[:10]:
                status = "✓" if mm.matched else "✗"
                modules_info = ""
                if mm.cbb_modules:
                    modules_info = " → " + ", ".join(m["cbb_name"] for m in mm.cbb_modules[:2])
                lines.append(f"    [{status}] {mm.vl_module} → {mm.cbb_category}/{mm.cbb_sub_type} [{mm.match_level}]{modules_info}")

        # VL报告摘要
        vl_report = analysis_result.get("vl_report", {})
        if vl_report:
            section3 = vl_report.get("section3_module_comparison", {})
            if section3:
                same = len(section3.get("same_modules", []))
                comp_only = len(section3.get("competitor_only", []))
                own_only = len(section3.get("own_only", []))
                reuse_rate = section3.get("overall_reuse_rate", "N/A")
                lines.append("")
                lines.append(f"  ── VL拆解 ──")
                lines.append(f"  模块对比: 相同{same} / 竞品独有{comp_only} / 自家独有{own_only}, 复用率{reuse_rate}%")

        # 市场概况
        market_overview = analysis_result.get("market_overview", {})
        if market_overview:
            lines.append("")
            lines.append("  ── 品类市场概况 ──")
            total_products = market_overview.get("total_products", 0)
            total_sales = market_overview.get("total_category_sales", 0)
            lines.append(f"  品类产品总数: {total_products}个")
            lines.append(f"  品类累计总销量: {total_sales:,}")

        # 优劣势和建议
        if score:
            if score.strengths:
                lines.append("")
                lines.append("  【优势】")
                for s in score.strengths:
                    lines.append(f"    + {s}")
            if score.weaknesses:
                lines.append("")
                lines.append("  【不足】")
                for w in score.weaknesses:
                    lines.append(f"    - {w}")
            if score.suggestions:
                lines.append("")
                lines.append("  【改进建议】")
                for i, s in enumerate(score.suggestions, 1):
                    lines.append(f"    {i}. {s}")

        return "\n".join(lines)
