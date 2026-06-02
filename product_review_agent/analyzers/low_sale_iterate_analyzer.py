# -*- coding: utf-8 -*-
"""
📉 未起量迭代分析器（V2 — ModuleSplitter + CBBMatcher + LLM评分）

核心问题：为什么没卖好？改什么能起量？

流程：
  1. 数据库检索 → 品类下所有产品 + 已起量/未起量分类 + 市场概况
  2. 图片检索 → 未起量产品图片 + 竞品图片
  3. VL对比拆解（analyze_compare: 自家 vs 竞品）
  4. CBBMatcher（FAISS语义检索）→ 竞品模块匹配
  5. LLM评分（5维度，诊断+一致性占50%）

量化打分（100分制）：
  - 问题诊断 (25分): 没卖好的原因是否找对
  - 迭代-诊断一致性 (25分): 提出的改动能否解决诊断问题
  - 模块复用基础 (20分): 现有CBB模块复用率
  - 增量空间 (15分): 竞品验证 + 市场容量
  - 风险可控度 (15分): 已起量产品冲突 + 新建成本
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


# 未起量迭代评分维度
LOW_SALE_DIMENSIONS = [
    ("问题诊断", 25, "没卖好的原因是否找对"),
    ("迭代-诊断一致性", 25, "提出的改动能否解决诊断问题"),
    ("模块复用基础", 20, "现有CBB模块复用率"),
    ("增量空间", 15, "竞品验证 + 市场容量"),
    ("风险可控度", 15, "已起量产品冲突 + 新建成本"),
]


class LowSaleIterateAnalyzer(BaseAnalyzer):
    """📉 未起量迭代分析器（V2）"""

    analysis_type = "low_sale_iterate"
    display_name = "未起量迭代"
    emoji = "📉"

    SCORING_DIMENSIONS = LOW_SALE_DIMENSIONS

    async def analyze(self, project_data: dict, images: list = None) -> dict:
        """未起量迭代分析主流程。"""
        llm = get_llm_client()
        category_l3 = project_data.get("category_l3") or project_data.get("categoryl3", "")
        category_l2 = project_data.get("category_l2") or project_data.get("categoryl2", "")
        category_l1 = project_data.get("category_l1") or project_data.get("categoryl1", "")
        brand = project_data.get("brand", "")
        competitor_name = project_data.get("competitor_name", "")
        product_name = project_data.get("product_name") or project_data.get("project_name", "")
        product_code = project_data.get("product_code", "")

        # Step 1: 数据库检索 → 品类下所有产品 + 市场概况
        all_products = []
        launched_products = []
        not_launched_products = []
        market_overview = {}
        sales_data = {}

        try:
            with ProductQuery() as pq:
                # 按二级品类+品牌检索所有产品
                all_products = pq.get_products_with_modules(
                    category_l2=category_l2, category_l3=category_l3, brand=brand,
                )

                # 批量获取销量数据
                all_skus = [p.get("product_code", "") for p in all_products]
                sales_data = pq.get_products_sales(all_skus)

                # 标记已起量/未起量
                launched_check = pq.check_product_launched(
                    category_l2=category_l2, brand=brand, threshold=500,
                )
                launched_map = {lc["product_code"]: lc for lc in launched_check}

                for p in all_products:
                    code = p.get("product_code", "")
                    lc = launched_map.get(code, {})
                    p["launched"] = lc.get("launched", False)
                    p["max_sales"] = lc.get("max_sales", 0)
                    if p["launched"]:
                        launched_products.append(p)
                    else:
                        not_launched_products.append(p)

                # 品类市场概况
                market_overview = pq.get_category_market_overview(
                    category_l2=category_l2, category_l3=category_l3,
                )
        except Exception as e:
            logger.error(f"[未起量迭代] 数据库查询异常: {e}")

        # Step 2: 图片检索 → 未起量产品图片 + 竞品图片
        own_images = []
        own_product_code = ""

        # 优先用指定的product_code
        if product_code:
            own_image_tuples = find_product_images(product_code, brand)
            own_images = [data for _, data, _ in own_image_tuples]
            if own_images:
                own_product_code = product_code
                logger.info(f"[未起量迭代] 未起量产品 {product_code} 找到 {len(own_images)} 张图片")

        # 否则取未起量产品中销量最高的
        if not own_images and not_launched_products:
            sorted_products = sorted(
                not_launched_products,
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
                    logger.info(f"[未起量迭代] 未起量产品 {code} 找到 {len(own_images)} 张图片")
                    break

        competitor_images = images or []
        if competitor_images:
            logger.info(f"[未起量迭代] 竞品图片 {len(competitor_images)} 张")

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
            logger.info(f"[未起量迭代] VL对比拆解: 自家{own_product_code} vs {competitor_name or '竞品'}")
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
            logger.info("[未起量迭代] 无自家产品图片，仅VL单拆竞品")
            vl_report = await splitter.analyze_single(
                images=competitor_images,
                product_code=competitor_name or "竞品",
                category_info={**category_info, "brand": "竞品"},
            )
        elif own_images:
            logger.info("[未起量迭代] 无竞品图片，仅VL单拆自家产品")
            vl_report = await splitter.analyze_single(
                images=own_images,
                product_code=own_product_code,
                category_info=category_info,
            )
        else:
            logger.warning("[未起量迭代] 无可用图片，跳过VL拆解")

        if vl_report.get("error"):
            logger.warning(f"[未起量迭代] VL拆解返回错误: {vl_report['error']}")

        # Step 4: CBB模块匹配（整合VL模块+设计要求 → FAISS语义检索）
        cbb_match = MatchResult()

        comparison = vl_report.get("section3_module_comparison", {})
        competitor_only = comparison.get("competitor_only", [])
        all_vl_modules = [m.get("module_name", "") for m in competitor_only if m.get("module_name")]

        # 兼容单拆模式
        if not all_vl_modules:
            b_level = vl_report.get("section2_abc_modules", {}).get("b_level", [])
            all_vl_modules = [m.get("name", "") for m in b_level if m.get("name")]

        logger.info(f"[未起量迭代] VL模块列表({len(all_vl_modules)}个): {all_vl_modules}")

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
            logger.info(f"[未起量迭代] 模块整合: {all_vl_modules} → {target_modules}")

        try:
            with CBBMatcher() as matcher:
                if target_modules:
                    cbb_match = matcher.match_modules(target_modules)
                    logger.info(f"[未起量迭代] CBB匹配: {cbb_match.matched}/{cbb_match.total} 匹配率{cbb_match.match_rate}%")
        except Exception as e:
            logger.error(f"[未起量迭代] CBB匹配异常: {e}")

        # Step 5: 汇总我方所有产品的CBB模块
        our_cbb_modules = []
        seen_cbb = set()
        for p in all_products:
            for m in p.get("modules", []):
                cbb_code = m.get("cbb_code", "")
                if cbb_code and cbb_code not in seen_cbb:
                    seen_cbb.add(cbb_code)
                    our_cbb_modules.append(m)

        # Step 6: LLM评分
        llm_scoring = await self._llm_scoring(
            vl_report, project_data, cbb_match, our_cbb_modules,
            all_products, launched_products, not_launched_products,
            market_overview, sales_data,
        )

        return {
            "analysis_type": "low_sale_iterate",
            "product_code": own_product_code,
            "brand": brand,
            "category_info": category_info,
            "project_data": project_data,
            "vl_report": vl_report,
            "market_overview": market_overview,
            "cbb_match": cbb_match,
            "our_cbb_modules": our_cbb_modules,
            "llm_scoring": llm_scoring,
            "all_products": [
                {
                    "product_code": p.get("product_code", ""),
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
                    "brand": p.get("brand", ""),
                    "max_sales": p["max_sales"],
                    "sales_data": sales_data.get(p["product_code"], []),
                    "modules": p.get("modules", []),
                }
                for p in launched_products
            ],
            "not_launched_products": [
                {
                    "product_code": p["product_code"],
                    "brand": p.get("brand", ""),
                    "sales_data": sales_data.get(p["product_code"], []),
                    "module_count": len(p.get("modules", [])),
                    "modules": p.get("modules", []),
                }
                for p in not_launched_products
            ],
        }

    # ============================================================
    # LLM评分
    # ============================================================

    async def _llm_scoring(
        self, vl_report: dict, project_data: dict,
        cbb_match: MatchResult, our_cbb_modules: list,
        all_products: list, launched_products: list,
        not_launched_products: list, market_overview: dict,
        sales_data: dict,
    ) -> dict:
        """未起量迭代 LLM评分：5维度（诊断25 + 一致性25 + 复用20 + 增量15 + 风险15）"""
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

        # 我方CBB模块
        our_cbb_str = ""
        if our_cbb_modules:
            our_cbb_str = "\n".join(
                f"  - {m.get('cbb_name','')} ({m.get('category','')}/{m.get('sub_type','')})"
                for m in our_cbb_modules[:15]
            )
        else:
            our_cbb_str = "  (我方产品无CBB模块数据)"

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

        # 产品现状
        product_status = f"""我方{len(all_products)}个产品: 已起量{len(launched_products)}个, 未起量{len(not_launched_products)}个"""
        if launched_products:
            product_status += "\n已起量产品:"
            for p in launched_products[:3]:
                product_status += f"\n  - {p['product_code']}: 月最高{p['max_sales']}"
        if not_launched_products:
            product_status += "\n未起量产品:"
            for p in not_launched_products[:5]:
                status = "无销量数据"
                if p.get("sales_data"):
                    max_s = max((s.get("sales_volume", 0) for s in p["sales_data"]), default=0)
                    status = f"月最高{max_s}"
                product_status += f"\n  - {p['product_code']}: {status}"

        # 项目数据
        failure_analysis = project_data.get("failure_analysis", "")
        current_issues = project_data.get("current_issues", "")
        sales_data_desc = project_data.get("sales_data_desc", "")
        design_purpose = project_data.get("design_purpose", "")
        upgrade_modules = project_data.get("upgrade_modules", "")
        upgrade_valiable = project_data.get("upgrade_valiable", "")
        pricing = project_data.get("pricing", "")
        erp_cost = project_data.get("erp_cost", "")
        competitor_price = project_data.get("competitor_price", "")

        prompt = f"""你是资深的电商产品立项审核专家，精通模块化产品分析和供应链评估。

## 任务
这是未起量迭代项目——品类下产品销量均未起量，需要迭代升级寻找起量突破口。
核心问题：为什么没卖好？改什么能起量？

请根据以下信息进行5个维度的评分分析。

## 我方产品现状
{product_status}

## 我方产品已关联的CBB模块
{our_cbb_str}

## 竞品独有模块的CBB匹配结果（FAISS语义检索）
{cbb_match_str}

## VL对比拆解结果（自家 vs 竞品）
{vl_summary}

## 品类市场概况
{market_str or '(无市场数据)'}

## 项目信息
- 没卖好的原因分析: {failure_analysis or '(未填写)'}
- 当前产品具体问题: {current_issues or '(未填写)'}
- 销量现状描述: {sales_data_desc or '(未填写)'}
- 迭代设计目的: {design_purpose or '(未填写)'}
- 具体迭代模块: {upgrade_modules or '(未填写)'}
- 迭代可行性: {upgrade_valiable or '(未填写)'}
- 定价: {pricing or '(未填写)'}
- ERP成本: {erp_cost or '(未填写)'}
- 竞品价格: {competitor_price or '(未填写)'}

---

请严格按以下JSON格式返回，不要加```json```包裹：

{{
  "diagnosis": {{
    "score": 0-25,
    "identified_causes": ["识别出的原因列表"],
    "cause_accuracy": "诊断准确性评估",
    "reason": "问题诊断评语：是否找到没卖好的真正原因，诊断是否全面、有数据支撑"
  }},
  "alignment": {{
    "score": 0-25,
    "cause_action_pairs": [
      {{"cause": "诊断原因", "action": "对应迭代动作", "aligned": true/false}}
    ],
    "reason": "迭代-诊断一致性评语：提出的改动能否解决诊断出的问题"
  }},
  "module_reuse": {{
    "score": 0-20,
    "reuse_ratio": "可复用模块占比",
    "reason": "模块复用基础评语：现有CBB模块复用率、面料/版型复用情况"
  }},
  "increment_space": {{
    "score": 0-15,
    "market_validation": "市场验证情况",
    "reason": "增量空间评语：品类市场容量、竞品销量验证、价格带空间"
  }},
  "risk_control": {{
    "score": 0-15,
    "launched_conflict": "已起量产品冲突评估",
    "new_module_risk": "新建模块风险评估",
    "reason": "风险可控度评语：已起量产品冲突+新建成本+迭代周期"
  }}
}}

## 评分标准

### 问题诊断（25分）— 未起量迭代独有维度
- 诊断触及核心问题（产品力/定价/人群/场景/营销）= 20-25分
- 诊断部分正确但不全面 = 12-19分
- 诊断模糊、笼统或明显错误 = 0-11分
- 有数据支撑的诊断（如"月销50件，竞品月销2000件"）比纯主观判断得分高

### 迭代-诊断一致性（25分）
- 每个诊断问题都有对应的迭代动作 = 20-25分
- 部分问题有对应动作 = 12-19分
- 迭代方向和诊断脱节，改的不是问题所在 = 0-11分
- 关键：如果诊断说"面料差"，迭代模块必须包含面料升级

### 模块复用基础（20分）
- CBB匹配率高 + 现有产品模块覆盖率高 = 16-20分
- 部分可复用 = 10-15分
- 大部分需新建 = 0-9分
- 面料和版型是核心模块，这两项能否复用权重最高

### 增量空间（15分）
- 品类市场有容量 + 竞品有销量验证 = 12-15分
- 市场一般，有部分验证 = 7-11分
- 市场小或无验证 = 0-6分

### 风险可控度（15分）
- 无已起量产品冲突 + 新建模块少 = 12-15分
- 有已起量产品但不冲突，或新建模块适中 = 7-11分
- 已起量产品蚕食风险大，或需大量新建 = 0-6分

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
            logger.error(f"[未起量迭代] LLM评分异常: {e}")
            return {"_error": str(e)}

    # ============================================================
    # 评分
    # ============================================================

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """未起量迭代量化评分：从LLM评分结果中读取5维度分数"""
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

        # 问题诊断（25分）
        diag = llm_scoring.get("diagnosis", {})
        diag_score = min(max(diag.get("score", 12), 0), 25)
        diag_reason = diag.get("reason", "")

        # 迭代-诊断一致性（25分）
        align = llm_scoring.get("alignment", {})
        align_score = min(max(align.get("score", 12), 0), 25)
        align_reason = align.get("reason", "")

        # 模块复用基础（20分）
        mr = llm_scoring.get("module_reuse", {})
        reuse_score = min(max(mr.get("score", 10), 0), 20)
        reuse_reason = mr.get("reason", "")

        # 增量空间（15分）
        inc = llm_scoring.get("increment_space", {})
        inc_score = min(max(inc.get("score", 7), 0), 15)
        inc_reason = inc.get("reason", "")

        # 风险可控度（15分）
        risk = llm_scoring.get("risk_control", {})
        risk_score = min(max(risk.get("score", 7), 0), 15)
        risk_reason = risk.get("reason", "")

        dimensions = [
            DimensionScore("问题诊断", diag_score, 25, diag_reason),
            DimensionScore("迭代-诊断一致性", align_score, 25, align_reason),
            DimensionScore("模块复用基础", reuse_score, 20, reuse_reason),
            DimensionScore("增量空间", inc_score, 15, inc_reason),
            DimensionScore("风险可控度", risk_score, 15, risk_reason),
        ]

        total = sum(d.score for d in dimensions)

        # 生成建议
        suggestions = []
        if diag_score < 15:
            suggestions.append("问题诊断不够深入，建议补充销量数据和竞品对比分析")
        if align_score < 15:
            suggestions.append("迭代方向与诊断脱节，建议确保每个改动都针对具体问题")
        if reuse_score < 10:
            suggestions.append("模块复用基础薄弱，建议优先评估CBB库可复用模块")
        if inc_score < 8:
            suggestions.append("增量空间不足，建议验证品类市场容量和竞品销量")
        if risk_score < 8:
            suggestions.append("迭代风险偏高，建议控制新建模块比例，避免蚕食已起量产品")

        # 已起量产品特别提示
        launched = analysis_result.get("launched_products", [])
        if launched:
            suggestions.append(f"有{len(launched)}个已起量产品，迭代时注意避免蚕食自家份额")

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
        """格式化未起量迭代分析结果"""
        if not analysis_result:
            return "  (未起量迭代分析不可用)"

        lines = []
        lines.append("[📉 未起量迭代分析]")

        # 评分总览
        if score:
            lines.append(f"  评分: {score.total_score}/100 {'★' * score.star_rating}{'☆' * (5 - score.star_rating)} 风险: {score.risk_level}")
            for d in score.dimensions:
                lines.append(f"    {d.name}: {d.score}/{d.max_score} - {d.reason[:80]}")
            lines.append("")

        # 产品现状
        all_prods = analysis_result.get("all_products", [])
        launched = analysis_result.get("launched_products", [])
        not_launched = analysis_result.get("not_launched_products", [])
        lines.append(f"  品类下产品: {len(all_prods)}个 (已起量{len(launched)}个, 未起量{len(not_launched)}个)")

        # 项目诊断信息
        project_data = analysis_result.get("project_data", {})
        if project_data:
            if project_data.get("failure_analysis"):
                lines.append(f"  没卖好的原因: {project_data['failure_analysis'][:100]}")
            if project_data.get("current_issues"):
                lines.append(f"  当前问题: {project_data['current_issues'][:100]}")
            if project_data.get("sales_data_desc"):
                lines.append(f"  销量现状: {project_data['sales_data_desc'][:100]}")
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

            diag = llm_scoring.get("diagnosis", {})
            if diag:
                lines.append(f"  问题诊断: {diag.get('score', 0)}/25")
                if diag.get("identified_causes"):
                    lines.append(f"    识别原因: {', '.join(diag['identified_causes'][:5])}")
                lines.append(f"    {diag.get('reason', '')}")

            align = llm_scoring.get("alignment", {})
            if align:
                lines.append(f"  迭代-诊断一致性: {align.get('score', 0)}/25")
                if align.get("cause_action_pairs"):
                    for pair in align["cause_action_pairs"][:3]:
                        status = "✓" if pair.get("aligned") else "✗"
                        lines.append(f"    [{status}] {pair.get('cause', '')} → {pair.get('action', '')}")
                lines.append(f"    {align.get('reason', '')}")

            mr = llm_scoring.get("module_reuse", {})
            if mr:
                lines.append(f"  模块复用基础: {mr.get('score', 0)}/20 - {mr.get('reason', '')}")

            inc = llm_scoring.get("increment_space", {})
            if inc:
                lines.append(f"  增量空间: {inc.get('score', 0)}/15 - {inc.get('reason', '')}")

            risk = llm_scoring.get("risk_control", {})
            if risk:
                lines.append(f"  风险可控度: {risk.get('score', 0)}/15 - {risk.get('reason', '')}")

        # 改进建议
        if score and score.suggestions:
            lines.append("")
            lines.append("  改进建议:")
            for s in score.suggestions:
                lines.append(f"    > {s}")

        return "\n".join(lines)
