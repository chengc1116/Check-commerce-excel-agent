# -*- coding: utf-8 -*-
"""
🔥 爆品升级分析器（V2 — 基于VL对比拆解）

流程：
  1. 解析立项表Excel → 获取自家货号、升级方向、竞品图片等
  2. 自家货号 → 检索商品图片
  3. 调用ModuleSplitter对比拆解（自家+竞品分别VL拆解，异步并行）
  4. 结合表格信息 → 4维度量化评分

量化打分（100分制）：
  - 升级方向合理性 (30分): 升级方向是否命中VL对比发现的关键差距
  - 模块复用度 (30分): VL对比中相同模块占比
  - 升级增量价值 (20分): 新增模块价值 + 价格竞争力
  - 执行可行性 (20分): 打样周期 + 工艺难度 + 供应链风险
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from product_review_agent.agents.llm_client import LLMClient, get_llm_client
from product_review_agent.product_db.product_query import ProductQuery
from product_review_agent.product_db.module_sales_query import ModuleSalesQuery
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

        # Step 5: 模块销量验证（新增）
        module_sales_verification = self._verify_module_sales(vl_report, project_data, category2)

        # Step 6: LLM评分预计算（升级合理性 + 营销分析 + 可行性分析）
        # 构建完整的project_data供LLM评分使用
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
        llm_scoring = await self._precompute_llm_scoring(full_project_data)

        return {
            "analysis_type": "hot_upgrade",
            "product_code": product_code,
            "brand": brand,
            "category_info": category_info,
            "project_data": full_project_data,
            "vl_report": vl_report,
            "market_overview": market_overview,
            "module_sales_verification": module_sales_verification,
            "llm_scoring": llm_scoring,
        }

    def _verify_module_sales(self, vl_report: dict, project_data: dict,
                              product_category: str = "") -> dict:
        """
        模块销量验证：检查升级方向指向的模块是否在销量排名中靠前

        Returns:
          {
            "available": True/False,
            "verifications": [
              {
                "module_name": "VL输出的模块名",
                "matched_cbb_code": "匹配到的cbb_code",
                "matched_cbb_name": "匹配到的cbb_name",
                "rank": 排名,
                "module_sales": 模块销量,
                "trend": "rising"/"stable"/"falling"/"new",
                "source_rank": 自家原模块排名,
                "source_module_sales": 自家原模块销量,
                "is_upgrade": True/False (目标排名是否更高)
              }
            ],
            "best_verification": 最佳验证结果,
            "summary": "验证总结文本"
          }
        """
        result = {
            "available": False,
            "verifications": [],
            "best_verification": None,
            "summary": "无模块销量数据",
        }

        try:
            with ModuleSalesQuery() as msq:
                if not msq.has_module_sales:
                    return result

                # 获取升级方向命中的模块
                direction_data = vl_report.get("section4_upgrade_direction_score", {})
                hit_modules = direction_data.get("direction_hit_modules", [])

                # 获取竞品独有模块
                comparison = vl_report.get("section3_module_comparison", {})
                competitor_only = comparison.get("competitor_only", [])
                same_modules = comparison.get("same_modules", [])

                # 获取升级方向文本
                upgrade_dir = project_data.get("upgrade_direction", "")

                # 确定需要验证的模块：优先用 hit_modules，否则从 competitor_only 取
                modules_to_verify = []
                if hit_modules:
                    modules_to_verify = hit_modules
                elif competitor_only:
                    modules_to_verify = [
                        m.get("module_name", "") for m in competitor_only[:3]
                    ]

                if not modules_to_verify:
                    result["summary"] = "无竞品独有模块或升级方向命中信息"
                    return result

                # 匹配自家产品原模块（从 same_modules 中取第一个作为基准）
                source_rank = None
                source_module_sales = None
                source_cbb = None
                if same_modules:
                    first_same = same_modules[0]
                    source_name = first_same.get("module_name", "")
                    source_match = msq.match_module_by_name(source_name, product_category)
                    if source_match:
                        source_rank = source_match.get("rank")
                        source_module_sales = source_match.get("module_sales")
                        source_cbb = source_match.get("cbb_code")

                # 逐个验证目标模块
                verifications = []
                for mod_name in modules_to_verify:
                    if not mod_name:
                        continue
                    match = msq.match_module_by_name(mod_name, product_category)
                    if not match:
                        verifications.append({
                            "module_name": mod_name,
                            "matched_cbb_code": None,
                            "matched_cbb_name": None,
                            "rank": None,
                            "module_sales": None,
                            "trend": "unknown",
                            "source_rank": source_rank,
                            "source_module_sales": source_module_sales,
                            "is_upgrade": None,
                        })
                        continue

                    trend_info = msq.get_module_trend(match["cbb_code"])
                    is_upgrade = None
                    if source_rank and match.get("rank"):
                        is_upgrade = match["rank"] < source_rank

                    verifications.append({
                        "module_name": mod_name,
                        "matched_cbb_code": match.get("cbb_code"),
                        "matched_cbb_name": match.get("cbb_name"),
                        "rank": match.get("rank"),
                        "module_sales": match.get("module_sales"),
                        "trend": trend_info.get("trend", "unknown"),
                        "rank_change": trend_info.get("rank_change", 0),
                        "source_rank": source_rank,
                        "source_module_sales": source_module_sales,
                        "is_upgrade": is_upgrade,
                    })

                # 选择最佳验证结果（排名最高的那个）
                best = None
                for v in verifications:
                    if v["rank"] is not None:
                        if best is None or v["rank"] < best["rank"]:
                            best = v

                # 生成总结
                if best and best["rank"]:
                    if best["is_upgrade"]:
                        summary = (f"升级目标「{best['module_name']}」匹配到"
                                   f"「{best['matched_cbb_name']}」，排名#{best['rank']}"
                                   f"(销量{best['module_sales']})，高于原模块#{source_rank}，升级方向有市场验证")
                    elif best["is_upgrade"] is False:
                        summary = (f"升级目标「{best['module_name']}」匹配到"
                                   f"「{best['matched_cbb_name']}」，排名#{best['rank']}"
                                   f"(销量{best['module_sales']})，低于原模块#{source_rank}，需谨慎评估")
                    else:
                        summary = (f"升级目标「{best['module_name']}」匹配到"
                                   f"「{best['matched_cbb_name']}」，排名#{best['rank']}"
                                   f"(销量{best['module_sales']})")
                elif verifications:
                    summary = f"共{len(verifications)}个目标模块，均未在模块销量表中找到匹配"
                else:
                    summary = "无目标模块信息"

                result = {
                    "available": True,
                    "verifications": verifications,
                    "best_verification": best,
                    "source_rank": source_rank,
                    "source_module_sales": source_module_sales,
                    "summary": summary,
                }

        except Exception as e:
            logger.error(f"[爆品升级] 模块销量验证异常: {e}")
            result["summary"] = f"验证异常: {e}"

        return result

    async def _precompute_llm_scoring(self, project_data: dict) -> dict:
        """LLM预计算：升级合理性 + 营销分析 + 可行性分析"""
        llm = get_llm_client()
        if not llm.is_available:
            return {"_error": "LLM不可用"}

        design_purpose = project_data.get("design_purpose", "")
        upgrade_modules = project_data.get("upgrade_modules", "")
        product_hotpoint = project_data.get("product_hotpoint", "")
        upgrade_valiable = project_data.get("upgrade_valiable", "")

        prompt = f"""你是一个电商产品立项审核专家。请根据以下爆品升级信息，对三个维度进行评分分析。

【原有爆品卖点】
{product_hotpoint or '(未填写)'}

【升级目的】
{design_purpose or '(未填写)'}

【升级模块】
{upgrade_modules or '(未填写)'}

【升级可行性说明】
{upgrade_valiable or '(未填写)'}

---

请严格按以下JSON格式返回，不要加```json```包裹：

{{
  "upgrade_rationality": {{
    "goal_action_score": 0-8,
    "goal_action_reason": "目标-动作一致性分析（目标是否明确、动作是否能支撑目标）",
    "hotpoint_preserve_score": 0-8,
    "hotpoint_preserve_reason": "原有卖点保留度分析（升级后是否保留原有卖点，是否有卖点冲突）",
    "necessity_score": 0-6,
    "necessity_reason": "升级必要性分析（目标→动作的逻辑链是否合理）"
  }},
  "marketing": {{
    "score": 0-10,
    "reason": "升级卖点的营销价值分析（升级点能否转化为营销话术/口碑优势，design_purpose中是否有营销价值描述）"
  }},
  "feasibility": {{
    "score": 0-10,
    "reason": "供应链/打样可行性分析（upgrade_valiable中是否提到模块可获取性、打样难度等）"
  }}
}}"""

        try:
            result = await llm.acall_text(
                messages=[
                    {"role": "system", "content": "你是电商产品立项审核专家，只返回JSON。"},
                    {"role": "user", "content": prompt},
                ],
            )
            # acall_text 不指定 response_format，返回字符串，手动解析JSON
            if isinstance(result, dict) and not result.get("_parse_error"):
                return result
            if isinstance(result, str):
                # 尝试提取JSON（可能被```json```包裹）
                text = result.strip()
                if text.startswith("```"):
                    # 去掉 ```json 和 ```
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
                # 尝试找到第一个 { 和最后一个 }
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
            logger.error(f"[爆品升级] LLM评分预计算异常: {e}")
            return {"_error": str(e)}

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """爆品升级量化打分（V3 — 5维度，总分100）"""
        vl_report = analysis_result.get("vl_report", {})
        project_data = analysis_result.get("project_data", {})
        module_sales_v = analysis_result.get("module_sales_verification", {})
        llm_scoring = analysis_result.get("llm_scoring", {})

        # 从VL报告中提取关键信息
        comparison = vl_report.get("section3_module_comparison", {})
        same_modules = comparison.get("same_modules", [])
        competitor_only = comparison.get("competitor_only", [])
        own_only = comparison.get("own_only", [])

        # ==================== 维度1: 模块复用 (48分) ====================
        # 1a: 核心模块复用 (20分) — 面料、版型
        core_reuse_score = 10  # 基础分
        core_reuse_reason = ""

        core_modules = ["面料", "版型"]
        core_hit = 0
        core_details = []
        for mod in same_modules:
            mod_name = mod.get("module_name", "")
            for core in core_modules:
                if core in mod_name:
                    core_hit += 1
                    core_details.append(mod_name)
                    break

        if core_hit >= 2:
            core_reuse_score = 20
            core_reuse_reason = f"核心模块均复用: {', '.join(core_details)}"
        elif core_hit == 1:
            core_reuse_score = 12
            core_reuse_reason = f"部分核心模块复用: {', '.join(core_details)}"
        else:
            # 检查竞品独有中是否有核心模块
            comp_core = []
            for mod in competitor_only:
                mod_name = mod.get("module_name", "")
                for core in core_modules:
                    if core in mod_name:
                        comp_core.append(mod_name)
                        break
            if comp_core:
                core_reuse_score = 5
                core_reuse_reason = f"核心模块未复用，竞品独有: {', '.join(comp_core)}"
            else:
                core_reuse_score = 10
                core_reuse_reason = "核心模块信息不足，按基础分评估"

        # 1b: 非核心模块复用 (6分)
        non_core_reuse_score = 3  # 基础分
        non_core_reuse_reason = ""

        total_modules = len(same_modules) + len(competitor_only) + len(own_only)
        if total_modules > 0:
            non_core_same = len(same_modules) - core_hit
            non_core_total = total_modules - len(core_modules)
            if non_core_total > 0:
                non_core_rate = non_core_same / non_core_total
                if non_core_rate >= 0.7:
                    non_core_reuse_score = 6
                elif non_core_rate >= 0.4:
                    non_core_reuse_score = 4
                else:
                    non_core_reuse_score = 2
                non_core_reuse_reason = f"非核心模块复用率{int(non_core_rate * 100)}%({non_core_same}/{non_core_total})"
            else:
                non_core_reuse_reason = "非核心模块信息不足"
        else:
            non_core_reuse_reason = "无模块对比数据"

        # 1c: 升级方向匹配度 (12分) — VL分析 + 数据库检索
        direction_match_score = 6  # 基础分
        direction_match_reason = ""

        upgrade_modules_text = project_data.get("upgrade_modules", "")
        if upgrade_modules_text and module_sales_v.get("available"):
            verifications = module_sales_v.get("verifications", [])
            matched = [v for v in verifications if v.get("matched_cbb_code")]
            if matched:
                best = module_sales_v.get("best_verification")
                if best and best.get("rank"):
                    rank = best["rank"]
                    if rank <= 5:
                        direction_match_score = 12
                    elif rank <= 15:
                        direction_match_score = 9
                    else:
                        direction_match_score = 6
                    direction_match_reason = f"升级目标匹配到CBB模块「{best.get('matched_cbb_name', '')}」，排名#{rank}"
                else:
                    direction_match_score = 7
                    direction_match_reason = f"升级目标匹配到{len(matched)}个CBB模块"
            else:
                direction_match_score = 3
                direction_match_reason = "升级目标未在CBB模块库中找到匹配"
        elif upgrade_modules_text:
            direction_match_reason = "无模块销量数据，无法验证升级方向匹配"
        else:
            direction_match_reason = "未填写升级模块信息"

        # 1d: 目标模块市场验证 (10分)
        market_verify_score = 5  # 基础分
        market_verify_reason = ""

        if module_sales_v.get("available") and module_sales_v.get("best_verification"):
            best = module_sales_v["best_verification"]
            rank = best.get("rank")
            if rank is not None:
                if rank <= 3:
                    market_verify_score = 10
                elif rank <= 10:
                    market_verify_score = 7
                elif rank <= 30:
                    market_verify_score = 4
                else:
                    market_verify_score = 2
                trend = best.get("trend", "unknown")
                market_verify_reason = f"目标模块销量排名#{rank}"
                if trend == "rising":
                    market_verify_score = min(market_verify_score + 1, 10)
                    market_verify_reason += "，趋势上升"
                elif trend == "falling":
                    market_verify_score = max(market_verify_score - 2, 1)
                    market_verify_reason += "，趋势下降"
            else:
                market_verify_reason = f"目标模块「{best.get('module_name', '')}」无销量排名"
        else:
            market_verify_reason = "无模块销量数据"

        reuse_total = core_reuse_score + non_core_reuse_score + direction_match_score + market_verify_score
        reuse_reason = (f"核心模块{core_reuse_score}/20 | {core_reuse_reason}\n"
                        f"    非核心模块{non_core_reuse_score}/6 | {non_core_reuse_reason}\n"
                        f"    升级方向匹配{direction_match_score}/12 | {direction_match_reason}\n"
                        f"    市场验证{market_verify_score}/10 | {market_verify_reason}")

        # ==================== 维度2: 模块升级合理性 (22分) ====================
        upgrade_rationality_score = 11
        upgrade_rationality_reason = ""

        ur = llm_scoring.get("upgrade_rationality", {})
        if ur and not ur.get("_error"):
            goal_action_score = ur.get("goal_action_score", 4)
            hotpoint_preserve_score = ur.get("hotpoint_preserve_score", 4)
            necessity_score = ur.get("necessity_score", 3)
            upgrade_rationality_score = goal_action_score + hotpoint_preserve_score + necessity_score
            ga_reason = ur.get("goal_action_reason", "")[:60]
            hp_reason = ur.get("hotpoint_preserve_reason", "")[:60]
            ne_reason = ur.get("necessity_reason", "")[:60]
            upgrade_rationality_reason = (f"目标-动作{goal_action_score}/8: {ga_reason}\n"
                                          f"    卖点保留{hotpoint_preserve_score}/8: {hp_reason}\n"
                                          f"    升级必要性{necessity_score}/6: {ne_reason}")
        else:
            upgrade_rationality_reason = "LLM评分不可用，按基础分评估"

        # ==================== 维度3: 价格分析 (10分) ====================
        price_score = 5
        price_reason = ""

        pricing = project_data.get("pricing", "")
        product_price = project_data.get("product_price", "")
        competitor_price = project_data.get("competitor_price", "")
        erp_cost = project_data.get("erp_cost", "")

        # 3a: 价格定位合理性 (5分)
        price_position_score = 3
        price_position_reason = ""

        if pricing and competitor_price:
            # 提取数字进行比较
            import re
            new_price_nums = re.findall(r"[\d.]+", str(pricing))
            comp_price_nums = re.findall(r"[\d.]+", str(competitor_price))
            if new_price_nums and comp_price_nums:
                try:
                    new_p = float(new_price_nums[0])
                    comp_p = float(comp_price_nums[0])
                    if new_p <= comp_p:
                        price_position_score = 5
                        price_position_reason = f"定价{new_p}≤竞品{comp_p}，价格有竞争力"
                    elif new_p <= comp_p * 1.1:
                        price_position_score = 4
                        price_position_reason = f"定价{new_p}略高于竞品{comp_p}，在合理范围"
                    else:
                        price_position_score = 2
                        price_position_reason = f"定价{new_p}明显高于竞品{comp_p}，需差异化支撑"
                except ValueError:
                    price_position_reason = "价格数据格式异常"
            else:
                price_position_reason = "无法提取价格数字"
        elif pricing:
            price_position_reason = f"有定价{pricing}，但无竞品价格对比"
        else:
            price_position_reason = "未填写定价信息"

        # 3b: 成本-定价匹配度 (5分)
        cost_match_score = 3
        cost_match_reason = ""

        if upgrade_modules_text and erp_cost:
            # 升级模块推断成本变化方向
            cost_keywords_up = ["记忆棉", "冰丝", "真丝", "高端", "进口", "婴儿级", "骨架", "硅胶"]
            cost_keywords_down = ["普通棉", "简化", "基础"]
            cost_up = any(kw in upgrade_modules_text for kw in cost_keywords_up)
            cost_down = any(kw in upgrade_modules_text for kw in cost_keywords_down)

            if cost_up and pricing:
                cost_match_score = 2
                cost_match_reason = "升级模块推断成本上升，需确认定价是否合理覆盖"
            elif cost_down:
                cost_match_score = 5
                cost_match_reason = "升级模块推断成本下降，利润空间增大"
            else:
                cost_match_score = 3
                cost_match_reason = "成本变化不明显"
        else:
            cost_match_reason = "升级模块或ERP成本信息不足"

        price_score = price_position_score + cost_match_score
        price_reason = (f"价格定位{price_position_score}/5: {price_position_reason}\n"
                        f"    成本匹配{cost_match_score}/5: {cost_match_reason}")

        # ==================== 维度4: 营销分析 (10分) ====================
        marketing_score = 5
        marketing_reason = ""

        mk = llm_scoring.get("marketing", {})
        if mk and not mk.get("_error"):
            marketing_score = mk.get("score", 5)
            marketing_reason = mk.get("reason", "")[:100]
        else:
            marketing_reason = "LLM评分不可用，按基础分评估"

        # ==================== 维度5: 可行性分析 (10分) ====================
        feasibility_score = 5
        feasibility_reason = ""

        fe = llm_scoring.get("feasibility", {})
        if fe and not fe.get("_error"):
            feasibility_score = fe.get("score", 5)
            feasibility_reason = fe.get("reason", "")[:100]
        else:
            feasibility_reason = "LLM评分不可用，按基础分评估"

        # ==================== 汇总 ====================
        dimensions = [
            DimensionScore("模块复用", reuse_total, 48, reuse_reason),
            DimensionScore("模块升级合理性", upgrade_rationality_score, 22, upgrade_rationality_reason),
            DimensionScore("价格分析", price_score, 10, price_reason),
            DimensionScore("营销分析", marketing_score, 10, marketing_reason),
            DimensionScore("可行性分析", feasibility_score, 10, feasibility_reason),
        ]

        total = sum(d.score for d in dimensions)

        # 自动生成建议
        suggestions = []
        if core_reuse_score < 12:
            suggestions.append("核心模块（面料/版型）复用不足，需评估新建成本")
        if direction_match_score < 6:
            suggestions.append("升级方向在CBB模块库中匹配度低，可能需要新开模块")
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
        if core_reuse_score >= 16:
            strengths.append("核心模块复用度高，开发成本低")
        if direction_match_score >= 9:
            strengths.append("升级方向精准匹配CBB模块，有市场验证")
        if market_verify_score >= 8:
            strengths.append("目标模块销量排名靠前，市场验证充分")
        if upgrade_rationality_score >= 18:
            strengths.append("升级逻辑清晰，原有卖点保留完好")
        if price_score >= 8:
            strengths.append("价格定位合理，成本控制良好")
        if marketing_score >= 8:
            strengths.append("升级卖点有明确营销价值")
        if feasibility_score >= 8:
            strengths.append("供应链可行性高")

        if core_reuse_score < 8:
            weaknesses.append("核心模块复用不足，需大量新开")
        if direction_match_score < 4:
            weaknesses.append("升级方向在模块库中无匹配，供应链风险高")
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

        # 模块销量验证
        module_sales_v = analysis_result.get("module_sales_verification", {})
        if module_sales_v.get("available"):
            lines.append("  模块销量验证:")
            lines.append(f"    {module_sales_v.get('summary', '')}")
            for v in module_sales_v.get("verifications", []):
                matched = v.get("matched_cbb_code")
                if matched:
                    rank_str = f"#{v['rank']}" if v.get("rank") else "无排名"
                    sales_str = f"销量{v['module_sales']}" if v.get("module_sales") else ""
                    trend_str = f"趋势{v['trend']}" if v.get("trend") and v["trend"] != "unknown" else ""
                    upgrade_str = "↑优于原模块" if v.get("is_upgrade") else ("↓低于原模块" if v.get("is_upgrade") is False else "")
                    lines.append(f"    {v['module_name']} → {v['matched_cbb_name']} | 排名{rank_str} {sales_str} {trend_str} {upgrade_str}")
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
