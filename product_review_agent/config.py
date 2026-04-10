# 产品立项审核Agent 全局配置

import os

# 服务配置
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))
DEBUG = os.getenv("DEBUG", "true").lower() == "true"

# 上传文件配置
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {".docx", ".doc", ".xlsx", ".xls"}

# ============================================================
# LLM 配置（多Agent智能评估核心）
# 通过环境变量设置，支持 openai / deepseek / zhipu / qwen / ollama
# ============================================================
# LLM_PROVIDER   = openai | deepseek | zhipu | qwen | ollama
# LLM_API_KEY    = 你的 API Key
# LLM_MODEL      = gpt-4o | deepseek-chat | glm-4 | qwen-plus 等
# LLM_BASE_URL   = 自定义 API 端点（兼容 OpenAI 格式）
# LLM_TEMPERATURE = 0.3 (默认)
# LLM_MAX_TOKENS  = 2048 (默认)
#
# 未设置 LLM_API_KEY 时，自动回退到规则引擎模式

# 评估配置
MIN_CONFIDENCE_SCORE = 0.6  # 最低置信度阈值
PROPORTION_SUM_TOLERANCE = 0.05  # 占比总和容差（±5%）

# ============================================================
# 飞书配置（长连接WebSocket模式）
# ============================================================
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")
# 飞书连接模式: "websocket" (长连接,无需公网IP) | "webhook" (需要公网URL)
FEISHU_MODE = os.getenv("FEISHU_MODE", "websocket")

# 常用关键词库（规则引擎回退模式使用）
SCENARIO_NEEDS_KEYWORDS = [
    "减震", "稳定", "支撑", "透气", "防滑", "轻便", "耐磨", "舒适",
    "防水", "保暖", "缓震", "回弹", "包裹", "抓地", "抗扭转",
    "防护", "弹力", "速干", "吸汗", "抗菌", "减压"
]

PERSONA_LABELS = {
    "age_patterns": [
        r"(\d+)[-~—](\d+)\s*岁?",
        r"(\d+)[+-]\s*岁?",
        r"(\d+)\s*岁(?:以下|以上|左右)?",
    ],
    "gender_patterns": ["男", "女", "男性", "女性", "不限"],
}

