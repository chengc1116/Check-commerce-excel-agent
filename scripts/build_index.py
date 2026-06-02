# -*- coding: utf-8 -*-
"""
索引重建脚本：读取 cbb_modules.json 中的 embedding_text → 构建 FAISS 索引

- 每次人工精修 embedding_text 后手动执行
- 同时生成 index_meta.json（faiss_idx → cbb_code 映射、构建时间戳、模型版本）

用法:
    python scripts/build_index.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import faiss
import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from embedding.config import EMBEDDING_DIM, EMBEDDING_MODEL, FAISS_PATH, JSON_PATH, META_PATH
from embedding.retriever import embed_texts


def build_index():
    if not JSON_PATH.exists():
        print(f"JSON 文件不存在: {JSON_PATH}")
        print("请先运行 python scripts/init_json.py 初始化导出。")
        return

    # 加载 JSON
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        modules = json.load(f)

    if not modules:
        print("JSON 中无记录，无法构建索引。")
        return

    # 提取 embedding_text
    texts = []
    idx_to_code = []
    skipped = 0
    for i, m in enumerate(modules):
        emb_text = m.get("embedding_text", "").strip()
        if not emb_text:
            print(f"警告: 第 {i} 条记录 ({m.get('cbb_code', '?')}) embedding_text 为空，已跳过。")
            skipped += 1
            continue
        texts.append(emb_text)
        idx_to_code.append(m["cbb_code"])

    if not texts:
        print("无有效 embedding_text，无法构建索引。")
        return

    print(f"正在向量化 {len(texts)} 条 embedding_text（模型: {EMBEDDING_MODEL}）...")
    embeddings = embed_texts(texts)
    vectors = np.array(embeddings, dtype="float32")

    # 构建 FAISS 索引（Inner Product，向量已归一化时等价于余弦相似度）
    faiss.normalize_L2(vectors)
    index = faiss.IndexFlatIP(EMBEDDING_DIM)
    index.add(vectors)

    # 写入索引文件（FAISS C++ 后端不支持 Windows Unicode 路径，需经由临时文件中转）
    FAISS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".faiss")
    os.close(tmp_fd)
    try:
        faiss.write_index(index, tmp_path)
        shutil.move(tmp_path, str(FAISS_PATH))
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    # 写入元数据
    meta = {
        "idx_to_cbb_code": idx_to_code,
        "total_modules": len(modules),
        "indexed_count": len(texts),
        "skipped_count": skipped,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": EMBEDDING_DIM,
        "built_at": datetime.now().isoformat(),
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"索引构建完成: {len(texts)} 条向量 → {FAISS_PATH}")
    print(f"元数据 → {META_PATH}")
    print(f"跳过 {skipped} 条（embedding_text 为空）")


def main():
    build_index()


if __name__ == "__main__":
    main()
