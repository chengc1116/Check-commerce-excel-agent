# -*- coding: utf-8 -*-
"""
分析器基类 — 统一接口 + 量化打分

所有专项分析器继承 BaseAnalyzer，必须实现：
  - analyze(): 异步分析主逻辑
  - score(): 基于分析结果量化打分
  - format_report(): 格式化为可读报告

量化打分体系：
  每种分析器有4个维度，每维度25分，总分100分。
  打分基于分析结果中的 module_comparison / summary / upgrade_roadmap 量化计算。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 打分结果数据类
# ============================================================

@dataclass
class DimensionScore:
    """单个维度打分"""
    name: str           # 维度名称
    score: int          # 分数 0-25
    max_score: int = 25
    reason: str = ""    # 扣分/得分原因

    @property
    def ratio(self) -> float:
        return self.score / self.max_score if self.max_score > 0 else 0.0


@dataclass
class AnalyzerScore:
    """分析器量化打分结果"""
    analysis_type: str                          # 分析器类型
    dimensions: list[DimensionScore] = field(default_factory=list)
    total_score: int = 0                        # 总分 0-100
    max_score: int = 100
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def risk_level(self) -> str:
        """根据总分判断风险等级"""
        if self.total_score >= 75:
            return "低"
        elif self.total_score >= 50:
            return "中"
        else:
            return "高"

    @property
    def star_rating(self) -> int:
        """星级评定 1-5"""
        if self.total_score >= 90:
            return 5
        elif self.total_score >= 75:
            return 4
        elif self.total_score >= 60:
            return 3
        elif self.total_score >= 40:
            return 2
        else:
            return 1

    def to_dict(self) -> dict:
        return {
            "analysis_type": self.analysis_type,
            "total_score": self.total_score,
            "max_score": self.max_score,
            "risk_level": self.risk_level,
            "star_rating": self.star_rating,
            "dimensions": [
                {
                    "name": d.name,
                    "score": d.score,
                    "max_score": d.max_score,
                    "reason": d.reason,
                }
                for d in self.dimensions
            ],
            "strengths": self.strengths,
            "weaknesses": self.weaknesses,
            "suggestions": self.suggestions,
        }


# ============================================================
# 分析器基类
# ============================================================

class BaseAnalyzer(ABC):
    """
    专项分析器基类。

    子类必须实现：
      - analyze(): 异步执行分析
      - score(): 基于分析结果量化打分
      - format_report(): 格式化报告文本

    打分设计原则：
      - 打分基于分析结果中的客观指标（模块差距、复用率等）
      - 不再依赖LLM做二次打分（避免LLM对LLM的幻觉叠加）
      - 评分逻辑透明可解释
    """

    # 子类覆盖
    analysis_type: str = "base"
    display_name: str = "基础分析器"
    emoji: str = "📋"

    # 打分维度定义（子类覆盖）
    # 格式: [(维度名, 最大分值, 权重描述), ...]
    SCORING_DIMENSIONS: list[tuple[str, int, str]] = []

    def __init__(self):
        self._analysis_result: Optional[dict] = None

    @abstractmethod
    async def analyze(self, project_data: dict, images: list = None) -> dict:
        """
        执行专项分析。

        Args:
            project_data: Excel解析后的项目JSON数据
            images: 竞品/参考图片数据列表

        Returns:
            分析结果dict（各分析器自定义结构）
        """
        ...

    @abstractmethod
    def score(self, analysis_result: dict) -> AnalyzerScore:
        """
        基于分析结果量化打分。

        打分逻辑应基于分析结果中的客观指标：
          - module_comparison 中的 gap_level 分布
          - upgrade_roadmap 中的优先级分布
          - summary 中的复用/新建比例
          - 等等

        Args:
            analysis_result: analyze() 的返回值

        Returns:
            AnalyzerScore 量化打分结果
        """
        ...

    @abstractmethod
    def format_report(self, analysis_result: dict, score: AnalyzerScore = None) -> str:
        """
        将分析结果+打分格式化为可读报告。

        Args:
            analysis_result: analyze() 的返回值
            score: score() 的返回值（可选）

        Returns:
            Markdown格式报告文本
        """
        ...

    async def run(self, project_data: dict, images: list = None) -> tuple[dict, AnalyzerScore]:
        """
        完整执行流程: 分析 → 打分

        Returns:
            (analysis_result, score)
        """
        self._analysis_result = await self.analyze(project_data, images)
        score_result = self.score(self._analysis_result)
        return self._analysis_result, score_result


# ============================================================
# 通用打分辅助函数
# ============================================================

def _gap_level_to_score(gap_level: str) -> float:
    """差距等级 → 扣分系数 (0=不扣, 1=全扣)"""
    mapping = {
        "高": 1.0,
        "中": 0.6,
        "低": 0.3,
        "无": 0.0,
    }
    return mapping.get(gap_level, 0.5)


def _priority_to_score(priority: str) -> float:
    """优先级 → 紧迫度系数 (0=不紧迫, 1=最紧迫)"""
    mapping = {
        "P0": 1.0,
        "P1": 0.7,
        "P2": 0.4,
        "P3": 0.2,
    }
    return mapping.get(priority, 0.5)


def calc_gap_score(module_comparison: list[dict], max_score: int = 25) -> tuple[int, str]:
    """
    基于模块差距矩阵计算得分。

    逻辑:
      - 所有模块都没有差距 → 满分
      - 差距越大、差距模块越多 → 扣分越多
      - 用户感知度高的模块差距权重更大

    Returns:
        (score, reason)
    """
    if not module_comparison:
        return 10, "无模块对比数据，默认给中等偏下分数"

    total_penalty = 0.0
    max_penalty = 0.0
    gap_details = {"高": 0, "中": 0, "低": 0, "无": 0}

    for mc in module_comparison:
        gap = mc.get("gap_level", "中")
        perception = mc.get("user_perception", "中")

        # 用户感知权重
        perception_weight = {"高": 1.5, "中": 1.0, "低": 0.5}.get(perception, 1.0)

        penalty = _gap_level_to_score(gap) * perception_weight
        total_penalty += penalty
        max_penalty += 1.5  # 最大感知权重 * 最大差距
        gap_details[gap] = gap_details.get(gap, 0) + 1

    # 归一化扣分比例
    if max_penalty > 0:
        penalty_ratio = total_penalty / max_penalty
    else:
        penalty_ratio = 0.0

    score = max(0, int(max_score * (1 - penalty_ratio)))

    # 生成原因
    high_gaps = gap_details.get("高", 0)
    mid_gaps = gap_details.get("中", 0)
    low_gaps = gap_details.get("低", 0)
    no_gaps = gap_details.get("无", 0)

    reason_parts = []
    if high_gaps:
        reason_parts.append(f"{high_gaps}个高差距")
    if mid_gaps:
        reason_parts.append(f"{mid_gaps}个中差距")
    if low_gaps:
        reason_parts.append(f"{low_gaps}个低差距")
    if no_gaps:
        reason_parts.append(f"{no_gaps}个无差距")

    reason = "，".join(reason_parts) if reason_parts else "差距数据不完整"
    return score, reason


def calc_reuse_score(summary: dict, max_score: int = 25) -> tuple[int, str]:
    """
    基于复用率计算得分。

    逻辑:
      - 可复用模块越多 → 得分越高（说明基础好，风险低）
      - 需新建模块越多 → 得分越低（说明开发量大，风险高）

    Returns:
        (score, reason)
    """
    reuse_count = len(summary.get("reuse_modules", []))
    new_count = len(summary.get("new_modules_needed", []))
    total = reuse_count + new_count

    if total == 0:
        return 12, "无复用/新建模块数据"

    reuse_ratio = reuse_count / total
    score = max(0, int(max_score * reuse_ratio))

    reason = f"可复用{reuse_count}个，需新建{new_count}个，复用率{reuse_ratio:.0%}"
    return score, reason


def calc_roadmap_score(upgrade_roadmap: list[dict], max_score: int = 25) -> tuple[int, str]:
    """
    基于升级路线图评估可行性得分。

    逻辑:
      - P0级模块少 → 可行性高（聚焦核心升级）
      - P0+P1覆盖合理 → 可行性中等
      - P0过多 → 可行性低（资源分散）

    Returns:
        (score, reason)
    """
    if not upgrade_roadmap:
        return 10, "无升级路线图数据"

    p0_count = sum(1 for r in upgrade_roadmap if r.get("priority") == "P0")
    p1_count = sum(1 for r in upgrade_roadmap if r.get("priority") == "P1")
    p2_count = sum(1 for r in upgrade_roadmap if r.get("priority") == "P2")
    total = len(upgrade_roadmap)

    # P0占比评估
    p0_ratio = p0_count / total if total > 0 else 0

    if p0_ratio <= 0.2:
        # P0少，聚焦性好
        base_score = max_score * 0.9
    elif p0_ratio <= 0.4:
        # P0适中
        base_score = max_score * 0.7
    elif p0_ratio <= 0.6:
        # P0偏多
        base_score = max_score * 0.5
    else:
        # P0太多，资源分散
        base_score = max_score * 0.3

    score = max(0, int(base_score))
    reason = f"P0={p0_count}, P1={p1_count}, P2={p2_count}, 共{total}项升级"
    return score, reason


def calc_summary_quality_score(summary: dict, max_score: int = 25) -> tuple[int, str]:
    """
    基于分析摘要的完整度评分。

    逻辑:
      - 有优势 + 有不足 + 有整体评价 → 完整度好
      - 缺少某些板块 → 扣分

    Returns:
        (score, reason)
    """
    if not summary:
        return 5, "无分析摘要"

    parts = []
    if summary.get("our_strengths"):
        parts.append("优势")
    if summary.get("our_weaknesses"):
        parts.append("不足")
    if summary.get("overall_assessment"):
        parts.append("评价")
    if summary.get("reuse_modules") or summary.get("new_modules_needed"):
        parts.append("模块")

    completeness = len(parts) / 4.0  # 4个期望板块
    score = max(0, int(max_score * completeness))

    reason = f"包含: {'+'.join(parts)}" if parts else "摘要内容不完整"
    return score, reason


# ============================================================
# 通用产品信息格式化（四个分析器共用）
# ============================================================

def format_product_detail(p: dict, indent: str = "    ") -> list[str]:
    """
    格式化单个产品的详细信息：二级品类、三级品类、货号、销量、模块。

    Args:
        p: 产品数据dict，需含 sku/brand/category_l2/category_l3/sales_data/modules
        indent: 缩进前缀

    Returns:
        格式化后的行列表
    """
    lines = []
    sku = p.get("sku", p.get("product_code", "?"))
    brand = p.get("brand", "")
    cat2 = p.get("category_l2", "")
    cat3 = p.get("category_l3", "")

    # 基本信息
    parts = [f"货号: {sku}"]
    if brand:
        parts.append(f"品牌: {brand}")
    if cat2:
        parts.append(f"二级品类: {cat2}")
    if cat3:
        parts.append(f"三级品类: {cat3}")
    lines.append(f"{indent}- {' | '.join(parts)}")

    # 销量数据
    sales_data = p.get("sales_data", [])
    if sales_data:
        sales_str = " | ".join(f"{s['month']}: {s['sales_volume']}" for s in sales_data)
        lines.append(f"{indent}  销量: {sales_str}")
    else:
        lines.append(f"{indent}  销量: 暂无数据")

    # 模块信息
    modules = p.get("modules", [])
    if modules:
        lines.append(f"{indent}  模块 ({len(modules)}个):")
        for m in modules:
            cbb_code = m.get("cbb_code", "")
            category = m.get("category", m.get("sub_type", ""))
            lines.append(f"{indent}    • {cbb_code} [{category}]")
    else:
        lines.append(f"{indent}  模块: 暂无模块数据")

    return lines
