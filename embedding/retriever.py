# -*- coding: utf-8 -*-
"""
CBB 模块语义检索器

用法:
    from embedding.retriever import ModuleRetriever
    retriever = ModuleRetriever()
    results = retriever.search("热敷模组", top_k=5)
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import faiss
import httpx

from .config import (
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    FAISS_PATH,
    JSON_PATH,
    META_PATH,
)


def embed_texts(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """调用 SiliconFlow Embedding API，返回向量列表。"""
    all_embeddings: list[list[float]] = []
    url = f"{EMBEDDING_BASE_URL.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {EMBEDDING_API_KEY}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=60) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = client.post(url, headers=headers, json={
                "model": EMBEDDING_MODEL,
                "input": batch,
            })
            resp.raise_for_status()
            data = resp.json()
            # 按 index 排序，确保顺序与输入一致
            sorted_items = sorted(data["data"], key=lambda x: x["index"])
            all_embeddings.extend(item["embedding"] for item in sorted_items)

    return all_embeddings


class ModuleRetriever:
    """CBB 模块语义检索器 — 加载 JSON + FAISS，提供 search 方法。"""

    def __init__(
        self,
        json_path: str | Path | None = None,
        faiss_path: str | Path | None = None,
        meta_path: str | Path | None = None,
    ):
        self._json_path = Path(json_path) if json_path else JSON_PATH
        self._faiss_path = Path(faiss_path) if faiss_path else FAISS_PATH
        self._meta_path = Path(meta_path) if meta_path else META_PATH

        # 加载模块数据（cbb_code → 完整记录）
        with open(self._json_path, "r", encoding="utf-8") as f:
            modules = json.load(f)
        self._modules: dict[str, dict] = {m["cbb_code"]: m for m in modules}

        # 加载 FAISS 索引（FAISS C++ 后端不支持 Windows Unicode 路径，需经由临时文件中转）
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".faiss")
        os.close(tmp_fd)
        try:
            shutil.copy(str(self._faiss_path), tmp_path)
            self._index = faiss.read_index(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        # 加载元数据（faiss_idx → cbb_code）
        with open(self._meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self._idx_to_code: list[str] = meta["idx_to_cbb_code"]

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        语义检索，返回按相似度降序排列的完整模块记录列表。

        每条记录包含原始 JSON 全部字段，额外附加 `_score` 字段。
        """
        # 1. 向量化 query
        query_vec = embed_texts([query])[0]

        # 2. FAISS 检索（top_k 扩召）
        import numpy as np
        q = np.array([query_vec], dtype="float32")
        scores, indices = self._index.search(q, top_k)

        # 3. 组装结果
        results: list[dict] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            cbb_code = self._idx_to_code[idx]
            record = dict(self._modules[cbb_code])
            record["_score"] = float(score)
            results.append(record)

        return results

    @property
    def module_count(self) -> int:
        return len(self._modules)
