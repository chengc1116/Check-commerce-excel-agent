# -*- coding: utf-8 -*-
"""
⚔️ 竞品升级分析器（V2 — ModuleSplitter + CBBMatcher + LLM评分）

流程：
  1. 数据库检索 → 自家同品类产品 + 市场概况
  2. 图片检索 → 自家产品图片（取销量最高） + 竞品图片（Excel嵌入）
  3. VL对比拆解（analyze_compare: 自家 vs 竞品）
  4. CBBMatcher（FAISS语义检索）→ 竞品独有模块匹配
  5. LLM评分（5维度）

量化打分（100分制）：
  - 模块复用基础 (35分): 自家/竞品相同模块占比 + 竞品独有模块CBB匹配率
  - 卖点复制可行性 (25分): 竞品核心卖点对应模块的可获取性
  - 差异化超越空间 (20分): 自家独有模块 + 竞品弱点 → 超越方向
  - 价格竞争力 (10分): 定价 vs 竞品价格
  - 市场验证 (10分): 竞品销量 + 品类品牌数
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from product_review_agent.agents.llm_client import LLMClient, get_llm_client
from product_review_agent.product_db.product_query import ProductQuery
from product_review_agent.product_db.cbb_matcher import CBBMatcher, MatchResult, extract_target_modules
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


# 竞品升级评分维度
COMPETITOR_UPGRADE_DIMENSIONS = [
    ("模块复用基础", 35, "自家/竞品相同模块占比 + 竞品独有模块CBB匹配率"),
    ("卖点复制可行性", 25, "竞品核心卖点对应模块的可获取性"),
    ("差异化超越空间", 20, "自家独有模块 + 竞品弱点 → 超越方向"),
    ("价格竞争力", 10, "定价 vs 竞品价格偏差"),
    ("市场验证", 10, "竞品销量 + 品类品牌数"),
]


class CompetitorUpgradeAnalyzer(BaseAnalyzer):
    """⚔️ 竞品升级分析器（V2）"""

    analysis_type = "competitor_upgrade"
    display_name = "竞品升级"
    emoji = "⚔️"

    SCORING_DIMENSIONS = COMPETITOR_UPGRADE_DIMENSIONS

    async def analyze(self, project_data: dict, images: list = None) -> dict:
        """竞品升级模块对比分析。"""
        llm = get_llm_client()
        category_l3 = project_data.get("category_l3") or project_data.get("categoryl3", "")
        category_l2 = project_data.get("category_l2") or project_data.get("categoryl2", "")
        category_l1 = project_data.get("category_l1") or project_data.get("categoryl1", "")
        brand = project_data.get("brand", "")
        competitor_name = project_data.get("competitor_name", "")
        product_name = project_data.get("product_name") or project_data.get("project_name", "")
        product_code = project_data.get("product_code", "")

        # Step 1: 数据库检索
        our_products = []
        market_overview = {}
        sales_data = {}

        try:
            with ProductQuery() as pq:
                our_products = pq.get_products_with_modules(
                    category_l2=category_l2, category_l3=category_l3, brand=brand,
                )
                all_skus = [p.get("product_code", "") for p in our_products]
                sales_data = pq.get_products_sales(all_skus)
                market_overview = pq.get_category_market_overview(
                    category_l2=category_l2, category_l3=category_l3,
                )
        except Exception as e:
            logger.error(f"[竞品升级] 数据库查询异常: {e}")

        # Step 2: 图片检索
        own_images = []
        own_product_code = ""

        # 优先用指定的product_code，否则取销量最高的
        if product_code:
            own_image_tuples = find_product_images(product_code, brand)
            own_images = [data for _, data, _ in own_image_tuples]
            if own_images:
                own_product_code = product_code
                logger.info(f"[竞品升级] 自家产品 {product_code} 找到 {len(own_images)} 张图片")

        if not own_images and our_products:
            sorted_products = sorted(
                our_products,
                key=lambda p: max(
                    (s.get("sales_volume", 0) for s in sales_data.get(p.get("product_code", ""), [])),
                    default=0,
                ),
                reverse=True,
            )
            for p in sorted_products[:3]:
                code = p.get("product_code", "")
                imgs = find_product_images(code, brand)
                if imgs:
                    own_images = [data for _, data, _ in imgs[:2]]
                    own_product_code = code
                    logger.info(f"[竞品升级] 销量最高产品 {code} 找到 {len(own_images)} 张图片")
                    break

        competitor_images = images or []
        if competitor_images:
            logger.info(f"[竞品升级] 竞品图片 {len(competitor_images)} 张")

        # Step 3: VL对比拆解
        splitter = ModuleSplitter()
        vl_report = {}
        category_info = {
            "category1": category_l1,
            "category2": category_l2,
            "category3": category_l3,
            "brand": brand,
        }

        if own_images and competitor_images:
            logger.info(f"[竞品升级] VL对比拆解: 自家{own_product_code} vs {competitor_name or '竞品'}")
            vl_report = await splitter.analyze_compare(
                own_images=own_images,
                competitor_images=competitor_images,
                product_code=own_product_code,
                category_info=category_info,
                competitor_desc=competitor_name or "竞品",
                upgrade_direction=project_data.get("upgrade_direction", "") or project_data.get("design_purpose", ""),
                project_data=project_data,
            )
        elif competitor_images:
            logger.info("[竞品升级] 无自家产品图片，仅VL单拆竞品")
            vl_report = await splitter.analyze_single(
                images=competitor_images,
                product_code=competitor_name or "竞品",
                category_info={**category_info, "brand": "竞品"},
            )
        else:
            logger.warning("[竞品升级] 无可用图片，跳过VL拆解")

        if vl_report.get("error"):
            logger.warning(f"[竞品升级] VL拆解返回错误: {vl_report['error']}")

        # Step 4: CBB模块匹配（整合VL模块+设计要求 → FAISS语义检索）
        cbb_match = MatchResult()
        self_cbb_modules = []

        comparison = vl_report.get("section3_module_comparison", {})
        competitor_only = comparison.get("competitor_only", [])
        all_vl_modules = [m.get("module_name", "") for m in competitor_only if m.get("module_name")]

        # 兼容单拆模式
        if not all_vl_modules:
            b_level = vl_report.get("section2_abc_modules", {}).get("b_level", [])
            all_vl_modules = [m.get("name", "") for m in b_level if m.get("name")]

        logger.info(f"[竞品升级] VL模块列表({len(all_vl_modules)}个): {all_vl_modules}")

        # 整合VL模块与设计要求
        upgrade_modules = project_data.get("upgrade_modules", "")
        upgrade_valiable = project_data.get("upgrade_valiable", "")
        design_purpose = project_data.get("design_purpose", "")
        target_modules = await extract_target_modules(
            vl_modules=all_vl_modules,
            design_content=upgrade_modules or design_purpose,
            feasibility_analysis=upgrade_valiable,
        )
        if target_modules != all_vl_modules:
            logger.info(f"[竞品升级] 模块整合: {all_vl_modules} → {target_modules}")

        try:
            with CBBMatcher() as matcher:
                if target_modules:
                    cbb_match = matcher.match_modules(target_modules)
                    logger.info(f"[竞品升级] CBB匹配: {cbb_match.matched}/{cbb_match.total} 匹配率{cbb_match.match_rate}%")
        except Exception as e:
            logger.error(f"[竞品升级] CBB匹配异常: {e}")

        # 获取自家产品CBB模块
        if own_product_code:
            self_cbb_modules = self._get_self_cbb_modules(own_product_code)

        # Step 5: LLM评分
        llm_scoring = await self._llm_scoring(
            vl_report, project_data, cbb_match, self_cbb_modules, market_overview,
        )

        return {
            "analysis_type": "competitor_upgrade",
            "competitor_name": competitor_name,
            "product_code": own_product_code,
            "brand": brand,
            "category_info": category_info,
            "project_data": project_data,
            "vl_report": vl_report,
            "market_overview": market_overview,
            "cbb_match": cbb_match,
            "self_cbb_modules": self_cbb_modules,
            "llm_scoring": llm_scoring,
            "our_products": [
                {
                    "product_code": p.get("product_code", ""),
                    "brand": p.get("brand", ""),
                    "category_l2": p.get("category_l2", category_l2),
                    "category_l3": p.get("category_l3", ""),
                    "sales_data": sales_data.get(p.get("product_code", ""), []),
                    "module_count": len(p.get("modules", [])),
                    "modules": p.get("modules", []),
                }
                for p in our_products
            ],
        }

    def _get_self_cbb_modules(self, product_code: str) -> list[dict]:
        """获取自家产品已关联的CBB模块"""
        if not product_code:
            return []
        try:
            from product_review_agent.product_db.database import ProductDB
            db = ProductDB()
            modules = db._get_product_modules(product_code)
            db.close()
            return modules
        except Exception as e:
            logger.error(f"[竞品升级] 自家CBB模块查询异常: {e}")
            return []

    # ============================================================
    # LLM评分
    # ============================================================

    async def _llm_scoring(
        self, vl_report: dict, project_data: dict,
        cbb_match: MatchResult, self_cbb_modules: list,
        market_overview: dict,
    ) -> dict:
        """竞品升级 LLM评分：5维度（模块复用35 + 卖点复制25 + 差异化20 + 价格10 + 市场10）"""
        llm = get_llm_client()
        if not llm.is_available:
            return {"_error": "LLM不可用"}

        # VL对比结果
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

        # 自家CBB模块
        self_cbb_str = ""
        if self_cbb_modules:
            self_cbb_str = "\n".join(
                f"  - {m.get('cbb_name','')} ({m.get('category','')}/{m.get('sub_type','')})"
                for m in self_cbb_modules
            )
        else:
            self_cbb_str = "  (自家产品无CBB模块数据)"

        # CBB匹配结果
        cbb_match_str = self._format_cbb_match(cbb_match)

        # 市场概况
        market_str = ""
        if market_overview:
            total_products = market_overview.get("total_products", 0)
            total_sales = market_overview.get("total_category_sales", 0)
            brand_dist = market_overview.get("brand_distribution", [])
            market_str = f"品类产品数: {total_products}, 累计销量: {total_sales:,}\n"
            if brand_dist:
                market_str += "品牌分布:\n"
                for bd in brand_dist[:5]:
                    pct = (bd["total_sales"] / total_sales * 100) if total_sales > 0 else 0
                    market_str += f"  - {bd['brand']}: {bd['product_count']}个产品, 销量{bd['total_sales']:,} ({pct:.1f}%)\n"

        # 项目数据
        competitor_strengths_copy = project_data.get("competitor_strengths_copy", "")
        competitor_advantage = project_data.get("competitor_advantage", "")
        product_hotpoint = project_data.get("product_hotpoint", "")
        upgrade_modules = project_data.get("upgrade_modules", "")
        upgrade_valiable = project_data.get("upgrade_valiable", "") or project_data.get("feasibility_analysis", "")
        design_purpose = project_data.get("design_purpose", "")
        pricing = project_data.get("pricing", "")
        erp_cost = project_data.get("erp_cost", "")
        competitor_price = project_data.get("competitor_price", "")

        prompt = f"""你是资深的电商产品立项审核专家，精通模块化产品分析和供应链评估。

## 任务
根据以下信息，对竞品升级项目进行5个维度的评分分析。竞品升级的核心问题是：我方有同品类产品，竞品有更好的卖点，评估能否复制、如何超越。

## 自家产品已关联的CBB模块
{self_cbb_str}

## 竞品独有模块的CBB匹配结果（FAISS语义检索）
{cbb_match_str}

## VL对比拆解结果（自家 vs 竞品）
{vl_summary}

## 品类市场概况
{market_str or '(无市场数据)'}

## 项目信息
- 竞品名称: {project_data.get('competitor_name', '') or '(未填写)'}
- 竞品卖点（需复制）: {competitor_strengths_copy or '(未填写)'}
- 竞品卖点（需超越）: {competitor_advantage or '(未填写)'}
- 自家产品卖点: {product_hotpoint or '(未填写)'}
- 升级模块: {upgrade_modules or '(未填写)'}
- 升级可行性: {upgrade_valiable or '(未填写)'}
- 设计目的: {design_purpose or '(未填写)'}
- 定价: {pricing or '(未填写)'}
- ERP成本: {erp_cost or '(未填写)'}
- 竞品价格: {competitor_price or '(未填写)'}

---

请严格按以下JSON格式返回，不要加```json```包裹：

{{
  "module_reuse": {{
    "score": 0-35,
    "same_module_ratio": "自家和竞品相同模块占比（如60%）",
    "competitor_unique_cbb_rate": "竞品独有模块的CBB匹配率",
    "reason": "模块复用基础评语：相同模块占比、竞品独有模块匹配情况、复用可行性"
  }},
  "copy_feasibility": {{
    "score": 0-25,
    "key_modules_copied": ["竞品核心卖点对应的模块，列出可复制的"],
    "hard_to_copy": ["难以复制的模块及原因"],
    "reason": "卖点复制可行性评语：竞品卖点对应模块的可获取性、CBB匹配情况、开模难度"
  }},
  "differentiation": {{
    "score": 0-20,
    "our_unique_modules": ["自家独有模块（潜在超越点）"],
    "competitor_weaknesses": ["竞品弱点/可超越方向"],
    "reason": "差异化超越空间评语：自家优势模块、竞品弱点、超越方向"
  }},
  "price_competitiveness": {{
    "score": 0-10,
    "reason": "价格竞争力评语：定价vs竞品偏差、成本覆盖度"
  }},
  "market_validation": {{
    "score": 0-10,
    "reason": "市场验证评语：竞品销量表现、品类品牌集中度"
  }}
}}

## 评分标准

### 模块复用基础（35分）
- 自家和竞品相同模块占比：占比越高，升级成本越低。60%以上=28-35分，40-60%=20-27分，40%以下=0-19分
- 竞品独有模块在CBB库中的匹配率：匹配率高说明可复用现有模块，不需要全部新建
- 面料和版型是核心模块，这两项能否复用权重最高

### 卖点复制可行性（25分）
- 竞品核心卖点（需复制的部分）对应到具体模块
- 这些模块在CBB库中有匹配=容易获取（18-25分）
- 需要开模或寻找新供应商=困难（0-12分）
- 结合可行性分析文本判断

### 差异化超越空间（20分）
- 自家独有模块=潜在超越点，数量越多空间越大
- 竞品弱点=可超越方向
- 有明确差异化方向=15-20分，方向模糊=8-14分，无差异化空间=0-7分

### 价格竞争力（10分）
- 定价 vs 竞品价格偏差：±10%内=9-10分，±20%=6-8分，超20%=0-5分

### 市场验证（10分）
- 竞品在该品类有销量=需求验证充分（7-10分）
- 品类品牌数多=市场成熟（加分）

### 重要规则
- 字段标注"(未填写)"的，表示数据缺失而非该项能力差。请给该项中间分（满分的50%-60%），不要给0分。
- 只有当字段已填写但内容明显不佳时，才给低分。
- CBB匹配结果为"无CBB匹配数据"时，表示检索未执行而非模块不可复用，请给该项中间分。"""

        return await self._call_llm(llm, prompt)

    def _format_cbb_match(self, cbb_match: MatchResult) -> str:
        """格式化CBB匹配结果为文本"""
        if not cbb_match or not cbb_match.module_matches:
            return "  (无CBB匹配数据)"

        fabric_matches = []
        pattern_matches = []
        other_matches = []

        for mm in cbb_match.module_matches:
            score_str = f" score={mm.score:.2f}" if mm.score else ""
            modules_info = ""
            if mm.cbb_modules:
                modules_info = " → " + ", ".join(
                    f"{m['cbb_name']}({m['cbb_code']})" for m in mm.cbb_modules[:3]
                )
            line = f"  - {mm.vl_module} → {mm.cbb_category}/{mm.cbb_sub_type} [{mm.match_level}{score_str}]{modules_info}"

            if mm.cbb_category == "FABRIC":
                fabric_matches.append(line)
            elif mm.cbb_category == "PATTERN":
                pattern_matches.append(line)
            else:
                other_matches.append(line)

        lines = [f"总匹配率: {cbb_match.match_rate}% ({cbb_match.matched}/{cbb_match.total})"]

        if fabric_matches:
            lines.append(f"\n【面料 FABRIC】({len(fabric_matches)}个)")
            lines.extend(fabric_matches)
        else:
            lines.append("\n【面料 FABRIC】(无匹配模块)")

        if pattern_matches:
            lines.append(f"\n【版型 PATTERN】({len(pattern_matches)}个)")
            lines.extend(pattern_matches)
        else:
            lines.append("\n【版型 PATTERN】(无匹配模块)")

        if other_matches:
            lines.append(f"\n【其它模块】({len(other_matches)}个)")
            lines.extend(other_matches)

        return "\n".join(lines)

    async def _call_llm(self, llm, prompt: str) -> dict:
        """调用LLM并解析返回的JSON"""
        try:
            result = await llm.acall_text(
                messages=[
                    {"role": "system", "content": "你是电商产品立项审核专家，精通模块化产品分析。只返回JSON。"},
                    {"role": "user", "content": prompt},
                ],
                response_format="json",
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
                    text = "\n".join(json_lines)
                return json.loads(text)
        except Exception as e:
            logger.error(f"[竞品升级] LLM评分异常: {e}")
            return {"_error": str(e)}

    # ============================================================
    # 评分
    # ============================================================

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """竞品升级量化评分：从LLM评分结果中读取5维度分数"""
        llm_scoring = analysis_result.get("llm_scoring", {})
        if not llm_scoring or llm_scoring.get("_error"):
            return AnalyzerScore(
                analysis_type=self.analysis_type,
                dimensions=[
                    DimensionScore(name, 0, max_s, "LLM评分不可用")
                    for name, max_s, _ in self.SCORING_DIMENSIONS
                ],
                total_score=0,
                max_score=100,
                suggestions=["LLM评分不可用，建议检查配置"],
            )

        # 模块复用基础（35分）
        mr = llm_scoring.get("module_reuse", {})
        reuse_score = min(max(mr.get("score", 17), 0), 35)
        reuse_reason = mr.get("reason", "")

        # 卖点复制可行性（25分）
        cf = llm_scoring.get("copy_feasibility", {})
        copy_score = min(max(cf.get("score", 12), 0), 25)
        copy_reason = cf.get("reason", "")

        # 差异化超越空间（20分）
        diff = llm_scoring.get("differentiation", {})
        diff_score = min(max(diff.get("score", 10), 0), 20)
        diff_reason = diff.get("reason", "")

        # 价格竞争力（10分）
        pc = llm_scoring.get("price_competitiveness", {})
        price_score = min(max(pc.get("score", 5), 0), 10)
        price_reason = pc.get("reason", "")

        # 市场验证（10分）
        mv = llm_scoring.get("market_validation", {})
        market_score = min(max(mv.get("score", 5), 0), 10)
        market_reason = mv.get("reason", "")

        dimensions = [
            DimensionScore("模块复用基础", reuse_score, 35, reuse_reason),
            DimensionScore("卖点复制可行性", copy_score, 25, copy_reason),
            DimensionScore("差异化超越空间", diff_score, 20, diff_reason),
            DimensionScore("价格竞争力", price_score, 10, price_reason),
            DimensionScore("市场验证", market_score, 10, market_reason),
        ]

        total = sum(d.score for d in dimensions)

        # 生成建议
        suggestions = []
        if reuse_score < 20:
            suggestions.append("模块复用基础薄弱，自家和竞品差异大，升级成本高")
        if copy_score < 15:
            suggestions.append("竞品核心卖点难以复制，建议聚焦可实现的升级方向")
        if diff_score < 10:
            suggestions.append("差异化超越空间有限，需从设计/品牌层面寻找突破点")
        if price_score < 5:
            suggestions.append("价格竞争力不足，升级后定价需谨慎")

        return AnalyzerScore(
            analysis_type=self.analysis_type,
            dimensions=dimensions,
            total_score=total,
            max_score=100,
            suggestions=suggestions,
        )

    # ============================================================
    # 报告格式化
    # ============================================================

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
                lines.append(f"    {d.name}: {d.score}/{d.max_score} - {d.reason[:80]}")
            lines.append("")

        # 竞品信息
        competitor_name = analysis_result.get("competitor_name", "未知竞品")
        project_data = analysis_result.get("project_data", {})
        lines.append(f"  ── 目标竞品: {competitor_name} ──")
        if project_data.get("competitor_strengths_copy"):
            lines.append(f"  竞品卖点(复制): {project_data['competitor_strengths_copy']}")
        if project_data.get("competitor_advantage"):
            lines.append(f"  竞品卖点(超越): {project_data['competitor_advantage']}")
        lines.append("")

        # 市场概况
        market_overview = analysis_result.get("market_overview", {})
        if market_overview:
            total_products = market_overview.get("total_products", 0)
            total_sales = market_overview.get("total_category_sales", 0)
            brand_dist = market_overview.get("brand_distribution", [])
            lines.append(f"  ── 品类市场概况 ──")
            lines.append(f"  品类产品总数: {total_products}个 | 累计总销量: {total_sales:,}")
            if brand_dist:
                lines.append("  品牌份额:")
                for bd in brand_dist[:5]:
                    pct = (bd["total_sales"] / total_sales * 100) if total_sales > 0 else 0
                    lines.append(f"    • {bd['brand']}: {bd['product_count']}个产品, 销量{bd['total_sales']:,} ({pct:.1f}%)")
            lines.append("")

        # CBB匹配详情
        cbb_match = analysis_result.get("cbb_match")
        if cbb_match and hasattr(cbb_match, "module_matches") and cbb_match.module_matches:
            lines.append("  ── CBB模块匹配（FAISS语义检索） ──")
            lines.append(f"  匹配率: {cbb_match.match_rate}% ({cbb_match.matched}/{cbb_match.total})")
            for mm in cbb_match.module_matches[:10]:
                status = "✓" if mm.matched else "✗"
                score_str = f" score={mm.score:.2f}" if mm.score else ""
                modules_info = ""
                if mm.cbb_modules:
                    modules_info = " → " + ", ".join(m["cbb_name"] for m in mm.cbb_modules[:2])
                lines.append(f"    [{status}] {mm.vl_module} → {mm.cbb_category}/{mm.cbb_sub_type} [{mm.match_level}{score_str}]{modules_info}")
            lines.append("")

        # LLM评分详情
        llm_scoring = analysis_result.get("llm_scoring", {})
        if llm_scoring and not llm_scoring.get("_error"):
            lines.append("  ── 评分详情 ──")

            mr = llm_scoring.get("module_reuse", {})
            if mr:
                lines.append(f"  模块复用基础: {mr.get('score', 0)}/35")
                lines.append(f"    相同模块占比: {mr.get('same_module_ratio', 'N/A')}")
                lines.append(f"    竞品独有CBB匹配率: {mr.get('competitor_unique_cbb_rate', 'N/A')}")
                lines.append(f"    {mr.get('reason', '')}")

            cf = llm_scoring.get("copy_feasibility", {})
            if cf:
                lines.append(f"  卖点复制可行性: {cf.get('score', 0)}/25")
                if cf.get("key_modules_copied"):
                    lines.append(f"    可复制模块: {', '.join(cf['key_modules_copied'][:5])}")
                if cf.get("hard_to_copy"):
                    lines.append(f"    难复制模块: {', '.join(cf['hard_to_copy'][:5])}")
                lines.append(f"    {cf.get('reason', '')}")

            diff = llm_scoring.get("differentiation", {})
            if diff:
                lines.append(f"  差异化超越空间: {diff.get('score', 0)}/20")
                if diff.get("our_unique_modules"):
                    lines.append(f"    自家独有: {', '.join(diff['our_unique_modules'][:5])}")
                if diff.get("competitor_weaknesses"):
                    lines.append(f"    竞品弱点: {', '.join(diff['competitor_weaknesses'][:5])}")
                lines.append(f"    {diff.get('reason', '')}")

            pc = llm_scoring.get("price_competitiveness", {})
            if pc:
                lines.append(f"  价格竞争力: {pc.get('score', 0)}/10 - {pc.get('reason', '')}")

            mv = llm_scoring.get("market_validation", {})
            if mv:
                lines.append(f"  市场验证: {mv.get('score', 0)}/10 - {mv.get('reason', '')}")

        # 改进建议
        if score and score.suggestions:
            lines.append("")
            lines.append("  改进建议:")
            for s in score.suggestions:
                lines.append(f"    > {s}")

        return "\n".join(lines)
