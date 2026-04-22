# -*- coding: utf-8 -*-
"""产品立项审核分析器模块"""

from product_review_agent.analyzers.base import BaseAnalyzer, AnalyzerScore, DimensionScore
from product_review_agent.analyzers.hot_upgrade_analyzer import HotUpgradeAnalyzer
from product_review_agent.analyzers.competitor_upgrade_analyzer import CompetitorUpgradeAnalyzer
from product_review_agent.analyzers.low_sale_iterate_analyzer import LowSaleIterateAnalyzer
from product_review_agent.analyzers.category_gap_analyzer import CategoryGapAnalyzer

ANALYZER_MAP = {
    "hot_upgrade": HotUpgradeAnalyzer,
    "competitor_upgrade": CompetitorUpgradeAnalyzer,
    "low_sale_iterate": LowSaleIterateAnalyzer,
    "category_gap": CategoryGapAnalyzer,
}

__all__ = [
    "BaseAnalyzer", "AnalyzerScore", "DimensionScore",
    "HotUpgradeAnalyzer", "CompetitorUpgradeAnalyzer",
    "LowSaleIterateAnalyzer", "CategoryGapAnalyzer",
    "ANALYZER_MAP",
]
