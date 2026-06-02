# -*- coding: utf-8 -*-
"""
CBB模块匹配公共组件（V2 — FAISS语义检索替代LLM映射）

使用FAISS向量检索将VL拆解的模块匹配到CBB库中的具体模块，
供爆品升级、品类缺失等分析器共用。

使用方式:
    matcher = CBBMatcher()
    result = matcher.match_modules(vl_module_names)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# FAISS相似度阈值
THRESHOLD_HIGH = 0.70    # >= 此值判定为"高度相似"
THRESHOLD_EXACT = 0.85   # >= 此值判定为"完全匹配"


@dataclass
class ModuleMatch:
    """单个VL模块的匹配结果"""
    vl_module: str                  # VL拆解出的模块名
    cbb_category: str = ""          # 映射到的CBB大类 (FABRIC/PAD/...)
    cbb_sub_type: str = ""          # 映射到的CBB子类
    matched: bool = False           # CBB库中是否有匹配的模块
    match_level: str = "需新建"      # 完全匹配/高度相似/可改造/需新建
    cbb_modules: list = field(default_factory=list)  # 匹配到的CBB模块列表
    reason: str = ""                # 匹配/不匹配的原因
    score: float = 0.0              # FAISS最高相似度分数


@dataclass
class MatchResult:
    """整体匹配结果"""
    total: int = 0
    matched: int = 0
    match_rate: float = 0.0
    module_matches: list = field(default_factory=list)
    category_coverage: dict = field(default_factory=dict)  # {category: {matched: N, total: N}}
    _error: str = ""


class CBBMatcher:
    """CBB模块匹配器（V2 — FAISS语义检索）"""

    CATEGORIES = ["FABRIC", "APPEARANCE", "PATTERN", "PAD", "VELCRO", "WEBBING", "PARTS"]

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            project_root = Path(__file__).resolve().parent.parent.parent
            db_path = str(project_root / "data" / "project_review.db")

        if not os.path.exists(db_path):
            raise FileNotFoundError(f"数据库文件不存在: {db_path}")

        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

        # 初始化FAISS检索器
        self._retriever = None
        try:
            from embedding.retriever import ModuleRetriever
            self._retriever = ModuleRetriever()
            logger.info(f"[CBBMatcher] FAISS检索器加载成功，模块数: {self._retriever.module_count}")
        except Exception as e:
            logger.warning(f"[CBBMatcher] FAISS检索器加载失败，将回退到DB查询: {e}")

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ============================================================
    # CBB数据查询（保留，其他组件可能用到）
    # ============================================================

    def get_cbb_by_category(self) -> dict[str, dict[str, list[dict]]]:
        """获取CBB模块按 category → sub_type 分组的数据。"""
        rows = self.conn.execute(
            "SELECT cbb_code, cbb_name, category, sub_type, supplier, price, specification "
            "FROM cbb_modules WHERE status = 'ACTIVE' ORDER BY category, sub_type, cbb_name"
        ).fetchall()

        result: dict[str, dict[str, list[dict]]] = {}
        for cat in self.CATEGORIES:
            result[cat] = {}

        for r in rows:
            cat = r["category"] or "UNKNOWN"
            sub = r["sub_type"] or "未分类"
            if cat not in result:
                result[cat] = {}
            if sub not in result[cat]:
                result[cat][sub] = []
            result[cat][sub].append({
                "cbb_code": r["cbb_code"],
                "cbb_name": r["cbb_name"] or "",
                "category": cat,
                "sub_type": sub,
                "supplier": r["supplier"] or "",
                "price": r["price"],
                "specification": r["specification"] or "",
            })

        return result

    def get_sub_types(self, category: str) -> list[dict]:
        """获取某个category下的所有sub_type及模块数量。"""
        rows = self.conn.execute(
            "SELECT cbb_code, cbb_name, sub_type, supplier, price "
            "FROM cbb_modules WHERE category = ? AND status = 'ACTIVE' "
            "ORDER BY sub_type, cbb_name",
            (category,),
        ).fetchall()

        grouped: dict[str, list[dict]] = {}
        for r in rows:
            sub = r["sub_type"] or "未分类"
            if sub not in grouped:
                grouped[sub] = []
            grouped[sub].append({
                "cbb_code": r["cbb_code"],
                "cbb_name": r["cbb_name"] or "",
                "supplier": r["supplier"] or "",
                "price": r["price"],
            })

        return [
            {"sub_type": sub, "count": len(mods), "modules": mods}
            for sub, mods in grouped.items()
        ]

    def get_modules_by_sub_type(self, category: str, sub_type: str) -> list[dict]:
        """获取指定 category + sub_type 下的所有CBB模块"""
        rows = self.conn.execute(
            "SELECT cbb_code, cbb_name, supplier, price, specification "
            "FROM cbb_modules WHERE category = ? AND sub_type = ? AND status = 'ACTIVE'",
            (category, sub_type),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_cbb_summary(self) -> str:
        """生成CBB模块库的文本摘要（兼容旧接口）。"""
        by_cat = self.get_cbb_by_category()
        lines = []
        category_labels = {
            "FABRIC": "面料/材质", "APPEARANCE": "外观/标识", "PATTERN": "版型/结构",
            "PAD": "支撑件/填充", "VELCRO": "魔术贴/粘扣", "WEBBING": "织带/松紧", "PARTS": "配件/扣具",
        }
        for cat in self.CATEGORIES:
            sub_types = by_cat.get(cat, {})
            if not sub_types:
                continue
            total = sum(len(mods) for mods in sub_types.values())
            label = category_labels.get(cat, cat)
            lines.append(f"{cat} ({label}, 共{total}个):")
            if cat == "PATTERN" and "未分类" in sub_types:
                mods = sub_types["未分类"]
                unique_names = list(dict.fromkeys(m["cbb_name"] for m in mods))
                for name in unique_names[:10]:
                    lines.append(f"  - {name}")
                if len(unique_names) > 10:
                    lines.append(f"  - ... 等{len(unique_names)}个")
                continue
            for sub_name, mods in sub_types.items():
                examples = ", ".join(m["cbb_name"] for m in mods[:3])
                if len(mods) > 3:
                    examples += f" 等{len(mods)}个"
                lines.append(f"  - {sub_name} ({len(mods)}个): {examples}")
        return "\n".join(lines)

    # ============================================================
    # FAISS模块匹配
    # ============================================================

    def match_modules(
        self,
        vl_modules: list[str],
        top_k: int = 3,
    ) -> MatchResult:
        """
        使用FAISS语义检索将VL模块匹配到CBB库。

        Args:
            vl_modules: VL拆解出的模块名列表
            top_k: 每个模块检索的候选数量

        Returns:
            MatchResult: 匹配结果
        """
        if not vl_modules:
            return MatchResult(total=0, matched=0, match_rate=0.0)

        if not self._retriever:
            return MatchResult(
                total=len(vl_modules),
                _error="FAISS检索器不可用",
            )

        module_matches = []
        category_coverage: dict[str, dict] = {}

        for vl_name in vl_modules:
            mm = self._match_single(vl_name, top_k)
            module_matches.append(mm)

            # 统计分类覆盖
            if mm.cbb_category:
                if mm.cbb_category not in category_coverage:
                    category_coverage[mm.cbb_category] = {"matched": 0, "total": 0}
                category_coverage[mm.cbb_category]["total"] += 1
                if mm.matched:
                    category_coverage[mm.cbb_category]["matched"] += 1

        total = len(module_matches)
        matched = sum(1 for m in module_matches if m.matched)

        return MatchResult(
            total=total,
            matched=matched,
            match_rate=round(matched / total * 100, 1) if total > 0 else 0,
            module_matches=module_matches,
            category_coverage=category_coverage,
        )

    def _match_single(self, vl_module: str, top_k: int = 3) -> ModuleMatch:
        """对单个VL模块执行FAISS检索，返回匹配结果"""
        try:
            results = self._retriever.search(vl_module, top_k=top_k)
        except Exception as e:
            logger.error(f"[CBBMatcher] FAISS检索异常 '{vl_module}': {e}")
            return ModuleMatch(vl_module=vl_module, reason=f"检索异常: {e}")

        if not results:
            return ModuleMatch(vl_module=vl_module, reason="FAISS未返回结果")

        # 取最高分的结果
        best = results[0]
        best_score = best.get("_score", 0.0)
        best_category = best.get("category", "")
        best_sub_type = best.get("sub_type", "") or ""

        # 阈值判定
        if best_score >= THRESHOLD_EXACT:
            match_level = "完全匹配"
            matched = True
        elif best_score >= THRESHOLD_HIGH:
            match_level = "高度相似"
            matched = True
        else:
            match_level = "需新建"
            matched = False

        # 组装匹配到的CBB模块列表
        cbb_modules = []
        for r in results:
            if r.get("_score", 0) >= THRESHOLD_HIGH:
                cbb_modules.append({
                    "cbb_code": r.get("cbb_code", ""),
                    "cbb_name": r.get("cbb_name", ""),
                    "category": r.get("category", ""),
                    "sub_type": r.get("sub_type", "") or "",
                    "supplier": r.get("supplier", ""),
                    "price": r.get("price"),
                    "score": r.get("_score", 0),
                })

        reason = f"FAISS最高分{best_score:.2f}→{best.get('cbb_name', '')}({best_category}/{best_sub_type})"

        return ModuleMatch(
            vl_module=vl_module,
            cbb_category=best_category,
            cbb_sub_type=best_sub_type,
            matched=matched,
            match_level=match_level,
            cbb_modules=cbb_modules,
            reason=reason,
            score=best_score,
        )


async def extract_target_modules(
    vl_modules: list[str],
    design_content: str = "",
    feasibility_analysis: str = "",
    upgrade_modules: str = "",
) -> list[str]:
    """
    整合VL拆解模块与设计要求，输出用于CBB检索的目标模块列表。

    逻辑：设计要求中明确提到的模块优先，未提到的保留VL原始模块。
    使用fast_model，prompt简短，预计2-5秒。

    Args:
        vl_modules: VL拆解出的模块名列表
        design_content: 设计内容/设计目的
        feasibility_analysis: 可行性分析
        upgrade_modules: 具体升级/迭代模块

    Returns:
        整合后的目标模块名列表，用于CBB FAISS检索
    """
    # 设计文本为空时直接返回VL模块
    design_text = "\n".join(filter(None, [design_content, upgrade_modules, feasibility_analysis]))
    logger.info(f"[CBBMatcher] extract_target_modules 输入: vl_modules={vl_modules}")
    logger.info(f"[CBBMatcher] design_content='{design_content[:100] if design_content else ''}'")
    logger.info(f"[CBBMatcher] upgrade_modules='{upgrade_modules[:100] if upgrade_modules else ''}'")
    logger.info(f"[CBBMatcher] feasibility_analysis='{feasibility_analysis[:100] if feasibility_analysis else ''}'")
    if not design_text.strip():
        logger.info(f"[CBBMatcher] 设计文本为空，直接返回VL模块")
        return vl_modules

    # VL模块为空时直接返回空
    if not vl_modules:
        logger.info(f"[CBBMatcher] VL模块为空，返回空")
        return []

    try:
        from product_review_agent.agents.llm_client import get_llm_client
        llm = get_llm_client()
        if not llm.is_available:
            return vl_modules

        vl_str = ", ".join(vl_modules)
        prompt = f"""请根据设计要求，替换竞品模块列表中对应的模块，输出最终用于检索的模块名称列表。

【竞品模块列表】: {vl_str}

【设计要求】:
{design_text[:500]}

规则：
1. 如果设计要求中明确提到了某个模块的替代方案（如"冰丝面料"替代"短毛绒"），则用新模块替换旧模块，旧模块不再出现
2. 设计要求中未涉及的模块，保留原样
3. 每个模块名称简短（2-6个字），适合语义检索
4. 输出数量应与输入数量一致（替换而非追加）

示例：
竞品: ["短毛绒面料", "U型版型", "魔术贴"]
设计要求: 面料改为冰丝面料
输出: ["冰丝面料", "U型版型", "魔术贴"]

只返回JSON数组，不要加```json```包裹："""

        result = await llm.acall_text(
            messages=[
                {"role": "system", "content": "你是模块分析助手。只返回JSON数组。"},
                {"role": "user", "content": prompt},
            ],
            use_fast_model=True,
        )

        logger.info(f"[CBBMatcher] LLM返回类型: {type(result)}, 内容: {str(result)[:300]}")

        if isinstance(result, list):
            integrated = [str(m).strip() for m in result if str(m).strip()]
            logger.info(f"[CBBMatcher] LLM整合结果(list): {integrated}")
            return integrated

        if isinstance(result, dict) and not result.get("_parse_error"):
            # 某些LLM客户端会自动解析JSON数组为list
            logger.info(f"[CBBMatcher] LLM返回dict，keys={list(result.keys())}")

        if isinstance(result, str):
            text = result.strip()
            # 找JSON数组
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end != -1 and end > start:
                import json
                parsed = json.loads(text[start:end + 1])
                if isinstance(parsed, list):
                    integrated = [str(m).strip() for m in parsed if str(m).strip()]
                    logger.info(f"[CBBMatcher] LLM整合结果(str→json): {integrated}")
                    return integrated

        logger.warning(f"[CBBMatcher] 模块整合LLM返回异常，回退到VL模块列表")
        return vl_modules

    except Exception as e:
        logger.error(f"[CBBMatcher] 模块整合异常: {e}")
        return vl_modules
