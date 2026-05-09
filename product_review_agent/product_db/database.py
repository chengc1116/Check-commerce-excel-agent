# -*- coding: utf-8 -*-
"""
SQLite 产品库管理器 — 产品主表 + 月度销量副表

表结构:
  products        — 产品主表 (id, 品类, 版本, 品牌/渠道, 货号, 图片路径, 状态)
  sales_records   — 月度销量副表 (id, 货号, 月份, 销量)

设计要点:
  - 货号(sku)是唯一键，同一货号跨渠道共享
  - 品牌/渠道(brand)与货号绑定（同一产品不同渠道可能用不同货号）
  - 销量按货号+月份唯一，导入时覆盖更新
  - data/ 目录不入 git，各环境独立积累
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================
# 建表 SQL
# ============================================================

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category1     TEXT,
    category2     TEXT,
    category3     TEXT,
    version         TEXT,
    brand           TEXT,
    sku             TEXT NOT NULL,
    image_path      TEXT,
    status          TEXT DEFAULT 'active',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sku_brand ON products(sku, brand);
CREATE INDEX IF NOT EXISTS idx_category2 ON products(category2);
CREATE INDEX IF NOT EXISTS idx_brand ON products(brand);
CREATE INDEX IF NOT EXISTS idx_status ON products(status);

CREATE TABLE IF NOT EXISTS sales_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sku             TEXT NOT NULL,
    brand           TEXT,
    month           TEXT NOT NULL,
    sales_volume    INTEGER NOT NULL DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sku_brand_month ON sales_records(sku, brand, month);

CREATE TABLE IF NOT EXISTS operation_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       DATETIME DEFAULT CURRENT_TIMESTAMP,
    action          TEXT NOT NULL,
    category        TEXT NOT NULL,
    operator        TEXT DEFAULT 'system',
    target          TEXT,
    detail          TEXT,
    status          TEXT DEFAULT 'success',
    elapsed_ms      INTEGER,
    extra           TEXT
);

CREATE INDEX IF NOT EXISTS idx_oplog_timestamp ON operation_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_oplog_action ON operation_logs(action);
CREATE INDEX IF NOT EXISTS idx_oplog_category ON operation_logs(category);
"""


# ============================================================
# ProductDB
# ============================================================

class ProductDB:
    """产品库管理器"""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            from product_review_agent.config import PRODUCT_DB_PATH
            db_path = PRODUCT_DB_PATH

        # 确保目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        # 检测 products 表实际的货号列名（可能是 sku 或 product_code）
        self._sku_col = self._detect_sku_column()
        # 检测是否有 CBB 模块表
        self._has_cbb = self._check_cbb_tables()
        logger.debug(f"[ProductDB] 已连接: {db_path}, sku_col={self._sku_col}, has_cbb={self._has_cbb}")

    def _detect_sku_column(self) -> str:
        """检测 products 表中货号列的实际名称"""
        try:
            cursor = self.conn.execute("PRAGMA table_info(products)")
            cols = {r[1] for r in cursor.fetchall()}
            if "product_code" in cols:
                return "product_code"
            if "sku" in cols:
                return "sku"
        except Exception:
            pass
        return "sku"  # 降级

    def _init_schema(self):
        """初始化表结构（逐条执行，跳过旧表不兼容的索引）"""
        for stmt in _SCHEMA_SQL.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError as e:
                # 旧表结构不兼容时跳过（如 products 表没有 sku 列导致索引创建失败）
                logger.debug(f"[ProductDB] 跳过SQL: {stmt[:60]}... ({e})")
        self.conn.commit()

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ============================================================
    # 产品主表 CRUD
    # ============================================================

    def insert_product(self, data: dict) -> int:
        """
        插入产品，返回 id。
        如果 sku+brand 已存在则更新。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = self.get_product_by_sku_brand(data["sku"], data.get("brand", ""))

        if existing:
            # 更新已有记录
            sets = []
            params = []
            for key in ["category1", "category2", "category3", "version", "image_path", "status"]:
                if key in data and data[key] is not None:
                    sets.append(f"{key} = ?")
                    params.append(data[key])
            if sets:
                sets.append("updated_at = ?")
                params.append(now)
                params.append(data["sku"])
                params.append(data.get("brand", ""))
                sql = f"UPDATE products SET {', '.join(sets)} WHERE {self._sku_col} = ? AND brand = ?"
                self.conn.execute(sql, params)
                self.conn.commit()
                logger.debug(f"[ProductDB] 更新产品: {data['sku']} ({data.get('brand', '')})")
                return existing["id"]
        else:
            # 插入新记录
            sql = f"""
                INSERT INTO products (category1, category2, category3, version, brand, {self._sku_col}, image_path, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = [
                data.get("category1"),
                data.get("category2"),
                data.get("category3"),
                data.get("version"),
                data.get("brand"),
                data["sku"],
                data.get("image_path"),
                data.get("status", "active"),
                now, now,
            ]
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            logger.debug(f"[ProductDB] 新增产品: {data['sku']} ({data.get('brand', '')})")
            return cursor.lastrowid

    def update_product(self, product_id: int, data: dict) -> bool:
        """更新产品信息"""
        sets = []
        params = []
        for key in ["category1", "category2", "category3", "version", "brand", "sku", "image_path", "status"]:
            if key in data:
                col = self._sku_col if key == "sku" else key
                sets.append(f"{col} = ?")
                params.append(data[key])
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        params.append(product_id)
        self.conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()
        return True

    def get_product(self, product_id: int) -> dict | None:
        """获取单个产品"""
        row = self.conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return dict(row) if row else None

    def get_product_by_sku(self, sku: str) -> dict | None:
        """按货号获取产品（返回第一条匹配）"""
        row = self.conn.execute(f"SELECT * FROM products WHERE {self._sku_col} = ? LIMIT 1", (sku,)).fetchone()
        return dict(row) if row else None

    def get_product_by_sku_brand(self, sku: str, brand: str) -> dict | None:
        """按货号+品牌获取产品"""
        row = self.conn.execute(f"SELECT * FROM products WHERE {self._sku_col} = ? AND brand = ?", (sku, brand)).fetchone()
        return dict(row) if row else None

    def delete_product(self, product_id: int) -> bool:
        """删除产品及关联销量"""
        product = self.get_product(product_id)
        if not product:
            return False
        sku = product.get(self._sku_col) or product.get("sku", "")
        self.conn.execute("DELETE FROM sales_records WHERE sku = ?", (sku,))
        self.conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        self.conn.commit()
        logger.info(f"[ProductDB] 删除产品: {sku} (ID:{product_id})")
        return True

    def archive_product(self, product_id: int) -> bool:
        """归档产品"""
        return self.update_product(product_id, {"status": "archived"})

    def activate_product(self, product_id: int) -> bool:
        """激活产品"""
        return self.update_product(product_id, {"status": "active"})

    # ============================================================
    # 销量副表
    # ============================================================

    def upsert_sales(self, sku: str, month: str, volume: int, brand: str = "") -> bool:
        """插入或更新某货号某品牌某月的销量"""
        sql = """
            INSERT INTO sales_records (sku, brand, month, sales_volume)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sku, brand, month) DO UPDATE SET sales_volume = excluded.sales_volume
        """
        self.conn.execute(sql, (sku, brand, month, volume))
        self.conn.commit()
        return True

    def batch_upsert_sales(self, records: list[dict]) -> dict:
        """
        批量导入销量。
        records: [{"sku": str, "brand": str, "month": str, "sales_volume": int}, ...]
        Returns: {"inserted": N, "updated": M, "skipped": K}
        """
        inserted = 0
        updated = 0
        skipped = 0
        for rec in records:
            sku = rec["sku"]
            brand = rec.get("brand", "")
            # 检查货号+品牌是否存在
            if not self.get_product_by_sku_brand(sku, brand):
                skipped += 1
                logger.warning(f"[ProductDB] 跳过不存在的货号: {sku} ({brand})")
                continue
            # 检查是否已存在
            existing = self.conn.execute(
                "SELECT id FROM sales_records WHERE sku = ? AND brand = ? AND month = ?",
                (sku, brand, rec["month"])
            ).fetchone()
            self.upsert_sales(sku, rec["month"], rec["sales_volume"], brand)
            if existing:
                updated += 1
            else:
                inserted += 1
        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    def get_recent_sales(self, sku: str, months: int = 2, brand: str = "") -> list[dict]:
        """获取某货号最近 N 个月的销量，按月份倒序"""
        if brand:
            rows = self.conn.execute(
                "SELECT * FROM sales_records WHERE sku = ? AND brand = ? ORDER BY month DESC LIMIT ?",
                (sku, brand, months),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM sales_records WHERE sku = ? ORDER BY month DESC LIMIT ?",
                (sku, months),
            ).fetchall()
        return [dict(r) for r in rows]

    def _check_cbb_tables(self) -> bool:
        """检测数据库中是否有 CBB 模块相关表"""
        try:
            tables = [r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            return "cbb_modules" in tables and "product_cbb_rel" in tables
        except Exception:
            return False

    def _get_product_modules(self, product_code: str) -> list[dict]:
        """获取产品关联的 CBB 模块（需要 cbb_modules + product_cbb_rel 表）"""
        if not self._has_cbb:
            return []

        try:
            rows = self.conn.execute(
                """SELECT r.cbb_code, r.used_position,
                          m.cbb_name, m.category, m.sub_type, m.supplier,
                          m.image_front_url
                   FROM product_cbb_rel r
                   LEFT JOIN cbb_modules m ON r.cbb_code = m.cbb_code
                   WHERE r.product_code = ?
                   ORDER BY m.category, r.cbb_code""",
                (product_code,)
            ).fetchall()

            return [
                {
                    "cbb_code": r["cbb_code"],
                    "cbb_name": r["cbb_name"] or "",
                    "category": r["category"] or "",
                    "sub_type": r["sub_type"] or "",
                    "used_position": r["used_position"] or "",
                    "supplier": r["supplier"] or "",
                    "image_front_url": r["image_front_url"] or "",
                }
                for r in rows
            ]
        except Exception as e:
            logger.debug(f"[ProductDB] CBB模块查询失败: {e}")
            return []

    # ============================================================
    # 检索（审核时使用）
    # ============================================================

    def get_products_by_category2(
        self, category2: str, include_archived: bool = False
    ) -> list[dict]:
        """
        按二级品类检索产品，附带最近2月销量和CBB模块。

        Returns:
            [{"id", "sku", "brand", "category1", "category2", "category3",
              "version", "image_path", "status", "recent_sales": [...],
              "modules": [{cbb_code, cbb_name, category, sub_type, used_position, supplier}]}]
        """
        sku_col = self._sku_col
        if include_archived:
            rows = self.conn.execute(
                f"SELECT * FROM products WHERE category2 = ? ORDER BY category3, version, {sku_col}",
                (category2,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT * FROM products WHERE category2 = ? AND status = 'active' ORDER BY category3, version, {sku_col}",
                (category2,),
            ).fetchall()

        result = []
        for row in rows:
            product = dict(row)
            sku_val = product.get(sku_col) or product.get("sku", "")
            product["recent_sales"] = self.get_recent_sales(sku_val, months=2, brand=product.get("brand", ""))
            # 添加 CBB 模块数据
            product["modules"] = self._get_product_modules(sku_val)
            result.append(product)
        return result

    # ============================================================
    # 统计与列表
    # ============================================================

    def get_stats(self) -> dict:
        """产品库统计"""
        total = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        active = self.conn.execute("SELECT COUNT(*) FROM products WHERE status = 'active'").fetchone()[0]
        archived = self.conn.execute("SELECT COUNT(*) FROM products WHERE status = 'archived'").fetchone()[0]
        sales_months = self.conn.execute("SELECT COUNT(DISTINCT month) FROM sales_records").fetchone()[0]

        # 各二级品类分布
        cats = self.conn.execute(
            "SELECT category2, COUNT(*) as cnt FROM products WHERE status = 'active' GROUP BY category2 ORDER BY cnt DESC"
        ).fetchall()

        return {
            "total": total,
            "active": active,
            "archived": archived,
            "sales_months": sales_months,
            "categories": [{"name": r["category2"], "count": r["cnt"]} for r in cats if r["category2"]],
        }

    def list_products(
        self, status: str = None, brand: str = None, category2: str = None,
        offset: int = 0, limit: int = 20,
    ) -> list[dict]:
        """分页查询产品列表"""
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if brand:
            conditions.append("brand = ?")
            params.append(brand)
        if category2:
            conditions.append("category2 = ?")
            params.append(category2)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM products {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def vacuum(self):
        """压缩数据库"""
        self.conn.execute("VACUUM")
        logger.info("[ProductDB] 数据库已压缩")
