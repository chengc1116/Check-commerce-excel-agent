# -*- coding: utf-8 -*-
"""
统一产品数据库查询层

为四种审核任务提供统一的数据检索接口：
  - 爆品识别（按二级品类检索，任一月销量>2000）
  - 已起量判断（按二级品类检索，任一月销量>500）
  - 模块检索（产品→CBB模块列表）
  - 品类缺失检查（某品牌在某二级品类下是否存在产品）
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


class ProductQuery:
    """统一产品数据库查询"""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            # 优先用 project_review.db（有cbb模块数据）
            project_db = os.path.join(
                os.path.dirname(__file__), "..", "..", "data", "project_review.db"
            )
            project_db = os.path.normpath(project_db)

            if os.path.exists(project_db):
                db_path = project_db
            else:
                # 降级到 products.db（只有产品+销量）
                db_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "data", "products.db"
                )
                db_path = os.path.normpath(db_path)

        if not os.path.exists(db_path):
            raise FileNotFoundError(f"数据库文件不存在: {db_path}")

        self.db_path = db_path
        import sqlite3
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

        # 检测是否有 cbb 模块表
        tables = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        self.has_cbb = "cbb_modules" in tables and "product_cbb_rel" in tables
        self.has_sales = "sales_records" in tables

        # 一次性缓存 products 表的列名映射
        self._col_map = self._detect_columns()

        logger.debug(f"[ProductQuery] db={db_path}, has_cbb={self.has_cbb}, has_sales={self.has_sales}")
        logger.debug(f"[ProductQuery] col_map={self._col_map}")

    def get_own_brands(self) -> list[str]:
        """获取数据库中所有自有品牌列表"""
        rows = self.conn.execute("SELECT DISTINCT brand FROM products WHERE brand IS NOT NULL AND brand != '' ORDER BY brand").fetchall()
        return [r[0] for r in rows]

    def resolve_brand(self, brand: str) -> tuple[str, bool]:
        """
        解析品牌：判断是自有品牌还是竞品品牌。

        Args:
            brand: 项目书中的品牌字段

        Returns:
            (own_brand, is_competitor)
            - own_brand: 用于数据库查询的自有品牌名（如果是竞品则返回所有自有品牌列表用逗号分隔）
            - is_competitor: True表示项目书中的品牌是竞品品牌
        """
        own_brands = self.get_own_brands()
        if not brand:
            # 没填品牌，用所有自有品牌
            return ", ".join(own_brands), False
        if brand in own_brands:
            return brand, False
        # 项目书中的品牌不在自有品牌列表中 → 是竞品品牌
        # 返回所有自有品牌用于数据库查询
        return ", ".join(own_brands), True

    def _detect_columns(self) -> dict:
        """检测 products 表的实际列名，建立映射"""
        cursor = self.conn.execute("PRAGMA table_info(products)")
        actual_cols = {r[1] for r in cursor.fetchall()}

        # 逻辑名 → 候选列名（按优先级）
        mapping = {
            "sku": ["product_code", "sku"],
            "image": ["image_url", "image_path"],
            "category1": ["category1", "category_l1"],
            "category2": ["category2", "category_l2"],
            "category3": ["category3", "category_l3"],
        }

        result = {}
        for logical, candidates in mapping.items():
            for c in candidates:
                if c in actual_cols:
                    result[logical] = c
                    break
            else:
                result[logical] = candidates[0]  # 降级用第一个

        return result

    def _col(self, logical: str) -> str:
        """获取逻辑列名对应的实际列名"""
        return self._col_map.get(logical, logical)

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ============================================================
    # 爆品识别
    # ============================================================

    def find_hot_products(
        self,
        category_l3: str = "",
        category_l2: str = "",
        brand: str = "",
        threshold: int = 2000,
    ) -> list[dict]:
        """
        查找爆品：任一月销量超过阈值的产品。

        按二级品类检索，只要有一个月销量>=threshold即为爆品。

        Args:
            category_l3: 三级品类（优先）
            category_l2: 二级品类（category_l3为空时使用）
            brand: 品牌（可选筛选）
            threshold: 月销量阈值（默认2000）

        Returns:
            [{"product_code/sku", "brand", "category_l2", "category_l3",
              "image_url/image_path", "hot_months": ["2026-01", ...],
              "max_sales": int, "avg_sales": float, "modules": [...]}]
        """
        if not self.has_sales:
            logger.warning("[ProductQuery] 无销量数据，无法识别爆品")
            return []

        # 按二级品类检索产品（优先 category_l2）
        sku_col = self._col("sku")
        image_col = self._col("image")
        cat2_col = self._col("category2")
        cat3_col = self._col("category3")

        # 第一次尝试：精确匹配 category_l3
        products = self._query_products(
            sku_col, image_col, cat2_col, cat3_col,
            category_l2=category_l2, category_l3=category_l3, brand=brand,
        )

        # 降级：category_l3 精确匹配无结果 → 仅按 category_l2
        if not products and category_l3 and category_l2:
            logger.info(f"[ProductQuery.find_hot] category_l3='{category_l3}' 无结果，降级为 category_l2='{category_l2}'")
            products = self._query_products(
                sku_col, image_col, cat2_col, cat3_col,
                category_l2=category_l2, category_l3="", brand=brand,
            )

        hot_products = []
        for p in products:
            sku = p[0]
            p_brand = p[1]

            # 查销量记录
            sales_col = "sku" if self._col_exists("sales_records", "sku") else "product_code"
            sales_rows = self.conn.execute(
                f"SELECT month, sales_volume FROM sales_records WHERE {sales_col} = ? ORDER BY month DESC",
                (sku,)
            ).fetchall()

            if not sales_rows:
                continue

            # 任一月销量>=threshold即为爆品
            sales_by_month = {r["month"]: r["sales_volume"] for r in sales_rows}
            hot_months = [m for m, s in sales_by_month.items() if s >= threshold]

            if hot_months:
                # 获取模块
                modules = self._get_product_modules(sku)
                max_sales = max(sales_by_month.values())

                hot_products.append({
                    "product_code": sku,
                    "sku": sku,
                    "brand": p_brand,
                    "category_l2": p[2] or "",
                    "category_l3": p[3] or "",
                    "image_url": p[4] or "",
                    "hot_months": hot_months,
                    "max_sales": max_sales,
                    "avg_sales": sum(sales_by_month.values()) / len(sales_by_month),
                    "modules": modules,
                })

        # 按最高月销量降序排列
        hot_products.sort(key=lambda x: x["max_sales"], reverse=True)

        logger.info(f"[ProductQuery] 爆品识别: category_l2={category_l2}, brand={brand}, found={len(hot_products)}")
        return hot_products

    # ============================================================
    # 已起量判断
    # ============================================================

    def check_product_launched(
        self,
        sku: str = "",
        category_l2: str = "",
        brand: str = "",
        threshold: int = 500,
        lookback_months: int = 6,
    ) -> list[dict]:
        """
        检查哪些产品已起量（历史6个月任一月销量>threshold）。

        Args:
            sku: 特定货号（可选）
            category_l2: 二级品类（可选）
            brand: 品牌（可选）
            threshold: 起量阈值
            lookback_months: 回看月数

        Returns:
            [{"product_code/sku", "brand", "category_l3", "launched": bool,
              "max_sales": int, "launched_month": str}]
        """
        if not self.has_sales:
            return []

        # 查产品
        conditions = []
        params = []
        sku_col = self._col("sku")

        if sku:
            conditions.append(f"{sku_col} = ?")
            params.append(sku)
        if category_l2:
            conditions.append(f"{self._col('category2')} = ?")
            params.append(category_l2)
        if brand:
            conditions.append("brand = ?")
            params.append(brand)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cat3_col = self._col("category3")

        products = self.conn.execute(
            f"SELECT {sku_col}, brand, {cat3_col} FROM products {where}",
            params
        ).fetchall()

        results = []
        for p in products:
            p_sku = p[0]
            p_brand = p[1]

            sales_col = "sku" if self._col_exists("sales_records", "sku") else "product_code"
            sales_rows = self.conn.execute(
                f"SELECT month, sales_volume FROM sales_records WHERE {sales_col} = ? ORDER BY month DESC LIMIT ?",
                (p_sku, lookback_months)
            ).fetchall()

            max_sales = 0
            launched_month = ""
            for r in sales_rows:
                if r["sales_volume"] > max_sales:
                    max_sales = r["sales_volume"]
                    launched_month = r["month"]

            results.append({
                "product_code": p_sku,
                "sku": p_sku,
                "brand": p_brand,
                "category_l3": p[2] or "",
                "launched": max_sales > threshold,
                "max_sales": max_sales,
                "launched_month": launched_month,
            })

        return results

    # ============================================================
    # 模块检索
    # ============================================================

    def get_products_with_modules(
        self,
        category_l2: str = "",
        category_l3: str = "",
        brand: str = "",
    ) -> list[dict]:
        """
        查询产品及其关联模块。
        
        自动降级策略：category_l3 精确匹配无结果时，降级为仅按 category_l2 查询。

        Returns:
            [{"product_code", "brand", "category_l2", "category_l3",
              "image_url", "modules": [{cbb_code, cbb_name, category, sub_type, used_position}]}]
        """
        sku_col = self._col("sku")
        image_col = self._col("image")
        cat2_col = self._col("category2")
        cat3_col = self._col("category3")

        # 第一次尝试：精确匹配 category_l3
        products = self._query_products(
            sku_col, image_col, cat2_col, cat3_col,
            category_l2=category_l2, category_l3=category_l3, brand=brand,
        )

        # 降级：category_l3 精确匹配无结果 → 仅按 category_l2
        if not products and category_l3 and category_l2:
            logger.info(f"[ProductQuery] category_l3='{category_l3}' 精确匹配无结果，降级为 category_l2='{category_l2}'")
            products = self._query_products(
                sku_col, image_col, cat2_col, cat3_col,
                category_l2=category_l2, category_l3="", brand=brand,
            )

        results = []
        for p in products:
            p_sku = p[0]
            modules = self._get_product_modules(p_sku)
            results.append({
                "product_code": p_sku,
                "sku": p_sku,
                "brand": p[1],
                "category_l2": p[2] or "",
                "category_l3": p[3] or "",
                "image_url": p[4] or "",
                "modules": modules,
            })

        return results

    def _query_products(
        self,
        sku_col: str,
        image_col: str,
        cat2_col: str,
        cat3_col: str,
        category_l2: str = "",
        category_l3: str = "",
        brand: str = "",
        brands: list[str] | None = None,
    ) -> list:
        """底层产品查询，返回原始行。支持单品牌(brand)或多品牌(brands)筛选。"""
        conditions = []
        params = []

        if category_l2:
            conditions.append(f"{cat2_col} = ?")
            params.append(category_l2)
        if category_l3:
            conditions.append(f"{cat3_col} = ?")
            params.append(category_l3)
        if brands:
            placeholders = ",".join("?" * len(brands))
            conditions.append(f"brand IN ({placeholders})")
            params.extend(brands)
        elif brand:
            conditions.append("brand = ?")
            params.append(brand)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        return self.conn.execute(
            f"SELECT {sku_col}, brand, {cat2_col}, {cat3_col}, {image_col} FROM products {where}",
            params
        ).fetchall()

    def _get_product_modules(self, product_code: str) -> list[dict]:
        """获取产品关联的CBB模块"""
        if not self.has_cbb:
            return []

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

    def get_all_cbb_modules(self, category_l2: str = "", category: str = "") -> list[dict]:
        """
        获取CBB模块库中的模块（可选按品类筛选）。

        用于品类缺失场景的间接复用评估——当公司没有该品类产品时，
        通过CBB模块库判断工厂是否已有可复用的面料/版型/功能组件。

        Args:
            category_l2: 二级品类（用于关联产品筛选）
            category: CBB模块大类（如：面料、版型、功能组件等）

        Returns:
            [{"cbb_code", "cbb_name", "category", "sub_type", "supplier"}]
        """
        if not self.has_cbb:
            return []

        if category:
            rows = self.conn.execute(
                "SELECT cbb_code, cbb_name, category, sub_type, supplier "
                "FROM cbb_modules WHERE category = ? ORDER BY category, sub_type",
                (category,)
            ).fetchall()
        elif category_l2:
            # 通过产品关联找该二级品类下用到的所有CBB模块
            cat2_col = self._col("category2")
            rows = self.conn.execute(
                f"""SELECT DISTINCT m.cbb_code, m.cbb_name, m.category, m.sub_type, m.supplier
                FROM cbb_modules m
                INNER JOIN product_cbb_rel r ON m.cbb_code = r.cbb_code
                INNER JOIN products p ON r.product_code = p.product_code
                WHERE p.{cat2_col} = ?
                ORDER BY m.category, m.sub_type""",
                (category_l2,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT cbb_code, cbb_name, category, sub_type, supplier "
                "FROM cbb_modules ORDER BY category, sub_type"
            ).fetchall()

        return [
            {
                "cbb_code": r["cbb_code"],
                "cbb_name": r["cbb_name"] or "",
                "category": r["category"] or "",
                "sub_type": r["sub_type"] or "",
                "supplier": r["supplier"] or "",
            }
            for r in rows
        ]

    # ============================================================
    # 品类缺失检查
    # ============================================================

    def check_category_gap(
        self,
        category_l2: str,
        brand: str,
        category_l3: str = "",
    ) -> dict:
        """
        检查某品牌在某品类下是否存在类似产品。

        Returns:
            {
                "has_gap": bool,           # True=品类缺失
                "brand": str,
                "category_l2": str,
                "category_l3": str,
                "existing_products": [...], # 已有产品列表
                "gap_description": str,     # 缺失描述
            }
        """
        sku_col = self._col("sku")
        image_col = self._col("image")
        cat2_col = self._col("category2")
        cat3_col = self._col("category3")

        # 第一次尝试：精确匹配
        rows = self._query_products(
            sku_col, image_col, cat2_col, cat3_col,
            category_l2=category_l2, category_l3=category_l3, brand=brand,
        )

        # 降级：category_l3 精确匹配无结果 → 仅按 category_l2
        if not rows and category_l3 and category_l2:
            logger.info(f"[ProductQuery.check_category_gap] category_l3='{category_l3}' 无结果，降级为 category_l2='{category_l2}'")
            rows = self._query_products(
                sku_col, image_col, cat2_col, cat3_col,
                category_l2=category_l2, category_l3="", brand=brand,
            )

        existing = [
            {
                "product_code": r[0],
                "sku": r[0],
                "brand": r[1],
                "category_l2": r[2] or "",
                "category_l3": r[3] or "",
                "image_url": r[4] or "",
                "modules": self._get_product_modules(r[0]),
            }
            for r in rows
        ]

        has_gap = len(existing) == 0

        if has_gap:
            desc = f"品牌「{brand}」在品类「{category_l3 or category_l2}」下无现有产品，属于品类空白。"
        else:
            product_names = [p["product_code"] for p in existing]
            desc = f"品牌「{brand}」在品类「{category_l3 or category_l2}」下已有 {len(existing)} 个产品: {', '.join(product_names)}"

        return {
            "has_gap": has_gap,
            "brand": brand,
            "category_l2": category_l2,
            "category_l3": category_l3,
            "existing_products": existing,
            "gap_description": desc,
        }

    # ============================================================
    # 品类市场概况
    # ============================================================

    def get_category_market_overview(self, category_l2: str, category_l3: str = "") -> dict:
        """
        获取某品类的市场概况：品牌分布、产品数量、销量汇总。

        Returns:
            {
                "category_l2": str,
                "category_l3": str,
                "total_products": int,
                "total_skus_with_sales": int,
                "brand_distribution": [{"brand": str, "product_count": int, "total_sales": int}],
                "top_selling_products": [{"product_code": str, "brand": str, "category_l3": str, "max_sales": int}],
                "total_category_sales": int,
            }
        """
        sku_col = self._col("sku")
        cat2_col = self._col("category2")
        cat3_col = self._col("category3")

        conditions = [f"{cat2_col} = ?"]
        params: list = [category_l2]
        if category_l3:
            conditions.append(f"{cat3_col} = ?")
            params.append(category_l3)
        where = f"WHERE {' AND '.join(conditions)}"

        # 产品总数
        total_products = self.conn.execute(
            f"SELECT COUNT(*) FROM products {where}", params
        ).fetchone()[0]

        # 品牌分布
        brand_rows = self.conn.execute(
            f"SELECT brand, COUNT(*) as cnt FROM products {where} GROUP BY brand ORDER BY cnt DESC",
            params
        ).fetchall()
        brand_distribution = []
        all_skus = []
        for br in brand_rows:
            brand_name = br[0]
            product_count = br[1]
            # 该品牌的销量汇总
            brand_skus = self.conn.execute(
                f"SELECT {sku_col} FROM products {where} AND brand = ?",
                params + [brand_name]
            ).fetchall()
            brand_sku_list = [r[0] for r in brand_skus]
            all_skus.extend(brand_sku_list)

            total_sales = 0
            if self.has_sales and brand_sku_list:
                sales_col = "sku" if self._col_exists("sales_records", "sku") else "product_code"
                placeholders = ",".join("?" * len(brand_sku_list))
                sales_row = self.conn.execute(
                    f"SELECT COALESCE(SUM(sales_volume), 0) FROM sales_records WHERE {sales_col} IN ({placeholders})",
                    brand_sku_list
                ).fetchone()
                total_sales = sales_row[0] if sales_row else 0

            brand_distribution.append({
                "brand": brand_name,
                "product_count": product_count,
                "total_sales": total_sales,
            })

        # Top selling products
        top_selling = []
        if self.has_sales and all_skus:
            sales_col = "sku" if self._col_exists("sales_records", "sku") else "product_code"
            placeholders = ",".join("?" * len(all_skus))
            top_rows = self.conn.execute(
                f"SELECT {sales_col}, MAX(sales_volume) as max_sales FROM sales_records "
                f"WHERE {sales_col} IN ({placeholders}) GROUP BY {sales_col} ORDER BY max_sales DESC LIMIT 10",
                all_skus
            ).fetchall()
            for tr in top_rows:
                p_info = self.conn.execute(
                    f"SELECT brand, {cat3_col} FROM products WHERE {sku_col} = ?",
                    (tr[0],)
                ).fetchone()
                if p_info:
                    top_selling.append({
                        "product_code": tr[0],
                        "brand": p_info[0],
                        "category_l3": p_info[1] or "",
                        "max_sales": tr[1],
                    })

        # 品类总销量
        total_category_sales = sum(bd["total_sales"] for bd in brand_distribution)

        return {
            "category_l2": category_l2,
            "category_l3": category_l3,
            "total_products": total_products,
            "total_skus_with_sales": len(all_skus),
            "brand_distribution": brand_distribution,
            "top_selling_products": top_selling,
            "total_category_sales": total_category_sales,
        }

    # ============================================================
    # 批量销量查询
    # ============================================================

    def get_products_sales(self, skus: list[str]) -> dict[str, list[dict]]:
        """
        批量查询产品的所有月度销量。

        Args:
            skus: 货号列表

        Returns:
            {sku: [{"month": "2025-10", "sales_volume": 123}, ...]}
        """
        if not self.has_sales or not skus:
            return {}

        sales_col = "sku" if self._col_exists("sales_records", "sku") else "product_code"
        placeholders = ",".join("?" * len(skus))
        rows = self.conn.execute(
            f"SELECT {sales_col}, month, sales_volume FROM sales_records "
            f"WHERE {sales_col} IN ({placeholders}) ORDER BY month",
            skus,
        ).fetchall()

        result: dict[str, list[dict]] = {s: [] for s in skus}
        for r in rows:
            sku = r[0]
            if sku in result:
                result[sku].append({
                    "month": r["month"],
                    "sales_volume": r["sales_volume"],
                })
        return result

    # ============================================================
    # 获取产品图片
    # ============================================================

    def get_product_images(self, product_code: str) -> list[str]:
        """获取产品的图片URL/路径列表"""
        image_col = self._col("image")
        sku_col = self._col("sku")

        row = self.conn.execute(
            f"SELECT {image_col} FROM products WHERE {sku_col} = ?",
            (product_code,)
        ).fetchone()

        if not row or not row[0]:
            return []

        # 可能是逗号分隔的多个路径
        return [p.strip() for p in str(row[0]).split(",") if p.strip()]

    # ============================================================
    # 辅助
    # ============================================================

    def _col_exists(self, table: str, col: str) -> bool:
        """检查表中是否存在某列"""
        try:
            cursor = self.conn.execute(f"PRAGMA table_info({table})")
            columns = [r[1] for r in cursor.fetchall()]
            return col in columns
        except Exception:
            return False
