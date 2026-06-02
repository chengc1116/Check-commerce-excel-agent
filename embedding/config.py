# -*- coding: utf-8 -*-
"""
Embedding 模块配置 — 路径、模型、API 密钥

API Key 和 Base URL 优先读取 EMBEDDING_* 环境变量，
未设置时回退到 LLM_* 环境变量（复用同一 SiliconFlow 账户）。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载工作区根目录 .env
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# ---- 目录 / 文件路径 ----
EMBEDDING_DIR = _PROJECT_ROOT / "embedding"
JSON_PATH = EMBEDDING_DIR / "cbb_modules.json"
FAISS_PATH = EMBEDDING_DIR / "index.faiss"
META_PATH = EMBEDDING_DIR / "index_meta.json"
DB_PATH = _PROJECT_ROOT / "data" / "project_review.db"

# ---- Embedding API ----
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")

# ---- 向量维度（bge-m3 输出 1024 维）----
EMBEDDING_DIM = 1024
