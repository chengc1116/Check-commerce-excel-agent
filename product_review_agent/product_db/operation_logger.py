# -*- coding: utf-8 -*-
"""
操作记录器 — 记录系统内所有关键行为到 operation_logs 表

表结构:
  operation_logs — 操作日志 (id, 时间, 动作, 分类, 操作者, 目标, 详情, 状态, 耗时, 扩展信息)

分类(category):
  - review:     审核流程（解析/分析/评分/报告生成）
  - product:    产品库操作（导入/修改/删除）
  - system:     系统行为（启动/配置/异常）

用法:
    from product_review_agent.product_db.operation_logger import OperationLogger

    logger = OperationLogger()

    # 简单记录
    logger.log("excel_parse", "review", target="xxx.xlsx", detail="解析完成")

    # 带耗时
    logger.log("pipeline_complete", "review", target="xxx.xlsx",
               detail="综合分75", elapsed_ms=12300, status="success")

    # 批量查询
    logs = logger.query(category="review", limit=20)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from typing import Optional

from product_review_agent.product_db.database import ProductDB

logger = logging.getLogger(__name__)


class OperationLogger:
    """操作记录器"""

    def __init__(self, db_path: str | None = None):
        self._db = ProductDB(db_path)

    def close(self):
        if self._db:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ============================================================
    # 写入
    # ============================================================

    def log(
        self,
        action: str,
        category: str,
        *,
        operator: str = "system",
        target: str | None = None,
        detail: str | None = None,
        status: str = "success",
        elapsed_ms: int | None = None,
        extra: dict | None = None,
    ) -> int:
        """
        记录一条操作日志。

        Args:
            action:   动作标识（如 excel_parse, pipeline_complete, product_import）
            category: 分类（review / product / system）
            operator: 操作者（默认 system）
            target:   操作目标（如文件名、产品货号）
            detail:   详情描述
            status:   状态（success / failed / partial）
            elapsed_ms: 耗时毫秒
            extra:    扩展信息（dict，自动序列化为JSON）

        Returns:
            插入的记录 ID
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None

        try:
            cursor = self._db.conn.execute(
                """INSERT INTO operation_logs
                   (timestamp, action, category, operator, target, detail, status, elapsed_ms, extra)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, action, category, operator, target, detail, status, elapsed_ms, extra_json),
            )
            self._db.conn.commit()
            return cursor.lastrowid
        except Exception as e:
            logger.warning(f"[OperationLogger] 写入操作日志失败: {e}")
            return -1

    # ============================================================
    # 便捷方法：带耗时计算
    # ============================================================

    def log_with_timer(self, action: str, category: str, **kwargs):
        """
        返回一个上下文管理器，自动计算耗时并记录日志。

        用法:
            with op_logger.log_with_timer("pipeline_complete", "review", target="xxx.xlsx") as ctx:
                # ... 执行操作 ...
                ctx.detail = "综合分75"
        """
        return _LogTimer(self, action, category, **kwargs)

    # ============================================================
    # 查询
    # ============================================================

    def query(
        self,
        *,
        category: str | None = None,
        action: str | None = None,
        operator: str | None = None,
        status: str | None = None,
        target: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """查询操作日志，支持多种过滤条件"""
        conditions = []
        params = []

        if category:
            conditions.append("category = ?")
            params.append(category)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if operator:
            conditions.append("operator = ?")
            params.append(operator)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if target:
            conditions.append("target LIKE ?")
            params.append(f"%{target}%")
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM operation_logs {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._db.conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            r = dict(row)
            # 反序列化 extra
            if r.get("extra"):
                try:
                    r["extra"] = json.loads(r["extra"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(r)
        return results

    def count(
        self,
        *,
        category: str | None = None,
        action: str | None = None,
        status: str | None = None,
    ) -> int:
        """统计操作日志条数"""
        conditions = []
        params = []

        if category:
            conditions.append("category = ?")
            params.append(category)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT COUNT(*) FROM operation_logs {where}"
        return self._db.conn.execute(sql, params).fetchone()[0]

    def get_recent(self, limit: int = 20) -> list[dict]:
        """获取最近 N 条操作日志"""
        return self.query(limit=limit)

    def get_stats(self) -> dict:
        """操作日志统计"""
        total = self._db.conn.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]

        # 按分类统计
        cat_rows = self._db.conn.execute(
            "SELECT category, COUNT(*) as cnt FROM operation_logs GROUP BY category ORDER BY cnt DESC"
        ).fetchall()

        # 按动作统计（Top 10）
        act_rows = self._db.conn.execute(
            "SELECT action, COUNT(*) as cnt FROM operation_logs GROUP BY action ORDER BY cnt DESC LIMIT 10"
        ).fetchall()

        # 成功率
        success_count = self._db.conn.execute(
            "SELECT COUNT(*) FROM operation_logs WHERE status = 'success'"
        ).fetchone()[0]

        return {
            "total": total,
            "success_rate": round(success_count / total * 100, 1) if total > 0 else 0,
            "by_category": [{"name": r["category"], "count": r["cnt"]} for r in cat_rows],
            "top_actions": [{"name": r["action"], "count": r["cnt"]} for r in act_rows],
        }


# ============================================================
# 计时上下文管理器
# ============================================================

class _LogTimer:
    """自动计时的日志记录上下文管理器"""

    def __init__(self, logger: OperationLogger, action: str, category: str, **kwargs):
        self._logger = logger
        self._action = action
        self._category = category
        self._kwargs = kwargs
        self.detail: str | None = kwargs.get("detail")
        self.status: str = "success"
        self.extra: dict | None = kwargs.get("extra")
        self._start: float = 0

    def __enter__(self):
        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed_ms = int((time.time() - self._start) * 1000)
        if exc_type:
            self.status = "failed"
            self.detail = f"{self.detail or ''} [异常: {exc_val}]".strip()

        self._logger.log(
            self._action,
            self._category,
            operator=self._kwargs.get("operator", "system"),
            target=self._kwargs.get("target"),
            detail=self.detail,
            status=self.status,
            elapsed_ms=elapsed_ms,
            extra=self.extra,
        )
