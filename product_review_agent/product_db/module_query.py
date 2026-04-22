# -*- coding: utf-8 -*-
"""
产品-模块关联查询器

功能: 根据二级品类(category_l2)查询产品及其关联的CBB模块信息
数据库: ./data/project_review.db
  - products 表: 产品主表(product_code, category1/2/3, brand, ...)
  - cbb_modules 表: CBB模块表(cbb_code, cbb_name, category, sub_type, ...)
  - product_cbb_rel 表: 关联表(product_code, cbb_code, used_position, ...)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ProductModuleQuery:
    """产品-模块关联查询器"""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "project_review.db")
            db_path = os.path.normpath(db_path)

        if not os.path.exists(db_path):
            raise FileNotFoundError(f"数据库文件不存在: {db_path}")

        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        logger.debug(f"[ProductModuleQuery] 已连接: {db_path}")

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ============================================================
    # 核心: 按二级品类查询产品+关联模块
    # ============================================================

    def query_by_category_l2(self, category_l2: str) -> dict:
        """
        根据二级品类查询产品及其关联CBB模块。

        Args:
            category_l2: 二级品类名称，如"健身护腕"、"骨折护膝"

        Returns:
            {
                "category_l2": str,
                "product_count": int,
                "products": [
                    {
                        "product_code": str,
                        "category1": str,
                        "category2": str,
                        "category3": str,
                        "brand": str,
                        "status": str,
                        "image_url": str,
                        "modules": [
                            {
                                "cbb_code": str,
                                "cbb_name": str,
                                "category": str,
                                "sub_type": str,
                                "used_position": str,
                                "supplier": str,
                            }
                        ]
                    }
                ],
                "module_summary": {
                    "total_modules": int,
                    "by_category": { "FABRIC": N, "PATTERN": N, ... },
                    "shared_modules": [  # 多产品共用的模块
                        {
                            "cbb_code": str,
                            "cbb_name": str,
                            "category": str,
                            "used_in_products": [str, ...],
                            "used_count": int
                        }
                    ]
                }
            }
        """
        # 1. 查询该品类下的所有产品
        products = self.conn.execute(
            """SELECT product_code, category1, category2, category3, 
                      version, brand, status, image_url
               FROM products 
               WHERE category2 = ?
               ORDER BY product_code, brand""",
            (category_l2,)
        ).fetchall()

        if not products:
            return {
                "category_l2": category_l2,
                "product_count": 0,
                "products": [],
                "module_summary": {"total_modules": 0, "by_category": {}, "shared_modules": []}
            }

        # 2. 获取所有产品编号
        product_codes = [p["product_code"] for p in products]

        # 3. 批量查询关联的模块信息（JOIN cbb_modules）
        placeholders = ",".join(["?"] * len(product_codes))
        rel_rows = self.conn.execute(
            f"""SELECT r.product_code, r.cbb_code, r.used_position,
                       m.cbb_name, m.category, m.sub_type, m.supplier,
                       m.size, m.price, m.image_front_url
                FROM product_cbb_rel r
                LEFT JOIN cbb_modules m ON r.cbb_code = m.cbb_code
                WHERE r.product_code IN ({placeholders})
                ORDER BY r.product_code, m.category, r.cbb_code""",
            product_codes
        ).fetchall()

        # 4. 构建产品→模块映射
        product_modules = defaultdict(list)
        for row in rel_rows:
            product_modules[row["product_code"]].append({
                "cbb_code": row["cbb_code"],
                "cbb_name": row["cbb_name"] or "",
                "category": row["category"] or "",
                "sub_type": row["sub_type"] or "",
                "used_position": row["used_position"] or "",
                "supplier": row["supplier"] or "",
                "size": row["size"] or "",
                "price": row["price"],
                "image_front_url": row["image_front_url"] or "",
            })

        # 5. 组装结果
        result_products = []
        for p in products:
            result_products.append({
                "product_code": p["product_code"],
                "category1": p["category1"] or "",
                "category2": p["category2"] or "",
                "category3": p["category3"] or "",
                "version": p["version"] or "",
                "brand": p["brand"] or "",
                "status": p["status"] or "",
                "image_url": p["image_url"] or "",
                "modules": product_modules.get(p["product_code"], [])
            })

        # 6. 模块汇总统计
        # 6a. 按category统计
        all_module_cats = defaultdict(int)
        for row in rel_rows:
            if row["category"]:
                all_module_cats[row["category"]] += 1

        # 6b. 共用模块（被多个产品引用的模块）
        module_product_map = defaultdict(list)
        for row in rel_rows:
            module_product_map[row["cbb_code"]].append(row["product_code"])

        shared_modules = []
        for cbb_code, prod_list in module_product_map.items():
            unique_prods = list(set(prod_list))
            if len(unique_prods) > 1:
                # 获取模块详情
                mod = self.conn.execute(
                    "SELECT cbb_name, category, sub_type FROM cbb_modules WHERE cbb_code = ?",
                    (cbb_code,)
                ).fetchone()
                shared_modules.append({
                    "cbb_code": cbb_code,
                    "cbb_name": mod["cbb_name"] if mod else "",
                    "category": mod["category"] if mod else "",
                    "used_in_products": unique_prods,
                    "used_count": len(unique_prods)
                })

        # 按引用次数降序
        shared_modules.sort(key=lambda x: x["used_count"], reverse=True)

        return {
            "category_l2": category_l2,
            "product_count": len(result_products),
            "products": result_products,
            "module_summary": {
                "total_modules": len(rel_rows),
                "by_category": dict(all_module_cats),
                "shared_modules": shared_modules
            }
        }

    # ============================================================
    # 辅助查询
    # ============================================================

    def list_category_l2(self) -> list[str]:
        """列出所有二级品类"""
        rows = self.conn.execute(
            "SELECT DISTINCT category2 FROM products WHERE category2 IS NOT NULL AND category2 != '' ORDER BY category2"
        ).fetchall()
        return [r["category2"] for r in rows]

    def get_product_modules(self, product_code: str) -> list[dict]:
        """查询单个产品的所有关联模块"""
        rows = self.conn.execute(
            """SELECT r.cbb_code, r.used_position,
                       m.cbb_name, m.category, m.sub_type, m.supplier,
                       m.size, m.price, m.image_front_url
                FROM product_cbb_rel r
                LEFT JOIN cbb_modules m ON r.cbb_code = m.cbb_code
                WHERE r.product_code = ?
                ORDER BY m.category, r.cbb_code""",
            (product_code,)
        ).fetchall()

        return [dict(r) for r in rows]

    def get_module_products(self, cbb_code: str) -> list[dict]:
        """查询使用某个模块的所有产品"""
        rows = self.conn.execute(
            """SELECT p.product_code, p.category2, p.category3, p.brand, p.status,
                       r.used_position
                FROM product_cbb_rel r
                LEFT JOIN products p ON r.product_code = p.product_code
                WHERE r.cbb_code = ?
                ORDER BY p.category2, p.product_code""",
            (cbb_code,)
        ).fetchall()

        return [dict(r) for r in rows]
