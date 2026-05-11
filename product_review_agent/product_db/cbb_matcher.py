# -*- coding: utf-8 -*-
"""
CBB模块匹配公共组件

将VL拆解的模块映射到CBB分类体系（category + sub_type），
并验证CBB库中是否存在可复用的模块。

供爆品升级、品类缺失等分析器共用。

使用方式:
    matcher = CBBMatcher()
    # 获取品类维度的CBB数据摘要（传给LLM做映射参考）
    cbb_summary = matcher.get_cbb_summary()
    # 匹配VL模块
    result = await matcher.match_modules(vl_module_names, llm)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ModuleMatch:
    """单个VL模块的匹配结果"""
    vl_module: str                  # VL拆解出的模块名
    cbb_category: str = ""          # 映射到的CBB大类 (FABRIC/PAD/...)
    cbb_sub_type: str = ""          # 映射到的CBB子类
    matched: bool = False           # CBB库中是否有该子类的模块
    match_level: str = "需新建"      # 完全匹配/高度相似/可改造/需新建
    cbb_modules: list = field(default_factory=list)  # 匹配到的CBB模块列表
    reason: str = ""                # 匹配/不匹配的原因


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
    """CBB模块匹配器"""

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

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ============================================================
    # CBB数据查询
    # ============================================================

    def get_cbb_by_category(self) -> dict[str, dict[str, list[dict]]]:
        """
        获取CBB模块按 category → sub_type 分组的数据。

        Returns:
            {
                "FABRIC": {
                    "热熔胶膜类复合布": [
                        {"cbb_code": "...", "cbb_name": "...", "supplier": "...", ...},
                        ...
                    ],
                    "四面弹类布": [...],
                },
                "PAD": { ... },
                ...
            }
        """
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

    def get_cbb_summary(self) -> str:
        """
        生成CBB模块库的文本摘要，供LLM做模块映射参考。

        输出格式:
            FABRIC (面料):
              - 热熔胶膜类复合布 (18个): 热熔胶膜类复合布A, 热熔胶膜类复合布B, ...
              - 四面弹类布 (14个): ...
            PAD (支撑件):
              - 塑料支撑类 (12个): ...
        """
        by_cat = self.get_cbb_by_category()
        lines = []

        category_labels = {
            "FABRIC": "面料/材质",
            "APPEARANCE": "外观/标识",
            "PATTERN": "版型/结构",
            "PAD": "支撑件/填充",
            "VELCRO": "魔术贴/粘扣",
            "WEBBING": "织带/松紧",
            "PARTS": "配件/扣具",
        }

        for cat in self.CATEGORIES:
            sub_types = by_cat.get(cat, {})
            if not sub_types:
                continue
            total = sum(len(mods) for mods in sub_types.values())
            label = category_labels.get(cat, cat)
            lines.append(f"{cat} ({label}, 共{total}个):")

            # PATTERN类无sub_type，直接列出模块名
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

    def get_sub_types(self, category: str) -> list[dict]:
        """
        获取某个category下的所有sub_type及模块数量。

        Returns:
            [{"sub_type": "网布", "count": 4, "modules": [...]}, ...]
        """
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

    def _fuzzy_match_pattern(self, name: str) -> list[dict]:
        """PATTERN类无sub_type，按cbb_name模糊匹配"""
        rows = self.conn.execute(
            "SELECT cbb_code, cbb_name, supplier, price, specification "
            "FROM cbb_modules WHERE category = 'PATTERN' AND status = 'ACTIVE'",
        ).fetchall()

        matches = []
        name_lower = name.lower()
        for r in rows:
            cbb_name = (r["cbb_name"] or "").lower()
            # 双向包含匹配
            if name_lower in cbb_name or cbb_name in name_lower:
                matches.append(dict(r))

        # 如果精确包含没命中，做关键词匹配
        if not matches:
            import re
            tokens = [w for w in re.split(r"[\s,，、+/]+", name_lower) if len(w) >= 2]
            for r in rows:
                cbb_name = (r["cbb_name"] or "").lower()
                score = sum(1 for t in tokens if t in cbb_name)
                if score > 0:
                    matches.append(dict(r))

        return matches

    def get_sub_type_options_for_llm(self) -> str:
        """
        生成精简的 sub_type 选项列表，供LLM映射时参考。

        输出格式:
            FABRIC: 热熔胶膜类复合布(18), 四面弹类布(14), tpu贴合类复合面料(14), ...
            PAD: 塑料支撑类(12), TPE/硅胶缓冲垫(7), 金属支撑(6), ...
            PATTERN (无sub_type，按模块名匹配):
              仿OK+TPU+锦纶莱卡, SBR复合布 透气针眼布, 打孔SBR, ...

        注: PATTERN类没有sub_type，直接列出模块名供LLM参考。
        """
        lines = []
        for cat in self.CATEGORIES:
            sub_types = self.get_sub_types(cat)

            # PATTERN类无sub_type，直接列出模块名
            if cat == "PATTERN" and sub_types and sub_types[0]["sub_type"] == "未分类":
                names = [m["cbb_name"] for m in sub_types[0]["modules"]]
                # 按名称去重
                unique_names = list(dict.fromkeys(names))
                lines.append(f"PATTERN (无sub_type，按模块名匹配): {', '.join(unique_names[:20])}")
                continue

            if not sub_types:
                continue
            items = ", ".join(f"{s['sub_type']}({s['count']})" for s in sub_types)
            lines.append(f"{cat}: {items}")
        return "\n".join(lines)

    # ============================================================
    # LLM模块匹配
    # ============================================================

    async def match_modules(
        self,
        vl_modules: list[str],
        llm,
        product_category: str = "",
    ) -> MatchResult:
        """
        将VL拆解的模块列表映射到CBB分类，并验证CBB库中是否有可复用模块。

        Args:
            vl_modules: VL拆解出的模块名列表，如 ["3D记忆棉腰靠", "透气网眼布", "硅胶防滑条"]
            llm: LLMClient实例
            product_category: 产品品类（可选，用于辅助匹配）

        Returns:
            MatchResult: 匹配结果
        """
        if not vl_modules:
            return MatchResult(total=0, matched=0, match_rate=0.0)

        if not llm or not llm.is_available:
            return MatchResult(
                total=len(vl_modules),
                _error="LLM不可用",
            )

        # 准备CBB sub_type选项（给LLM参考）
        sub_type_options = self.get_sub_type_options_for_llm()
        module_list = "\n".join(f"  {i+1}. {name}" for i, name in enumerate(vl_modules))

        prompt = f"""你是产品模块化分析专家。请将以下VL拆解的模块映射到CBB分类体系。

## VL拆解模块
{module_list}

## CBB分类体系（category: sub_type选项）
{sub_type_options}

## 映射规则
1. 每个VL模块必须映射到一个 category + sub_type
2. sub_type 必须从上面的选项中选择最匹配的
3. PATTERN类无sub_type，该类的cbb_sub_type直接填写最接近的模块名或"需新建"
4. 如果VL模块无法匹配任何现有sub_type，选择最接近的category，cbb_sub_type设为"需新建"
5. 匹配级别: 完全匹配(名称高度吻合) / 高度相似(功能和材质相同) / 可改造(有类似模块可改) / 需新建(库中完全没有)

请返回纯JSON，格式如下：
{{
  "mappings": [
    {{
      "vl_module": "VL模块名",
      "cbb_category": "FABRIC/PAD/...",
      "cbb_sub_type": "从选项中选择的sub_type，或'需新建'",
      "match_level": "完全匹配/高度相似/可改造/需新建",
      "reason": "简要说明匹配依据"
    }}
  ]
}}"""

        try:
            result = await llm.acall_text(
                messages=[
                    {"role": "system", "content": "你是产品模块化分析专家，精通CBB模块分类体系。只返回JSON。"},
                    {"role": "user", "content": prompt},
                ],
            )

            mappings = self._parse_llm_result(result, vl_modules)
            return self._build_match_result(mappings)

        except Exception as e:
            logger.error(f"[CBBMatcher] LLM匹配异常: {e}")
            return MatchResult(
                total=len(vl_modules),
                _error=str(e),
            )

    def _parse_llm_result(self, result, vl_modules: list[str]) -> list[dict]:
        """解析LLM返回的映射结果"""
        mappings = []

        if isinstance(result, dict) and "mappings" in result:
            mappings = result["mappings"]
        elif isinstance(result, str):
            text = result.strip()
            # 尝试提取JSON
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

            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    parsed = json.loads(text[start:end + 1])
                    if isinstance(parsed, dict) and "mappings" in parsed:
                        mappings = parsed["mappings"]
                except json.JSONDecodeError:
                    pass

        # 确保每个VL模块都有对应的mapping
        mapped_names = {m.get("vl_module", "") for m in mappings}
        for vl_name in vl_modules:
            if vl_name not in mapped_names:
                mappings.append({
                    "vl_module": vl_name,
                    "cbb_category": "",
                    "cbb_sub_type": "需新建",
                    "match_level": "需新建",
                    "reason": "LLM未返回该模块的映射",
                })

        return mappings

    def _build_match_result(self, mappings: list[dict]) -> MatchResult:
        """根据LLM映射结果，查询CBB库验证匹配情况"""
        module_matches = []
        category_coverage: dict[str, dict] = {}

        for m in mappings:
            vl_name = m.get("vl_module", "")
            cat = m.get("cbb_category", "")
            sub = m.get("cbb_sub_type", "")
            level = m.get("match_level", "需新建")
            reason = m.get("reason", "")

            # 查询CBB库中是否有该sub_type的模块
            cbb_modules = []
            matched = False
            if cat and sub and sub != "需新建":
                cbb_modules = self.get_modules_by_sub_type(cat, sub)
                matched = len(cbb_modules) > 0

                # PATTERN类无sub_type，按cbb_name模糊匹配
                if not matched and cat == "PATTERN":
                    cbb_modules = self._fuzzy_match_pattern(sub)
                    matched = len(cbb_modules) > 0

            if not matched:
                level = "需新建"

            mm = ModuleMatch(
                vl_module=vl_name,
                cbb_category=cat,
                cbb_sub_type=sub,
                matched=matched,
                match_level=level,
                cbb_modules=cbb_modules[:5],  # 最多返回5个
                reason=reason,
            )
            module_matches.append(mm)

            # 统计分类覆盖
            if cat:
                if cat not in category_coverage:
                    category_coverage[cat] = {"matched": 0, "total": 0}
                category_coverage[cat]["total"] += 1
                if matched:
                    category_coverage[cat]["matched"] += 1

        total = len(module_matches)
        matched = sum(1 for m in module_matches if m.matched)

        return MatchResult(
            total=total,
            matched=matched,
            match_rate=round(matched / total * 100, 1) if total > 0 else 0,
            module_matches=module_matches,
            category_coverage=category_coverage,
        )
