# -*- coding: utf-8 -*-
"""
CBB 模块语义检索 — 命令行入口

用法:
    python -m embedding.search "热敷模组" --top-k 5
"""

import argparse
import json
import sys

from .retriever import ModuleRetriever


def main():
    parser = argparse.ArgumentParser(description="CBB 模块语义检索")
    parser.add_argument("query", help="检索关键词 / 自然语言描述")
    parser.add_argument("--top-k", type=int, default=5, help="返回结果数量（默认 5）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = parser.parse_args()

    retriever = ModuleRetriever()
    results = retriever.search(args.query, top_k=args.top_k)

    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return

    if not results:
        print("未找到匹配模块。")
        return

    for i, r in enumerate(results, 1):
        score = r.get("_score", 0)
        print(f"[{i}] {r['cbb_code']}  |  {r.get('cbb_name', '')}  |  score={score:.4f}")
        print(f"    category={r.get('category', '')}  sub_type={r.get('sub_type', '')}  price={r.get('price', '')}")
        emb_text = r.get("embedding_text", "")
        if emb_text:
            preview = emb_text[:80] + ("..." if len(emb_text) > 80 else "")
            print(f"    embedding_text: {preview}")
        print()


if __name__ == "__main__":
    main()
