# -*- coding: utf-8 -*-
"""
增量同步脚本：将 DB 中新增的 cbb_code 追加到 cbb_modules.json

- 已有记录的 embedding_text 及其他字段均不被覆盖
- 仅追加 DB 中存在但 JSON 中不存在的 cbb_code

用法:
    python scripts/sync_db_to_json.py
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


def sync():
    if not JSON_PATH.exists():
        print(f"JSON 文件不存在: {JSON_PATH}")
        print("请先运行 python scripts/init_json.py 初始化导出。")
        return

    # 加载已有 JSON
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        modules = json.load(f)
    existing_codes = {m["cbb_code"] for m in modules}

    # 从 DB 读取全部记录
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM cbb_modules")
    rows = cur.fetchall()
    conn.close()

    # 筛选新增记录
    new_count = 0
    for row in rows:
        record = dict(row)
        if record["cbb_code"] not in existing_codes:
            record["embedding_text"] = build_embedding_text(record)
            modules.append(record)
            new_count += 1

    if new_count == 0:
        print("无新增记录，JSON 文件无需更新。")
        return

    # 写回 JSON
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(modules, f, ensure_ascii=False, indent=2)

    print(f"同步完成: 新增 {new_count} 条记录，JSON 现有 {len(modules)} 条。")
    print(f"时间戳: {datetime.now().isoformat()}")


def main():
    sync()


if __name__ == "__main__":
    main()
