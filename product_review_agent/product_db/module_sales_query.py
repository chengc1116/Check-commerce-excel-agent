# -*- coding: utf-8 -*-
"""
模块销量排名查询层

为爆品升级分析器提供模块销量验证数据：
  - 模块排名查询
  - 模块名→cbb_code 模糊匹配
  - 两模块对比（目标 vs 原模块）
  - 趋势计算（近3个月排名变化）
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ModuleSalesQuery:
    """模块销量排名查询"""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            project_root = Path(__file__).resolve().parent.parent.parent
            db_path = str(project_root / "data" / "project_review.db")

        if not os.path.exists(db_path):
            raise FileNotFoundError(f"数据库文件不存在: {db_path}")

        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

        # 检查表是否存在
        tables = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        self.has_module_sales = "module_monthly_sales" in tables

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get_available_months(self) -> list[str]:
        """获取所有可用月份"""
        if not self.has_module_sales:
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT month FROM module_monthly_sales ORDER BY month DESC"
        ).fetchall()
        return [r[0] for r in rows]

    def get_latest_month(self) -> Optional[str]:
        """获取最新月份"""
        months = self.get_available_months()
        return months[0] if months else None

    def get_module_by_code(self, cbb_code: str, month: str = None) -> Optional[dict]:
        """按 cbb_code 查询模块排名信息"""
        if not self.has_module_sales:
            return None
        if month is None:
            month = self.get_latest_month()
        if not month:
            return None

        row = self.conn.execute(
            "SELECT * FROM module_monthly_sales WHERE cbb_code = ? AND month = ?",
            (cbb_code, month),
        ).fetchone()
        return dict(row) if row else None

    def get_module_rank(self, cbb_code: str, month: str = None) -> tuple[Optional[int], Optional[int]]:
        """获取模块排名和销量，返回 (rank, module_sales)"""
        info = self.get_module_by_code(cbb_code, month)
        if info:
            return info["rank"], info["module_sales"]
        return None, None

    def get_top_modules(self, month: str = None, product_category: str = None,
                        k: int = 30) -> list[dict]:
        """获取 Top-K 模块列表"""
        if not self.has_module_sales:
            return []
        if month is None:
            month = self.get_latest_month()
        if not month:
            return []

        # 按 cbb_code 去重，取 module_sales 最大的记录
        if product_category:
            rows = self.conn.execute(
                """SELECT cbb_code, cbb_name, category, module_sales, MIN(rank) as rank
                   FROM module_monthly_sales
                   WHERE month = ? AND product_category = ?
                   GROUP BY cbb_code
                   ORDER BY module_sales DESC
                   LIMIT ?""",
                (month, product_category, k),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT cbb_code, cbb_name, category, module_sales, MIN(rank) as rank
                   FROM module_monthly_sales
                   WHERE month = ?
                   GROUP BY cbb_code
                   ORDER BY module_sales DESC
                   LIMIT ?""",
                (month, k),
            ).fetchall()
        return [dict(r) for r in rows]

    def match_module_by_name(self, module_name: str, product_category: str = None,
                             month: str = None) -> Optional[dict]:
        """
        模糊匹配模块名 → cbb_code

        匹配策略：
        1. cbb_name 精确匹配
        2. cbb_name 包含匹配
        3. cbb_code 匹配
        """
        if not self.has_module_sales or not module_name:
            return None
        if month is None:
            month = self.get_latest_month()
        if not month:
            return None

        name = module_name.strip()
        category_filter = ""
        params_base = [month]

        if product_category:
            category_filter = " AND product_category = ?"
            params_base.append(product_category)

        # 1. 精确匹配
        row = self.conn.execute(
            f"SELECT * FROM module_monthly_sales WHERE month = ? AND cbb_name = ?{category_filter} LIMIT 1",
            [month, name] + ([product_category] if product_category else []),
        ).fetchone()
        if row:
            return dict(row)

        # 2. cbb_name 包含匹配
        rows = self.conn.execute(
            f"SELECT * FROM module_monthly_sales WHERE month = ? AND cbb_name LIKE ?{category_filter} LIMIT 5",
            [month, f"%{name}%"] + ([product_category] if product_category else []),
        ).fetchall()
        if rows:
            return dict(rows[0])

        # 3. 反向包含：name 包含 cbb_name
        rows = self.conn.execute(
            f"SELECT * FROM module_monthly_sales WHERE month = ?{category_filter}",
            params_base,
        ).fetchall()
        for r in rows:
            cbb_name = r["cbb_name"] or ""
            if cbb_name and cbb_name in name:
                return dict(r)

        # 4. 关键词匹配：拆分name，逐词在cbb_name中查找
        import re as _re
        # 按分隔符拆分
        tokens = [w for w in _re.split(r"[\s,，、+/]+", name) if len(w) >= 2]
        # 如果没有拆出来，做2-gram拆分
        if len(tokens) <= 1 and len(name) > 2:
            tokens = [name[i:i+2] for i in range(len(name) - 1)]
        stop_words = {"系统", "层", "结构", "模块", "组件", "面料", "材质", "设计", "升级", "为", "的", "和", "与"}
        keywords = [w for w in tokens if w not in stop_words]
        best_match = None
        best_score = 0
        for r in rows:
            cbb_name = r["cbb_name"] or ""
            score = sum(1 for kw in keywords if kw in cbb_name)
            if score > best_score:
                best_score = score
                best_match = dict(r)
        if best_match and best_score > 0:
            return best_match

        return None

    def get_module_trend(self, cbb_code: str, months: int = 3) -> dict:
        """
        获取模块近N个月的趋势

        返回:
          {
            "available": True/False,
            "trend": "rising"/"stable"/"falling"/"new"/"unknown",
            "rank_history": [(month, rank, module_sales), ...],
            "rank_change": 最新vs上月排名差（正=上升）,
            "sales_change_pct": 销量变化百分比
          }
        """
        if not self.has_module_sales:
            return {"available": False, "trend": "unknown"}

        all_months = self.get_available_months()
        if not all_months:
            return {"available": False, "trend": "unknown"}

        # 取最近N个月
        recent_months = all_months[:months]
        history = []
        for m in recent_months:
            row = self.conn.execute(
                "SELECT rank, module_sales FROM module_monthly_sales WHERE cbb_code = ? AND month = ?",
                (cbb_code, m),
            ).fetchone()
            if row:
                history.append({"month": m, "rank": row["rank"], "module_sales": row["module_sales"]})

        if not history:
            return {"available": False, "trend": "unknown", "rank_history": []}

        if len(history) == 1:
            return {
                "available": True,
                "trend": "new",
                "rank_history": history,
                "rank_change": 0,
                "sales_change_pct": 0,
            }

        # 计算趋势
        latest = history[0]
        prev = history[1]
        rank_change = (prev["rank"] or 0) - (latest["rank"] or 0)  # 正=排名上升
        sales_change_pct = 0
        if prev["module_sales"] and prev["module_sales"] > 0:
            sales_change_pct = ((latest["module_sales"] or 0) - prev["module_sales"]) / prev["module_sales"] * 100

        if rank_change > 2:
            trend = "rising"
        elif rank_change < -2:
            trend = "falling"
        else:
            trend = "stable"

        return {
            "available": True,
            "trend": trend,
            "rank_history": history,
            "rank_change": rank_change,
            "sales_change_pct": round(sales_change_pct, 1),
        }

    def compare_modules(self, target_cbb_code: str, source_cbb_code: str,
                        month: str = None) -> dict:
        """
        对比两个模块的销量排名

        Returns:
          {
            "target": {...},
            "source": {...},
            "target_is_higher": True/False,
            "rank_diff": 正=目标排名更高,
            "sales_ratio": 目标销量/源销量
          }
        """
        target = self.get_module_by_code(target_cbb_code, month)
        source = self.get_module_by_code(source_cbb_code, month)

        result = {
            "target": target,
            "source": source,
            "target_is_higher": False,
            "rank_diff": 0,
            "sales_ratio": 0,
        }

        if target and source:
            t_rank = target.get("rank") or 999
            s_rank = source.get("rank") or 999
            result["rank_diff"] = s_rank - t_rank  # 正=目标排名更高（数字小=排名高）
            result["target_is_higher"] = t_rank < s_rank
            s_sales = source.get("module_sales") or 1
            result["sales_ratio"] = round((target.get("module_sales") or 0) / s_sales, 2)

        return result
