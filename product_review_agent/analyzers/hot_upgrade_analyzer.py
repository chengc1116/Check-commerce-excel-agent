# -*- coding: utf-8 -*-
"""
🔥 爆品升级分析器（V5 — CBBMatcher sub_type级别匹配 + LLM全维度评分）

流程：
  1. 解析立项表Excel → 获取自家货号、升级方向、竞品图片等
  2. 自家货号 → 检索商品图片
  3. 调用ModuleSplitter对比拆解（自家+竞品分别VL拆解，异步并行）
  4. CBBMatcher: 竞品独有模块 → sub_type级别匹配（LLM映射+CBB库验证）
  5. 获取自家产品CBB模块
  6. LLM全维度评分（模块复用+升级合理性+价格+营销+可行性）

量化打分（100分制）：
  - 模块复用 (48分): CBBMatcher匹配结果 + VL对比覆盖率
  - 模块升级合理性 (22分): 目标-动作一致性 + 卖点保留度 + 升级必要性
  - 价格分析 (10分): 定价竞争力 + 成本-定价匹配度
  - 营销分析 (10分): 升级卖点的营销价值
  - 可行性分析 (10分): 供应链/打样可行性
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from product_review_agent.agents.llm_client import LLMClient, get_llm_client
from product_review_agent.product_db.product_query import ProductQuery
from product_review_agent.product_db.cbb_matcher import CBBMatcher, MatchResult, extract_target_modules
from product_review_agent.vl_module_splitter import ModuleSplitter, find_product_images
from product_review_agent.analyzers.base import (
    BaseAnalyzer,
    AnalyzerScore,
    DimensionScore,
    format_product_detail,
)

logger = logging.getLogger(__name__)


class HotUpgradeAnalyzer(BaseAnalyzer):
    """🔥 爆品升级分析器（V3 — 5维度评分）"""

    analysis_type = "hot_upgrade"
    display_name = "爆品升级"
    emoji = "🔥"

    SCORING_DIMENSIONS = [
        ("模块复用", 48, "核心/非核心模块复用 + 升级方向匹配 + 市场验证"),
        ("模块升级合理性", 22, "目标-动作一致性 + 卖点保留度 + 升级必要性"),
        ("价格分析", 10, "价格定位合理性 + 成本-定价匹配度"),
        ("营销分析", 10, "升级卖点的营销价值"),
        ("可行性分析", 10, "供应链/打样可行性"),
    ]

    async def analyze(self, project_data: dict, images: list = None) -> dict:
        """
        爆品升级分析主流程

        Args:
            project_data: 立项表解析后的结构化数据，包含：
                - product_code: 自家产品货号（如 HY63）
                - brand: 品牌（如 SERUNA）
                - category1/category2/category3: 品类
                - upgrade_direction: 具体升级方向
                - upgrade_modules: 具体升级模块
                - upgrade_function: 升级功能
                - design_purpose: 设计目的
                - price_margin: 价格/毛利
                - erp_cost: ERP成本
                - target_audience: 目标人群
                - target_scenario: 目标场景
                - is_new_category: 是否新品类
            images: 竞品图片bytes列表（从Excel提取）
        """
        product_code = project_data.get("product_code", "")
        brand = project_data.get("brand", "SERUNA")
        # 兼容两种字段名：category1 和 categoryl1
        category1 = project_data.get("category1", "") or project_data.get("categoryl1", "")
        category2 = project_data.get("category2", "") or project_data.get("categoryl2", "")
        category3 = project_data.get("category3", "") or project_data.get("categoryl3", "")
        upgrade_direction = project_data.get("upgrade_direction", "")
        upgrade_modules = project_data.get("upgrade_modules", "")
        upgrade_function = project_data.get("upgrade_function", "")
        design_purpose = project_data.get("design_purpose", "")
        price_margin = project_data.get("price_margin", "")
        erp_cost = project_data.get("erp_cost", "")
        pricing = project_data.get("pricing", "")
        product_price = project_data.get("product_price", "")
        product_hotpoint = project_data.get("product_hotpoint", "")
        upgrade_valiable = project_data.get("upgrade_valiable", "")
        competitor_price = project_data.get("competitor_price", "")

        category_info = {
            "category1": category1,
            "category2": category2,
            "category3": category3,
            "brand": brand,
        }

        # Step 1: 检索自家产品图片
        own_images = []
        if product_code:
            own_image_tuples = find_product_images(product_code, brand)
            own_images = [data for _, data, _ in own_image_tuples]
            if own_images:
                logger.info(f"[爆品升级] 自家产品 {product_code} 找到 {len(own_images)} 张图片")
            else:
                logger.warning(f"[爆品升级] 自家产品 {product_code} 未找到图片")

        # Step 2: 竞品图片
        competitor_images = images or []
        if competitor_images:
            logger.info(f"[爆品升级] 竞品图片 {len(competitor_images)} 张")
        else:
            logger.warning("[爆品升级] 无竞品图片")

        # Step 3: 调用ModuleSplitter进行对比拆解
        splitter = ModuleSplitter()
        vl_report = {}

        if own_images and competitor_images:
            # 有自家图片+竞品图片 → 对比模式
            logger.info("[爆品升级] 模式: VL对比拆解（自家+竞品）")
            vl_report = await splitter.analyze_compare(
                own_images=own_images,
                competitor_images=competitor_images,
                product_code=product_code,
                category_info=category_info,
                competitor_desc="竞品借鉴产品",
                upgrade_direction=upgrade_direction,
                project_data={
                    "upgrade_direction": upgrade_direction,
                    "upgrade_modules": upgrade_modules,
                    "upgrade_function": upgrade_function,
                    "design_purpose": design_purpose,
                    "price_margin": price_margin,
                    "erp_cost": erp_cost,
                },
            )
        elif own_images:
            # 只有自家图片 → 单商品模式
            logger.info("[爆品升级] 模式: 单商品拆解（仅自家）")
            vl_report = await splitter.analyze_single(
                images=own_images,
                product_code=product_code,
                category_info=category_info,
            )
        elif competitor_images:
            # 只有竞品图片 → 单商品拆解
            logger.info("[爆品升级] 模式: 单商品拆解（仅竞品）")
            vl_report = await splitter.analyze_single(
                images=competitor_images,
                product_code=product_code or "竞品",
                category_info=category_info,
            )
        else:
            logger.warning("[爆品升级] 无任何图片，跳过VL拆解")
            vl_report = {"product_code": product_code, "error": "无图片数据"}

        # Step 4: 获取品类市场概况（作为补充信息）
        market_overview = {}
        try:
            with ProductQuery() as pq:
                market_overview = pq.get_category_market_overview(
                    category_l2=category2,
                    category_l3=category3,
                )
        except Exception as e:
            logger.error(f"[爆品升级] 市场概况查询异常: {e}")

        # Step 5: CBB模块匹配（整合VL模块+设计要求 → FAISS语义检索）
        cbb_match = MatchResult()
        self_cbb_modules = []

        comparison = vl_report.get("section3_module_comparison", {})
        competitor_only = comparison.get("competitor_only", [])
        vl_module_names = [m.get("module_name", "") for m in competitor_only if m.get("module_name")]

        # 整合VL模块与设计要求
        target_modules = await extract_target_modules(
            vl_modules=vl_module_names,
            design_content=upgrade_direction or design_purpose,
            upgrade_modules=upgrade_modules,
            feasibility_analysis=upgrade_valiable,
        )
        if target_modules != vl_module_names:
            logger.info(f"[爆品升级] 模块整合: {vl_module_names} → {target_modules}")

        try:
            with CBBMatcher() as matcher:
                if target_modules:
                    cbb_match = matcher.match_modules(target_modules)
                    logger.info(f"[爆品升级] CBB匹配: {cbb_match.matched}/{cbb_match.total} 匹配率{cbb_match.match_rate}%")
        except Exception as e:
            logger.error(f"[爆品升级] CBB匹配异常: {e}")

        # 获取自家产品CBB模块
        self_cbb_modules = self._get_self_cbb_modules(product_code)

        # Step 6: LLM 全维度评分
        full_project_data = {
            "upgrade_direction": upgrade_direction,
            "upgrade_modules": upgrade_modules,
            "upgrade_function": upgrade_function,
            "design_purpose": design_purpose,
            "price_margin": price_margin,
            "erp_cost": erp_cost,
            "pricing": pricing,
            "product_price": product_price,
            "product_hotpoint": product_hotpoint,
            "upgrade_valiable": upgrade_valiable,
            "competitor_price": competitor_price,
            "target_audience": project_data.get("people_analysis", ""),
            "target_scenario": project_data.get("scene_analysis", ""),
            "is_new_category": project_data.get("is_new_category", "否"),
        }
        llm_scoring = await self._llm_full_scoring(
            vl_report, full_project_data, self_cbb_modules, cbb_match
        )

        return {
            "analysis_type": "hot_upgrade",
            "product_code": product_code,
            "brand": brand,
            "category_info": category_info,
            "project_data": full_project_data,
            "vl_report": vl_report,
            "market_overview": market_overview,
            "cbb_match": cbb_match,
            "self_cbb_modules": self_cbb_modules,
            "llm_scoring": llm_scoring,
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
            logger.error(f"[爆品升级] 自家CBB模块查询异常: {e}")
            return []

    async def _llm_full_scoring(self, vl_report: dict, project_data: dict,
                                 self_cbb_modules: list,
                                 cbb_match: MatchResult) -> dict:
        """LLM 全维度评分：模块复用 + 升级合理性 + 价格 + 营销 + 可行性"""
        llm = get_llm_client()
        if not llm.is_available:
            return {"_error": "LLM不可用"}

        # 构建 VL 对比结果摘要
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

        # 构建自家产品 CBB 模块摘要
        self_cbb_str = ""
        if self_cbb_modules:
            self_cbb_str = "\n".join(
                f"  - {m.get('cbb_name','')} ({m.get('category','')}/{m.get('sub_type','')})"
                for m in self_cbb_modules
            )
        else:
            self_cbb_str = "  (自家产品无CBB模块数据)"

        # 构建 CBB 匹配结果摘要（FAISS语义检索）
        cbb_match_str = ""
        if cbb_match and cbb_match.module_matches:
            cbb_match_str = f"匹配率: {cbb_match.match_rate}% ({cbb_match.matched}/{cbb_match.total})\n"
            for mm in cbb_match.module_matches:
                score_str = f"score={mm.score:.2f}" if mm.score else ""
                modules_info = ""
                if mm.cbb_modules:
                    modules_info = " → " + ", ".join(
                        f"{m['cbb_name']}({m['cbb_code']})" for m in mm.cbb_modules[:3]
                    )
                cbb_match_str += f"  - {mm.vl_module} → {mm.cbb_category}/{mm.cbb_sub_type} [{mm.match_level} {score_str}]{modules_info}\n"
        else:
            cbb_match_str = "  (无CBB匹配数据)"

        # 项目数据
        design_purpose = project_data.get("design_purpose", "")
        upgrade_modules = project_data.get("upgrade_modules", "")
        product_hotpoint = project_data.get("product_hotpoint", "")
        upgrade_valiable = project_data.get("upgrade_valiable", "")
        pricing = project_data.get("pricing", "")
        erp_cost = project_data.get("erp_cost", "")
        competitor_price = project_data.get("competitor_price", "")

        prompt = f"""你是资深的电商产品立项审核专家，精通模块化产品分析和供应链评估。

## 任务
根据以下信息，对爆品升级项目进行5个维度的评分分析。

## 自家产品已关联的CBB模块
{self_cbb_str}

## 竞品独有模块的CBB匹配结果（FAISS语义检索）
{cbb_match_str}

## VL对比拆解结果（自家 vs 竞品）
{vl_summary}

## 项目信息
- 升级方向: {project_data.get('upgrade_direction', '') or '(未填写)'}
- 升级模块: {upgrade_modules or '(未填写)'}
- 升级功能: {project_data.get('upgrade_function', '') or '(未填写)'}
- 升级目的: {design_purpose or '(未填写)'}
- 原有卖点: {product_hotpoint or '(未填写)'}
- 可行性说明: {upgrade_valiable or '(未填写)'}
- 定价: {pricing or '(未填写)'}
- ERP成本: {erp_cost or '(未填写)'}
- 竞品价格: {competitor_price or '(未填写)'}
- 是否新品类: {project_data.get('is_new_category', '否')}

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
    "reason": "模块复用总评语（含sub_type匹配率、核心分类覆盖率、缺失分类影响、与升级方向的关系）"
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
  "price_analysis": {{
    "score": 0-10,
    "reason": "价格定位合理性 + 成本-定价匹配度分析"
  }},
  "marketing": {{
    "score": 0-10,
    "reason": "升级卖点的营销价值分析"
  }},
  "feasibility": {{
    "score": 0-10,
    "reason": "供应链/打样可行性分析"
  }}
}}

## 评分标准

### 模块复用（48分）
- 参考上方"竞品独有模块的CBB匹配结果"，已匹配到CBB sub_type的模块=可复用，未匹配=需新建
- 同时考虑VL对比中"相同模块"（自家和竞品都有的模块）= 直接复用
- 核心分类覆盖率高=高分，简单产品模块少是正常的，按覆盖率比例打分
- 自家CBB模块和竞品模块的重合度是关键指标

### 升级合理性（22分）
- 目标-动作一致性(8分): 升级目标是否明确、动作能否支撑
- 卖点保留度(8分): 升级后是否保留原有卖点
- 升级必要性(6分): 目标→动作逻辑链是否合理

### 价格分析（10分）
- 定价 vs 竞品价格的竞争力
- 升级模块推断的成本变化 vs 定价是否合理覆盖

### 营销分析（10分）
- 升级点能否转化为营销话术/口碑优势

### 可行性分析（10分）
- 供应链/打样可行性，模块可获取性（结合CBB匹配结果：已匹配的模块可获取性高）

### 重要规则
- 字段标注"(未填写)"的，表示数据缺失而非该项能力差。请给该项中间分（满分的50%-60%），不要给0分。
- 只有当字段已填写但内容明显不佳时，才给低分。
- CBB匹配结果为"无CBB匹配数据"时，表示检索未执行而非模块不可复用，请给该项中间分。"""

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
            logger.error(f"[爆品升级] LLM全维度评分异常: {e}")
            return {"_error": str(e)}

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """爆品升级量化打分（V4 — 5维度全由LLM评分，总分100）"""
        llm_scoring = analysis_result.get("llm_scoring", {})
        vl_report = analysis_result.get("vl_report", {})
        comparison = vl_report.get("section3_module_comparison", {})
        same_modules = comparison.get("same_modules", [])
        competitor_only = comparison.get("competitor_only", [])
        own_only = comparison.get("own_only", [])

        if llm_scoring.get("_error"):
            # LLM 不可用时返回基础分
            return AnalyzerScore(
                analysis_type=self.analysis_type,
                dimensions=[
                    DimensionScore("模块复用", 24, 48, f"LLM不可用: {llm_scoring['_error']}"),
                    DimensionScore("模块升级合理性", 11, 22, "LLM不可用，按基础分评估"),
                    DimensionScore("价格分析", 5, 10, "LLM不可用，按基础分评估"),
                    DimensionScore("营销分析", 5, 10, "LLM不可用，按基础分评估"),
                    DimensionScore("可行性分析", 5, 10, "LLM不可用，按基础分评估"),
                ],
                total_score=50,
                max_score=100,
                strengths=[],
                weaknesses=["LLM评分不可用，所有维度按基础分评估"],
                suggestions=["建议检查LLM服务状态后重新评估"],
            )

        # ==================== 维度1: 模块复用 (48分) ====================
        mr = llm_scoring.get("module_reuse", {})
        reuse_score = min(max(mr.get("score", 24), 0), 48)
        reuse_reason = mr.get("reason", "")
        # 补充映射信息到评语
        mapping = mr.get("vl_to_cbb_mapping", [])
        if mapping:
            mapping_summary = ", ".join(
                f"{m.get('vl_module','')}→{m.get('cbb_category','')}"
                for m in mapping[:6]
            )
            reuse_reason = f"模块映射: {mapping_summary}\n{reuse_reason}"
        missing = mr.get("missing_categories", [])
        if missing:
            reuse_reason += f"\n缺失分类: {', '.join(missing)}"

        # ==================== 维度2: 模块升级合理性 (22分) ====================
        ur = llm_scoring.get("upgrade_rationality", {})
        if ur and not ur.get("_error"):
            goal_action_score = ur.get("goal_action_score", 4)
            hotpoint_preserve_score = ur.get("hotpoint_preserve_score", 4)
            necessity_score = ur.get("necessity_score", 3)
            upgrade_rationality_score = goal_action_score + hotpoint_preserve_score + necessity_score
            ga_reason = ur.get("goal_action_reason", "")[:80]
            hp_reason = ur.get("hotpoint_preserve_reason", "")[:80]
            ne_reason = ur.get("necessity_reason", "")[:80]
            upgrade_rationality_reason = (f"目标-动作{goal_action_score}/8: {ga_reason}\n"
                                          f"    卖点保留{hotpoint_preserve_score}/8: {hp_reason}\n"
                                          f"    升级必要性{necessity_score}/6: {ne_reason}")
        else:
            upgrade_rationality_score = 11
            upgrade_rationality_reason = "LLM评分不可用，按基础分评估"

        # ==================== 维度3: 价格分析 (10分) ====================
        pa = llm_scoring.get("price_analysis", {})
        if pa and not pa.get("_error"):
            price_score = min(max(pa.get("score", 5), 0), 10)
            price_reason = pa.get("reason", "")[:120]
        else:
            price_score = 5
            price_reason = "LLM评分不可用，按基础分评估"

        # ==================== 维度4: 营销分析 (10分) ====================
        mk = llm_scoring.get("marketing", {})
        if mk and not mk.get("_error"):
            marketing_score = min(max(mk.get("score", 5), 0), 10)
            marketing_reason = mk.get("reason", "")[:120]
        else:
            marketing_score = 5
            marketing_reason = "LLM评分不可用，按基础分评估"

        # ==================== 维度5: 可行性分析 (10分) ====================
        fe = llm_scoring.get("feasibility", {})
        if fe and not fe.get("_error"):
            feasibility_score = min(max(fe.get("score", 5), 0), 10)
            feasibility_reason = fe.get("reason", "")[:120]
        else:
            feasibility_score = 5
            feasibility_reason = "LLM评分不可用，按基础分评估"

        # ==================== 汇总 ====================
        dimensions = [
            DimensionScore("模块复用", reuse_score, 48, reuse_reason),
            DimensionScore("模块升级合理性", upgrade_rationality_score, 22, upgrade_rationality_reason),
            DimensionScore("价格分析", price_score, 10, price_reason),
            DimensionScore("营销分析", marketing_score, 10, marketing_reason),
            DimensionScore("可行性分析", feasibility_score, 10, feasibility_reason),
        ]

        total = sum(d.score for d in dimensions)

        # 自动生成建议
        suggestions = []
        if reuse_score < 24:
            suggestions.append("模块复用覆盖率低，需评估新建模块的成本和周期")
        if missing:
            suggestions.append(f"缺失CBB分类「{'、'.join(missing[:3])}」，建议补齐")
        if upgrade_rationality_score < 14:
            suggestions.append("升级合理性存疑，建议重新审视升级目标与动作的逻辑链")
        if price_score < 6:
            suggestions.append("价格与成本匹配度不足，建议重新评估定价策略")
        if marketing_score < 6:
            suggestions.append("升级卖点营销价值不明显，建议提炼差异化营销话术")
        if feasibility_score < 6:
            suggestions.append("供应链可行性存疑，建议提前确认模块可获取性")

        # 优劣势
        strengths = []
        weaknesses = []
        if reuse_score >= 38:
            strengths.append("模块复用覆盖率高，开发成本低")
        if reuse_score >= 24:
            missing_cats = mr.get("missing_categories", [])
            if not missing_cats:
                strengths.append("CBB分类全覆盖，无明显模块缺口")
        if upgrade_rationality_score >= 18:
            strengths.append("升级逻辑清晰，原有卖点保留完好")
        if price_score >= 8:
            strengths.append("价格定位合理，成本控制良好")
        if marketing_score >= 8:
            strengths.append("升级卖点有明确营销价值")
        if feasibility_score >= 8:
            strengths.append("供应链可行性高")

        if reuse_score < 16:
            weaknesses.append("模块复用覆盖率严重不足，需大量新建")
        if missing:
            weaknesses.append(f"缺失CBB分类: {', '.join(missing[:3])}")
        if upgrade_rationality_score < 12:
            weaknesses.append("升级逻辑不清晰或原有卖点损失")
        if price_score < 4:
            weaknesses.append("定价与成本不匹配")
        if marketing_score < 4:
            weaknesses.append("升级缺乏营销卖点支撑")
        if feasibility_score < 4:
            weaknesses.append("供应链可行性不足")

        for m in own_only:
            if isinstance(m, dict) and m.get("is_advantage"):
                strengths.append(f"自家独有优势: {m.get('module_name', m.get('name', ''))}")
        for m in competitor_only:
            if isinstance(m, dict):
                weaknesses.append(f"竞品独有: {m.get('module_name', m.get('name', ''))}")

        return AnalyzerScore(
            analysis_type=self.analysis_type,
            dimensions=dimensions,
            total_score=total,
            max_score=100,
            strengths=strengths[:5],
            weaknesses=weaknesses[:5],
            suggestions=suggestions,
        )

    def format_report(self, analysis_result: dict, score: AnalyzerScore = None) -> str:
        """格式化爆品升级分析结果（V3 — 5维度，含评语和建议）"""
        if not analysis_result:
            return "  (爆品升级分析不可用)"

        lines = []

        # ── 评分总览 ──
        if score:
            lines.append(f"  综合评分: {score.total_score}/100 {'★' * score.star_rating + '☆' * (5 - score.star_rating)}  风险等级: {score.risk_level}")
            lines.append("")
            for d in score.dimensions:
                bar_len = int(d.score / d.max_score * 20)
                bar = "█" * bar_len + "░" * (20 - bar_len)
                lines.append(f"  {d.name}: {d.score}/{d.max_score}  [{bar}]")
            lines.append("")

        # ── 立项基本信息 ──
        project_data = analysis_result.get("project_data", {})
        if project_data:
            lines.append("  ── 立项信息 ──")
            lines.append(f"  自家产品: {analysis_result.get('product_code', '?')} ({analysis_result.get('brand', '?')})")
            cat = analysis_result.get("category_info", {})
            lines.append(f"  品类: {cat.get('category1','')} > {cat.get('category2','')} > {cat.get('category3','')}")
            if project_data.get("upgrade_modules"):
                lines.append(f"  升级模块: {project_data['upgrade_modules']}")
            if project_data.get("design_purpose"):
                lines.append(f"  升级目的: {project_data['design_purpose']}")
            if project_data.get("product_hotpoint"):
                lines.append(f"  原有卖点: {project_data['product_hotpoint']}")
            if project_data.get("upgrade_valiable"):
                lines.append(f"  可行性说明: {project_data['upgrade_valiable']}")
            lines.append("")

        # ── 维度1: 模块复用 ──
        lines.append("  ═══ 维度一：模块复用（48分）═══")
        if score:
            d_reuse = next((d for d in score.dimensions if d.name == "模块复用"), None)
            if d_reuse:
                for sub_line in d_reuse.reason.split("\n"):
                    lines.append(f"  {sub_line.strip()}")
        lines.append("")

        # CBB匹配详情（FAISS语义检索）
        cbb_match = analysis_result.get("cbb_match")
        if cbb_match and hasattr(cbb_match, "module_matches") and cbb_match.module_matches:
            lines.append("  VL模块→CBB匹配 (FAISS语义检索):")
            lines.append(f"  匹配率: {cbb_match.match_rate}% ({cbb_match.matched}/{cbb_match.total})")
            for mm in cbb_match.module_matches[:10]:
                status = "✓" if mm.matched else "✗"
                score_str = f" score={mm.score:.2f}" if mm.score else ""
                modules_info = ""
                if mm.cbb_modules:
                    modules_info = " → " + ", ".join(m["cbb_name"] for m in mm.cbb_modules[:2])
                lines.append(f"    [{status}] {mm.vl_module} → {mm.cbb_category}/{mm.cbb_sub_type} [{mm.match_level}{score_str}]{modules_info}")
            lines.append("")

        # LLM模块映射详情
        llm_scoring = analysis_result.get("llm_scoring", {})
        mr = llm_scoring.get("module_reuse", {})
        if mr and not mr.get("_error"):
            mapping = mr.get("vl_to_cbb_mapping", [])
            if mapping:
                lines.append("  LLM模块映射:")
                for m in mapping[:10]:
                    sub_type = m.get("cbb_sub_type", "")
                    matched = "✓" if m.get("matched") else ""
                    lines.append(f"    {m.get('vl_module', '?')} → {m.get('cbb_category', '?')}/{sub_type} ({m.get('side', '')}) {matched}")
                lines.append("")
            core_cats = mr.get("core_categories", [])
            if core_cats:
                lines.append(f"  核心CBB分类: {', '.join(core_cats)}")
            reuse_cats = mr.get("reuse_categories", [])
            if reuse_cats:
                lines.append(f"  可复用分类: {', '.join(reuse_cats)}")
            missing_cats = mr.get("missing_categories", [])
            if missing_cats:
                lines.append(f"  缺失分类: {', '.join(missing_cats)}")
            unique_cats = mr.get("unique_categories", [])
            if unique_cats:
                lines.append(f"  自家独有分类: {', '.join(unique_cats)}")
            lines.append("")

        # VL对比详情
        vl_report = analysis_result.get("vl_report", {})
        if vl_report and "error" not in vl_report:
            comparison = vl_report.get("section3_module_comparison", {})
            if comparison:
                same = comparison.get("same_modules", [])
                comp_only = comparison.get("competitor_only", [])
                own_only = comparison.get("own_only", [])

                if same:
                    lines.append(f"  可复用模块（{len(same)}个）:")
                    for m in same[:8]:
                        name = m.get("module_name", "?")
                        own = str(m.get("own_detail", ""))[:40]
                        comp = str(m.get("competitor_detail", ""))[:40]
                        lines.append(f"    · {name}  —  自家: {own} | 竞品: {comp}")
                if comp_only:
                    lines.append(f"  竞品独有模块（{len(comp_only)}个，需补齐）:")
                    for m in comp_only[:6]:
                        name = m.get("module_name", "?")
                        detail = str(m.get("detail", ""))[:50]
                        lines.append(f"    · {name}  —  {detail}")
                if own_only:
                    lines.append(f"  自家独有模块（{len(own_only)}个，差异化优势）:")
                    for m in own_only[:4]:
                        name = m.get("module_name", "?")
                        detail = str(m.get("detail", ""))[:50]
                        adv = " ✓优势" if m.get("is_advantage") else ""
                        lines.append(f"    · {name}  —  {detail}{adv}")
                lines.append("")

        # ── 维度2: 模块升级合理性 ──
        lines.append("  ═══ 维度二：模块升级合理性（22分）═══")
        if score:
            d_upgrade = next((d for d in score.dimensions if d.name == "模块升级合理性"), None)
            if d_upgrade:
                for sub_line in d_upgrade.reason.split("\n"):
                    lines.append(f"  {sub_line.strip()}")
        lines.append("")

        # ── 维度3: 价格分析 ──
        lines.append("  ═══ 维度三：价格分析（10分）═══")
        if score:
            d_price = next((d for d in score.dimensions if d.name == "价格分析"), None)
            if d_price:
                for sub_line in d_price.reason.split("\n"):
                    lines.append(f"  {sub_line.strip()}")
        lines.append("")

        # ── 维度4: 营销分析 ──
        lines.append("  ═══ 维度四：营销分析（10分）═══")
        if score:
            d_mk = next((d for d in score.dimensions if d.name == "营销分析"), None)
            if d_mk:
                lines.append(f"  {d_mk.reason}")
        lines.append("")

        # ── 维度5: 可行性分析 ──
        lines.append("  ═══ 维度五：可行性分析（10分）═══")
        if score:
            d_fe = next((d for d in score.dimensions if d.name == "可行性分析"), None)
            if d_fe:
                lines.append(f"  {d_fe.reason}")
        lines.append("")

        # ── 综合评价 ──
        if score:
            lines.append("  ═══ 综合评价 ═══")
            lines.append("")

            # 优势
            if score.strengths:
                lines.append("  【优势】")
                for s in score.strengths:
                    lines.append(f"    + {s}")
                lines.append("")

            # 不足
            if score.weaknesses:
                lines.append("  【不足】")
                for w in score.weaknesses:
                    lines.append(f"    - {w}")
                lines.append("")

            # 改进建议
            if score.suggestions:
                lines.append("  【改进建议】")
                for i, s in enumerate(score.suggestions, 1):
                    lines.append(f"    {i}. {s}")
                lines.append("")

            # 风险提示
            lines.append("  【风险提示】")
            if score.risk_level == "高":
                lines.append("    该项目整体风险较高，建议在以下方面重点把控后再推进立项：")
                lines.append("    · 核心模块复用率不足时，需评估新建模块的供应链成本和周期")
                lines.append("    · 升级方向缺乏市场验证时，建议先做小规模用户调研")
                lines.append("    · 价格与成本不匹配时，需重新评估定价策略或寻找更优供应链")
            elif score.risk_level == "中":
                lines.append("    该项目有一定风险，建议关注以下方面：")
                lines.append("    · 确保升级方向有充分的用户需求支撑")
                lines.append("    · 关注核心模块的供应链稳定性")
                lines.append("    · 提前准备营销话术，将升级点转化为用户可感知的价值")
            else:
                lines.append("    该项目整体风险较低，建议按计划推进，关注以下细节：")
                lines.append("    · 保持原有卖点的同时做好升级模块的品质把控")
                lines.append("    · 提前锁定核心模块供应商，确保量产稳定性")

        return "\n".join(lines)
