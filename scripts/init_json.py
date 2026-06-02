# -*- coding: utf-8 -*-
"""
初始化导出脚本：从 cbb_modules 表读取全部记录 → 生成 cbb_modules.json

- 主键：cbb_code
- 自动拼接 embedding_text = category + cbb_name + sub_type（空格分隔，空值跳过）
- 已有 JSON 文件时会提示确认，避免覆盖人工精修的 embedding_text

用法:
    python scripts/init_json.py
    python scripts/init_json.py --force   # 覆盖已有 JSON
"""

import io
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from embedding.config import DB_PATH, JSON_PATH


def build_embedding_text(record: dict) -> str:
    """从 category + cbb_name + sub_type 拼接 embedding_text，跳过空值。"""
    parts = []
    for key in ("category", "cbb_name", "sub_type"):
        val = record.get(key)
        if val and str(val).strip() and str(val).strip().lower() not in ("none", "null"):
            parts.append(str(val).strip())
    return " ".join(parts)


def export_db_to_json(force: bool = False):
    if JSON_PATH.exists() and not force:
        print(f"文件已存在: {JSON_PATH}")
        print("如需覆盖，请使用 --force 参数。")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM cbb_modules")
    rows = cur.fetchall()
    conn.close()

    modules = []
    for row in rows:
        record = dict(row)
        record["embedding_text"] = build_embedding_text(record)
        modules.append(record)

    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(modules, f, ensure_ascii=False, indent=2)

    print(f"导出完成: {len(modules)} 条记录 → {JSON_PATH}")
    print(f"时间戳: {datetime.now().isoformat()}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="从 DB 导出 cbb_modules.json")
    parser.add_argument("--force", action="store_true", help="覆盖已有 JSON 文件")
    args = parser.parse_args()
    export_db_to_json(force=args.force)


if __name__ == "__main__":
    main()
