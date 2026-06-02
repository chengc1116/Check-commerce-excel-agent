# -*- coding: utf-8 -*-
"""
CBB 模块语义检索测试脚本

测试内容:
  1. 纯语义检索（无过滤，验证 embedding_text 质量）
  2. 返回结果完整性校验（确认每个结果包含全部原始字段）
  3. 输出结果格式化打印（含匹配度分数、cbb_code、cbb_name、embedding_text 预览）

用法:
    python scripts/test_search.py
    python scripts/test_search.py --query "热敷模组" --top-k 10
"""

import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from embedding.config import JSON_PATH
from embedding.retriever import ModuleRetriever

# 预置测试 query 列表
DEFAULT_QUERIES = [
    "热敷模组",
    "SBR 防水面料",
    "发热片 温控",
    "弹力带 束腰",
    "拉链 按扣",
]

# JSON 中应有的原始字段（embedding_text 除外）
REQUIRED_FIELDS = [
    "cbb_code", "size", "cbb_name", "category", "sub_type",
    "status", "version", "specification", "unit", "price",
    "supplier", "image_front_url", "image_back_url",
    "usage_count", "created_at", "updated_at",
]


def check_completeness(results: list[dict]) -> bool:
    """校验每条结果是否包含全部原始字段。"""
    ok = True
    for i, r in enumerate(results):
        for field in REQUIRED_FIELDS:
            if field not in r:
                print(f"  [FAIL] 结果 {i+1} ({r.get('cbb_code', '?')}) 缺失字段: {field}")
                ok = False
    return ok


def print_results(results: list[dict], query: str):
    """格式化打印检索结果。"""
    print(f"\n{'='*60}")
    print(f"Query: {query}")
    print(f"返回 {len(results)} 条结果")
    print(f"{'='*60}")

    if not results:
        print("  （无结果）")
        return

    for i, r in enumerate(results, 1):
        score = r.get("_score", 0)
        print(f"\n  [{i}] {r['cbb_code']}  |  {r.get('cbb_name', '')}")
        print(f"      score: {score:.4f}")
        print(f"      category: {r.get('category', '')}  sub_type: {r.get('sub_type', '')}  price: {r.get('price', '')}")
        emb_text = r.get("embedding_text", "")
        if emb_text:
            preview = emb_text[:100] + ("..." if len(emb_text) > 100 else "")
            print(f"      embedding_text: {preview}")


def run_test(queries: list[str], top_k: int = 5):
    print("正在加载检索器...")
    retriever = ModuleRetriever()
    print(f"模块总数: {retriever.module_count}")

    all_ok = True
    for query in queries:
        results = retriever.search(query, top_k=top_k)
        print_results(results, query)

        # 完整性校验
        if results:
            completeness_ok = check_completeness(results)
            if not completeness_ok:
                all_ok = False
                print(f"  [FAIL] 字段完整性校验未通过")
            else:
                print(f"  [PASS] 字段完整性校验通过")

    print(f"\n{'='*60}")
    if all_ok:
        print("全部测试通过。")
    else:
        print("存在未通过的测试项，请检查。")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CBB 模块语义检索测试")
    parser.add_argument("--query", type=str, help="自定义测试 query（可多次指定）", action="append")
    parser.add_argument("--top-k", type=int, default=5, help="返回结果数量（默认 5）")
    args = parser.parse_args()

    queries = args.query if args.query else DEFAULT_QUERIES
    run_test(queries, top_k=args.top_k)


if __name__ == "__main__":
    main()
