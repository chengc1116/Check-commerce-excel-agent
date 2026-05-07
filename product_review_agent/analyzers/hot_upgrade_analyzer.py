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
from product_review_agent.vl_module_splitter import ModuleSplitter, find_product_images
from product_review_agent.analyzers.base import (
    BaseAnalyzer,
    AnalyzerScore,
    DimensionScore,
    format_product_detail,
)

logger = logging.getLogger(__name__)


class HotUpgradeAnalyzer(BaseAnalyzer):
    """🔥 爆品升级分析器（V2 — 基于VL对比拆解）"""

    analysis_type = "hot_upgrade"
    display_name = "爆品升级"
    emoji = "🔥"

    SCORING_DIMENSIONS = [
        ("升级方向合理性", 30, "升级方向是否命中关键差距"),
        ("模块复用度", 30, "相同模块占比越高越好"),
        ("升级增量价值", 20, "新增模块价值+性价比"),
        ("执行可行性", 20, "打样周期+工艺难度+供应链"),
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

        return {
            "analysis_type": "hot_upgrade",
            "product_code": product_code,
            "brand": brand,
            "category_info": category_info,
            "project_data": {
                "upgrade_direction": upgrade_direction,
                "upgrade_modules": upgrade_modules,
                "upgrade_function": upgrade_function,
                "design_purpose": design_purpose,
                "price_margin": price_margin,
                "erp_cost": erp_cost,
                "target_audience": project_data.get("target_audience", ""),
                "target_scenario": project_data.get("target_scenario", ""),
                "is_new_category": project_data.get("is_new_category", "否"),
            },
            "vl_report": vl_report,
            "market_overview": market_overview,
        }

    def score(self, analysis_result: dict) -> AnalyzerScore:
        """爆品升级量化打分（4维度）"""
        vl_report = analysis_result.get("vl_report", {})
        project_data = analysis_result.get("project_data", {})

        # 从VL报告中提取关键信息
        comparison = vl_report.get("section3_module_comparison", {})
        same_modules = comparison.get("same_modules", [])
        competitor_only = comparison.get("competitor_only", [])
        own_only = comparison.get("own_only", [])
        reuse_rate = comparison.get("overall_reuse_rate", 0)

        # V2评分板块
        direction_score_data = vl_report.get("section4_upgrade_direction_score", {})
        reuse_score_data = vl_report.get("section5_module_reuse", {})
        value_score_data = vl_report.get("section6_upgrade_value", {})
        feasibility_score_data = vl_report.get("section7_execution_feasibility", {})

        # ========== 维度1: 升级方向合理性 (30分) ==========
        direction_score = 15  # 基础分
        direction_reason = ""

        if direction_score_data:
            hit = direction_score_data.get("direction_hits_gap", False)
            hit_modules = direction_score_data.get("direction_hit_modules", [])
            miss_modules = direction_score_data.get("direction_miss_modules", [])
            quality = direction_score_data.get("direction_quality", "")
            reason = direction_score_data.get("reason", "")

            if quality == "精准":
                direction_score = 26
                direction_reason = f"升级方向精准命中差距模块: {', '.join(hit_modules[:3])}"
            elif quality == "部分命中":
                direction_score = 20
                direction_reason = f"升级方向部分命中: {', '.join(hit_modules[:3])}，未命中: {', '.join(miss_modules[:2])}"
            elif quality == "偏离":
                direction_score = 8
                direction_reason = f"升级方向偏离关键差距，未命中: {', '.join(miss_modules[:3])}"
            else:
                direction_score = 15
                direction_reason = reason[:80] if reason else "升级方向评估信息不足"
        else:
            # 旧逻辑兜底：基于competitor_only和upgrade_direction手动匹配
            upgrade_dir = project_data.get("upgrade_direction", "")
            if upgrade_dir and competitor_only:
                hit_count = 0
                for comp_mod in competitor_only:
                    mod_name = comp_mod.get("module_name", comp_mod.get("name", ""))
                    for keyword in upgrade_dir.split():
                        if keyword in mod_name:
                            hit_count += 1
                            break
                if hit_count > 0:
                    direction_score = 20 + min(hit_count * 3, 6)
                    direction_reason = f"升级方向命中{hit_count}个竞品独有模块"
                else:
                    direction_score = 10
                    direction_reason = "升级方向未明显命中竞品独有模块"
            else:
                direction_reason = "无对比数据，升级方向合理性待评估"

        # ========== 维度2: 模块复用度 (30分) ==========
        reuse_score = 15  # 基础分
        reuse_reason = ""

        if reuse_score_data:
            overall_rate = reuse_score_data.get("overall_reuse_rate", 0)
            core_rate = reuse_score_data.get("core_module_reuse_rate", 0)
            new_modules = reuse_score_data.get("new_modules_needed", [])

            # 基于复用率打分
            if isinstance(overall_rate, (int, float)):
                if overall_rate >= 80:
                    reuse_score = 26
                elif overall_rate >= 60:
                    reuse_score = 21
                elif overall_rate >= 40:
                    reuse_score = 16
                else:
                    reuse_score = 10
            else:
                # 尝试从comparison中取
                try:
                    overall_rate = int(overall_rate)
                    if overall_rate >= 80:
                        reuse_score = 26
                    elif overall_rate >= 60:
                        reuse_score = 21
                    elif overall_rate >= 40:
                        reuse_score = 16
                    else:
                        reuse_score = 10
                except (ValueError, TypeError):
                    pass

            n_new = len(new_modules) if isinstance(new_modules, list) else 0
            reuse_reason = f"整体复用率{overall_rate}%，需新建{n_new}个模块"
            if isinstance(core_rate, (int, float)) and core_rate > 0:
                reuse_reason += f"，核心模块复用率{core_rate}%"

        elif isinstance(reuse_rate, (int, float)):
            if reuse_rate >= 80:
                reuse_score = 26
            elif reuse_rate >= 60:
                reuse_score = 21
            elif reuse_rate >= 40:
                reuse_score = 16
            else:
                reuse_score = 10
            n_same = len(same_modules)
            n_comp = len(competitor_only)
            reuse_reason = f"相同模块{n_same}个，竞品独有{n_comp}个，复用率{reuse_rate}%"
        else:
            reuse_reason = "复用度数据不足"

        # ========== 维度3: 升级增量价值 (20分) ==========
        value_score = 10  # 基础分
        value_reason = ""

        if value_score_data:
            perception = value_score_data.get("user_perception", "中")
            price_comp = value_score_data.get("price_competitiveness", "")
            inc_modules = value_score_data.get("incremental_modules", [])
            value_summary = value_score_data.get("value_summary", "")

            if perception == "高":
                value_score = 16
            elif perception == "中":
                value_score = 12
            elif perception == "低":
                value_score = 7

            n_inc = len(inc_modules) if isinstance(inc_modules, list) else 0
            value_reason = f"新增{n_inc}个模块，用户感知{perception}"
            if price_comp:
                value_reason += f"，{price_comp[:30]}"
        else:
            # 基于competitor_only粗估
            n_comp = len(competitor_only)
            if n_comp >= 3:
                value_score = 14
                value_reason = f"竞品独有{n_comp}个模块，升级空间较大"
            elif n_comp >= 1:
                value_score = 11
                value_reason = f"竞品独有{n_comp}个模块，有一定升级空间"
            else:
                value_score = 8
                value_reason = "无明显竞品独有模块，升级增量有限"

        # ========== 维度4: 执行可行性 (20分) ==========
        feasibility_score = 12  # 基础分
        feasibility_reason = ""

        if feasibility_score_data:
            proto_days = feasibility_score_data.get("prototype_days", 7)
            process_diff = feasibility_score_data.get("process_difficulty", "中")
            supply_risk = feasibility_score_data.get("supply_chain_risk", "中")
            is_new_cat = feasibility_score_data.get("is_new_category", False)
            new_processes = feasibility_score_data.get("new_process_needed", [])

            if process_diff == "低" and supply_risk == "低" and not is_new_cat:
                feasibility_score = 17
            elif process_diff == "中" or supply_risk == "中":
                feasibility_score = 13
            elif process_diff == "高" or supply_risk == "高":
                feasibility_score = 8
            else:
                feasibility_score = 12

            n_new_p = len(new_processes) if isinstance(new_processes, list) else 0
            feasibility_reason = f"打样{proto_days}天，工艺难度{process_diff}，供应链风险{supply_risk}"
            if n_new_p > 0:
                feasibility_reason += f"，需新开{n_new_p}项工艺"
            if is_new_cat:
                feasibility_score = max(feasibility_score - 3, 5)
                feasibility_reason += "，新品类供应链风险加成"
        else:
            is_new = project_data.get("is_new_category", "否")
            if is_new == "是":
                feasibility_score = 8
                feasibility_reason = "新品类，供应链风险较高"
            else:
                feasibility_reason = "可行性数据不足，按基础分评估"

        dimensions = [
            DimensionScore("升级方向合理性", direction_score, 30, direction_reason),
            DimensionScore("模块复用度", reuse_score, 30, reuse_reason),
            DimensionScore("升级增量价值", value_score, 20, value_reason),
            DimensionScore("执行可行性", feasibility_score, 20, feasibility_reason),
        ]

        total = sum(d.score for d in dimensions)

        # 自动生成建议
        suggestions = []
        if direction_score < 20:
            suggestions.append("升级方向与关键差距匹配度不足，建议重新审视升级重点")
        if reuse_score < 20:
            suggestions.append("模块复用度偏低，需评估新建模块的供应链风险和成本")
        if value_score < 12:
            suggestions.append("升级增量价值有限，建议聚焦用户强感知模块")
        if feasibility_score < 12:
            suggestions.append("执行可行性存疑，建议分批迭代降低风险")

        # 优劣势
        strengths = []
        weaknesses = []
        if direction_score >= 24:
            strengths.append(f"升级方向精准，命中关键差距模块")
        if reuse_score >= 24:
            strengths.append(f"模块复用度高({reuse_rate}%)，开发成本低")
        if value_score >= 15:
            strengths.append("升级增量价值明显，用户感知强")
        if direction_score < 15:
            weaknesses.append("升级方向偏离关键差距")
        if reuse_score < 15:
            weaknesses.append("复用基础薄弱，需大量新建模块")
        if feasibility_score < 10:
            weaknesses.append("执行风险较高")

        # 补充对比信息中的优劣势
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
        """格式化爆品升级分析结果"""
        if not analysis_result:
            return "  (爆品升级分析不可用)"

        lines = []
        lines.append("[🔥 爆品升级分析（V2 — VL对比拆解）]")

        # 评分总览
        if score:
            lines.append(f"  评分: {score.total_score}/100 {'★' * score.star_rating + '☆' * (5 - score.star_rating)} 风险: {score.risk_level}")
            for d in score.dimensions:
                lines.append(f"    {d.name}: {d.score}/{d.max_score} - {d.reason}")
            lines.append("")

        # 立项基本信息
        project_data = analysis_result.get("project_data", {})
        if project_data:
            lines.append("  ── 立项信息 ──")
            lines.append(f"  自家产品: {analysis_result.get('product_code', '?')} ({analysis_result.get('brand', '?')})")
            cat = analysis_result.get("category_info", {})
            lines.append(f"  品类: {cat.get('category1','')} > {cat.get('category2','')} > {cat.get('category3','')}")
            if project_data.get("upgrade_direction"):
                lines.append(f"  升级方向: {project_data['upgrade_direction']}")
            if project_data.get("upgrade_modules"):
                lines.append(f"  升级模块: {project_data['upgrade_modules']}")
            if project_data.get("price_margin"):
                lines.append(f"  价格/毛利: {project_data['price_margin']}")
            if project_data.get("erp_cost"):
                lines.append(f"  ERP成本: {project_data['erp_cost']}")
            lines.append("")

        # VL对比拆解结果
        vl_report = analysis_result.get("vl_report", {})
        if vl_report and "error" not in vl_report:
            # 模块对比
            comparison = vl_report.get("section3_module_comparison", {})
            if comparison:
                lines.append("  ── 模块对比分析 ──")
                same = comparison.get("same_modules", [])
                comp_only = comparison.get("competitor_only", [])
                own_only = comparison.get("own_only", [])
                structural = comparison.get("structural_differences", [])
                reuse = comparison.get("overall_reuse_rate", "?")

                lines.append(f"  整体复用率: {reuse}%")
                lines.append("")

                if same:
                    lines.append(f"  ✅ 相同模块（可复用）: {len(same)}个")
                    for m in same[:6]:
                        lines.append(f"    • {m.get('module_name','?')} | 自家: {str(m.get('own_detail',''))[:40]} | 竞品: {str(m.get('competitor_detail',''))[:40]}")
                    if len(same) > 6:
                        lines.append(f"    ... 还有{len(same)-6}个")
                    lines.append("")

                if comp_only:
                    lines.append(f"  🔴 竞品独有（需补齐）: {len(comp_only)}个")
                    for m in comp_only[:6]:
                        hit = "← 升级方向命中" if m.get("upgrade_direction_hit") else ""
                        lines.append(f"    • {m.get('module_name','?')} | {str(m.get('detail',''))[:50]} {hit}")
                    if len(comp_only) > 6:
                        lines.append(f"    ... 还有{len(comp_only)-6}个")
                    lines.append("")

                if own_only:
                    lines.append(f"  🟢 自家独有（差异化）: {len(own_only)}个")
                    for m in own_only[:4]:
                        adv = "✓ 优势" if m.get("is_advantage") else ""
                        lines.append(f"    • {m.get('module_name','?')} | {str(m.get('detail',''))[:50]} {adv}")
                    lines.append("")

                if structural:
                    lines.append(f"  ⚡ 结构差异: {len(structural)}处")
                    for s in structural[:4]:
                        lines.append(f"    • {s.get('aspect','?')} | 自家: {str(s.get('own',''))[:30]} | 竞品: {str(s.get('competitor',''))[:30]}")
                    lines.append("")

            # 升级方向评估
            dir_score = vl_report.get("section4_upgrade_direction_score", {})
            if dir_score:
                lines.append("  ── 升级方向评估 ──")
                quality = dir_score.get("direction_quality", "?")
                hit_mods = dir_score.get("direction_hit_modules", [])
                miss_mods = dir_score.get("direction_miss_modules", [])
                lines.append(f"  方向评估: {quality}")
                if hit_mods:
                    lines.append(f"  命中模块: {', '.join(hit_mods[:5])}")
                if miss_mods:
                    lines.append(f"  未命中: {', '.join(miss_mods[:5])}")
                if dir_score.get("reason"):
                    lines.append(f"  理由: {dir_score['reason']}")
                lines.append("")

            # 模块复用分析
            reuse_data = vl_report.get("section5_module_reuse", {})
            if reuse_data:
                lines.append("  ── 模块复用分析 ──")
                overall_rate = reuse_data.get("overall_reuse_rate", "?")
                core_rate = reuse_data.get("core_module_reuse_rate", "?")
                new_modules = reuse_data.get("new_modules_needed", [])
                reuse_analysis = reuse_data.get("reuse_analysis", [])
                reuse_summary = reuse_data.get("reuse_summary", "")
                lines.append(f"  整体复用率: {overall_rate}% | 核心模块复用率: {core_rate}%")
                if new_modules:
                    lines.append(f"  需新增模块: {', '.join(str(m) for m in new_modules[:5])}")
                for ra in reuse_analysis[:4]:
                    lines.append(f"    • {ra.get('module','?')} — 复用度: {ra.get('reusability','?')} | {str(ra.get('reason',''))[:60]}")
                if reuse_summary:
                    lines.append(f"  总结: {reuse_summary}")
                lines.append("")

            # 升级增量价值
            value_data = vl_report.get("section6_upgrade_value", {})
            if value_data:
                lines.append("  ── 升级增量价值 ──")
                inc_modules = value_data.get("incremental_modules", [])
                perception = value_data.get("user_perception", "?")
                price_comp = value_data.get("price_competitiveness", "")
                value_summary = value_data.get("value_summary", "")
                lines.append(f"  新增模块: {', '.join(str(m) for m in inc_modules[:5])}")
                lines.append(f"  用户感知: {perception}")
                if price_comp:
                    lines.append(f"  价格竞争力: {price_comp[:80]}")
                if value_summary:
                    lines.append(f"  总结: {value_summary}")
                lines.append("")

            # 执行可行性
            feas_data = vl_report.get("section7_execution_feasibility", {})
            if feas_data:
                lines.append("  ── 执行可行性 ──")
                proto_days = feas_data.get("prototype_days", "?")
                process_diff = feas_data.get("process_difficulty", "?")
                supply_risk = feas_data.get("supply_chain_risk", "?")
                is_new_cat = feas_data.get("is_new_category", False)
                new_processes = feas_data.get("new_process_needed", [])
                feas_summary = feas_data.get("feasibility_summary", "")
                lines.append(f"  打样周期: {proto_days}天 | 工艺难度: {process_diff} | 供应链风险: {supply_risk}")
                if is_new_cat:
                    lines.append(f"  新品类: 是（额外供应链风险）")
                if new_processes:
                    lines.append(f"  需新开工艺: {', '.join(str(p) for p in new_processes[:5])}")
                if feas_summary:
                    lines.append(f"  总结: {feas_summary}")
                lines.append("")

            # 下一步建议
            next_steps = vl_report.get("section9_next_steps", {})
            if next_steps:
                suggestions = next_steps.get("suggestions", [])
                summary = next_steps.get("summary", "")
                if suggestions:
                    lines.append("  ── 下一步建议 ──")
                    for s in suggestions[:5]:
                        lines.append(f"    > {s}")
                if summary:
                    lines.append(f"  综合建议: {summary}")
                lines.append("")

        # 优劣势
        if score:
            if score.strengths:
                lines.append("  优势:")
                for s in score.strengths:
                    lines.append(f"    + {s}")
            if score.weaknesses:
                lines.append("  不足:")
                for w in score.weaknesses:
                    lines.append(f"    - {w}")

            if score.suggestions:
                lines.append("")
                lines.append("  建议:")
                for s in score.suggestions:
                    lines.append(f"    > {s}")

        return "\n".join(lines)
