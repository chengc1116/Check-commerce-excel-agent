# Product-Review-Agent（产品立项审核助手）

电商产品立项智能审核系统。上传产品研发输入表（Excel），自动解析并对**目标人群**、**使用场景**、**九宫格竞品分析**三大维度进行 LLM 智能评分，输出专业审核报告。

支持**飞书 Bot 对话式审核**和**命令行独立审核**两种方式。

## 功能特性

- **模板解析** — 自动识别 Excel 研发输入表结构，提取立项关键信息
- **三维评分** — 人群 / 场景 / 九宫格竞品分析，每维度 4 个子项独立打分
- **LLM 驱动** — 兼容所有 OpenAI 协议供应商（SiliconFlow / DeepSeek / 智谱 / 通义 / Ollama）
- **飞书集成** — WebSocket 长连接模式，无需公网 IP，发送 Excel 即可获得审核报告
- **错误诊断** — 评分失败时区分 5 种原因（未配置 / API错误 / 超时 / 解析异常 / 数据为空）

## 技术栈

- Python 3.12
- openpyxl — Excel 解析
- openai SDK — LLM 统一调用层
- lark-oapi — 飞书 WebSocket 长连接

## 项目结构

```
project_root/
├── .env                        # 环境变量（API Key 等，不入库）
├── requirements.txt            # Python 依赖
├── run_feishu_bot.py           # 飞书 Bot 启动入口
├── sample_product_review.xlsx  # 示例审核模板
│
├── product_review_agent/       # 核心代码包
│   ├── config.py               # 全局配置
│   ├── load_env.py             # .env 环境变量加载器
│   ├── reviewer.py             # 核心审核逻辑（解析 + 评分 + 报告生成）
│   ├── agents/
│   │   └── llm_client.py       # LLM 客户端封装（同步 + 异步）
│   ├── parsers/
│   │   └── template_parser.py  # Excel 模板解析器
│   └── feishu/
│       ├── bot.py              # 飞书 Bot 事件处理与异步审核调度
│       ├── card_builder.py     # 飞书消息卡片构建器
│       └── file_handler.py     # 飞书文件下载处理
│
└── scripts/
    └── review_from_template.py # 命令行审核入口
```

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

### 2. 配置环境变量

复制 `.env` 文件并填入你的 API Key：

```ini
# LLM 配置
LLM_PROVIDER=siliconflow                    # 支持: siliconflow/deepseek/zhipu/qwen/ollama
LLM_API_KEY=sk-xxxxxxxx                     # 你的 API Key
LLM_MODEL=Qwen/Qwen3.5-27B                  # 可选，有默认值

# 飞书配置（已内置默认值，可选覆盖）
FEISHU_APP_ID=cli_a95f771655fa1bce
FEISHU_APP_SECRET=jBeC63k7Mcts4yRuZIOW9gfKuI8WaRO8
```

### 3a. 命令行审核

```bash
.venv\Scripts\python.exe scripts/review_from_template.py 产品立项表.xlsx
```

### 3b. 飞书 Bot 审核

```bash
.venv\Scripts\python.exe run_feishu_bot.py
```

启动后，在飞书群里 @Bot 并发送 Excel 文件即可自动审核，结果以消息卡片形式返回。

## LLM 供应商配置

项目兼容所有 OpenAI 协议供应商，通过 `.env` 切换：

```ini
# SiliconFlow（默认）
LLM_PROVIDER=siliconflow
LLM_API_KEY=sk-xxxx
LLM_MODEL=Qwen/Qwen3.5-27B

# DeepSeek
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-xxxx

# 智谱
LLM_PROVIDER=zhipu
LLM_API_KEY=xxxx

# Ollama（本地部署）
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:27b
```

优先级：命令行 `set` 环境变量 > `.env` 文件 > 代码默认值。

## 评分维度说明

| 维度 | 子项 | 数据来源 |
|------|------|---------|
| **目标人群** | 明确性 / 数据支撑 / 痛点分析 / 细分合理性 | Excel 人群字段 |
| **使用场景** | 清晰度 / 覆盖完整性 / 需求分析 / 价值评估 | Excel 场景字段 |
| **九宫格竞品** | 信息完整度 / 数据严谨性 / 逻辑自洽性 / 分析深度 | Excel 竞品+目标字段 |

每个子项 0-25 分，维度总分 0-100 分。LLM 不可用时自动降级为规则引擎评分（每项 10 分），并在报告中标注原因。
