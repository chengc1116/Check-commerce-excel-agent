# Product-Review-Agent（产品立项智能审核系统）

电商产品立项智能审核系统。上传产品研发输入表（Excel），自动解析并按**四种立项类型**执行专项分析 + 公共分析，量化打分输出专业审核报告。

支持 **飞书 Bot 对话式审核**、**Web UI 审核**和**命令行审核**三种方式。

---

## ✨ 功能特性

### 四种立项类型专项分析

| 类型 | 标识 | 核心问题 | 打分维度 |
|------|------|---------|---------|
| 🔥 爆品升级 | `hot_upgrade` | 爆品模块 vs 竞品差距在哪？ | 差距(30) + 可行性(25) + 复用(25) + 市场(20) |
| ⚔️ 竞品升级 | `competitor_upgrade` | 竞品哪些卖点可复制/超越？ | 理解(25) + 差异(25) + 复制(25) + 超越(25) |
| 📉 未起量迭代 | `low_sale_iterate` | 未起量产品的迭代方向？ | 方向(25) + 复用(25) + 增量(25) + 风险(25) |
| 🗺️ 品类缺失 | `category_gap` | 品类空白区是否值得进入？ | 市场(30) + 复用(25) + 门槛(25) + 风险(20) |

### 核心能力

- **LLM 语义解析** — 不依赖固定模板，LLM 自动理解 Excel 结构并映射字段
- **多模态拆解** — VL 视觉模型拆解产品/竞品图片，提取模块清单
- **CBB 模块库联动** — 自动检索产品库中的 CBB 模块，评估复用率
- **销量数据驱动** — 爆品识别（月销 >2000）、已起量判断（月销 >500），报告含月度销量明细
- **量化打分** — 每种分析器 4 个维度共 100 分，基于客观指标自动计算，不依赖 LLM 二次打分
- **并行 Pipeline** — Excel 解析 ∥ 图片提取、公共分析 ∥ 专项分析 ∥ 同类产品分析，最大化并行

---

## 🛠 技术栈

- **Python 3.12** + openpyxl + openai SDK + lark-oapi
- **LLM 三模型体系**: GLM-5(深度) / Qwen2.5-7B(快速) / Qwen3-VL-8B(视觉)
- **SQLite** 产品库（products + cbb_modules + sales_records）
- **FastAPI** Web UI 后端 + 原生前端
- **飞书** WebSocket 长连接 Bot

---

## 📁 项目结构

```
project_root/
├── .env                            # 环境变量（API Key 等，不入库）
├── requirements.txt                # Python 依赖
├── run_feishu_bot.py               # 飞书 Bot 启动入口
├── web_app.py                      # Web UI 启动入口（FastAPI，端口 8080）
│
├── product_review_agent/           # 核心代码包
│   ├── pipeline.py                 # 全流程异步编排器（run_pipeline 入口）
│   ├── config.py                   # 全局配置
│   ├── load_env.py                 # .env 环境变量加载
│   ├── reviewer.py                 # 公共评分逻辑（人群/场景 LLM 评分）
│   │
│   ├── agents/
│   │   └── llm_client.py          # LLM 统一调用层（三模型 + JSON 修复）
│   │
│   ├── analyzers/
│   │   ├── base.py                # 分析器基类 + 量化打分 + format_product_detail()
│   │   ├── hot_upgrade_analyzer.py       # 🔥 爆品升级分析器
│   │   ├── competitor_upgrade_analyzer.py # ⚔️ 竞品升级分析器
│   │   ├── low_sale_iterate_analyzer.py   # 📉 未起量迭代分析器
│   │   ├── category_gap_analyzer.py       # 🗺️ 品类缺失分析器
│   │   └── module_vision.py              # VL 视觉模型模块拆解
│   │
│   ├── parsers/
│   │   ├── excel_parsing_agent.py  # LLM 语义解析器（Excel → JSON）
│   │   └── excel_image_extractor.py # Excel 图片提取（浮动 + WPS 嵌入）
│   │
│   ├── product_db/
│   │   ├── database.py            # SQLite 管理器
│   │   ├── product_query.py       # 统一查询层（爆品/起量/模块/品类缺失）
│   │   ├── inventory_parser.py    # 货盘表解析器
│   │   ├── conflict_analyzer.py   # LLM 冲突分析
│   │   ├── operation_logger.py    # 操作记录器
│   │   └── module_query.py        # CBB 模块查询
│   │
│   └── feishu/
│       ├── bot.py                 # 飞书 Bot 事件处理 + 异步审核调度
│       ├── file_handler.py        # 飞书文件下载
│       ├── card_builder.py        # 消息卡片构建器
│       └── session_manager.py     # 用户会话状态管理
│
├── static/                         # Web UI 前端
│   ├── index.html
│   ├── css/
│   └── js/                        # auth / api / app / products / cbb / history / import
│
└── data/
    └── project_review.db           # SQLite 数据库（不入库，各环境独立）
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

### 2. 配置环境变量

复制 `.env` 文件并填入 API Key：

```ini
# LLM 配置（默认 SiliconFlow）
LLM_API_KEY=sk-xxxxxxxx

# 三模型体系（可选覆盖）
LLM_MODEL=Pro/zai-org/GLM-5              # 深度文本
LLM_FAST_MODEL=Qwen/Qwen2.5-7B-Instruct  # 快速文本（Excel 解析等）
LLM_VL_MODEL=Qwen/Qwen3-VL-8B-Instruct   # 视觉模型（图片拆解）

# 飞书配置（已内置默认值，可选覆盖）
FEISHU_APP_ID=cli_a95f771655fa1bce
FEISHU_APP_SECRET=xxxxxxxx
```

### 3. 启动服务

```bash
# Web UI
.venv\Scripts\python.exe web_app.py

# 飞书 Bot
.venv\Scripts\python.exe run_feishu_bot.py
```

---

## 📋 审核流程

```
Excel 上传
  │
  ├── Step 1: Excel 解析（LLM 语义） ∥ 图片提取
  │
  ├── Step 2: 公共分析 ∥ 专项分析 ∥ 同类产品分析
  │     │              │               │
  │     ├ 人群评分     ├ 🔥/⚔️/📉/🗺️  ├ 产品库检索
  │     └ 场景评分     ├ VL 图片拆解    └ 销量趋势
  │                    ├ CBB 模块对比
  │                    └ 量化打分
  │
  └── Step 3: 报告整合（7 大板块）
        一、立项信息
        二、市场与定价
        三、人群分析 (权重 20%)
        四、场景分析 (权重 20%)
        五、专项分析 (权重 60%)
        六、同类产品及销售情况
        七、综合评估 → 评分 + 风险等级 + 建议
```

### 专项分析详解

**🔥 爆品升级**：按二级品类检索月销 >2000 的爆品 → 获取其 CBB 模块 → VL 拆解竞品图片 → 逐模块对比 → 差距矩阵 + 升级优先级

**⚔️ 竞品升级**：提取竞品卖点（复制/超越）→ 获取我方同品类产品模块 → VL 拆解竞品图片 → 差异化策略 + 超越方案

**📉 未起量迭代**：按二级品类检索所有产品 → 标记已起量（月销 >500）→ 汇总可复用模块 → 对比竞品 → 迭代方向 + 风险评估

**🗺️ 品类缺失**：检查品牌在某品类下是否有产品 → VL 拆解参考图片 → 基于现有 CBB 模块库给出组合方案 → 评估市场空白风险

---

## 💾 数据库

所有数据统一存储在 `data/project_review.db`（SQLite）：

| 表 | 记录数 | 说明 |
|----|--------|------|
| `products` | 194 | 产品信息（货号/品牌/品类/版本/图片） |
| `cbb_modules` | 355 | CBB 模块库（模块号/类型/供应商/规格） |
| `product_cbb_rel` | 369 | 产品-模块关联 |
| `sales_records` | 616 | 月度销量（6 个月，195 个货号） |
| `reviews` | 12 | 审核记录 |
| `users` | 4 | Web UI 用户账号 |
| `operation_logs` | 8 | 操作日志 |

> **注意**：`products` 表的品类列名为 `category1`/`category2`/`category3`（非 `category_l1`/`l2`/`l3`），`ProductQuery._detect_columns()` 已做自动映射。

---

## 🌐 Web UI

- **地址**: `http://localhost:8080`
- **认证**: Token 机制，24 小时过期
- **默认账号**:

| 用户名 | 密码 | 角色 | 权限 |
|--------|------|------|------|
| admin | admin123 | super_admin | 全部（含删除审核、导入货盘） |
| zhangsan | zhang123 | user | 查看 + 审核 |
| lisi | li123 | user | 查看 + 审核 |
| wangwu | wang123 | user | 查看 + 审核 |

- **功能**: 产品库浏览、CBB 模块查询、审核记录查看/删除、货盘导入
- **分页**: 每页 10 条

---

## 💬 飞书 Bot

- **模式**: WebSocket 长连接（无需公网 IP）
- **交互流程**: 发消息 → 选择任务类型卡片 → 上传 Excel → 异步审核 → 结果卡片
- **四任务类型**: 🔥爆品升级 / ⚔️竞品升级 / 📉未起量迭代 / 🗺️品类缺失
- **会话管理**: 内存存储，5 分钟超时

> **已知问题**: lark-oapi 1.5.3 的 WebSocket Client 会丢弃 CARD 类型消息，已通过 monkey patch 修复。

---

## ⚙️ LLM 配置

项目兼容所有 OpenAI 协议供应商，通过 `.env` 或环境变量切换：

```ini
# SiliconFlow（默认）
LLM_API_KEY=sk-xxxx
LLM_BASE_URL=https://api.siliconflow.cn/v1

# DeepSeek
LLM_API_KEY=sk-xxxx
LLM_BASE_URL=https://api.deepseek.com/v1

# Ollama（本地部署）
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:27b
```

优先级：环境变量 > `.env` 文件 > 代码默认值。

---

## 📊 评分体系

### 公共评分（权重各 20%）

| 维度 | 子项 | 数据来源 |
|------|------|---------|
| **人群分析** | 明确性 / 数据支撑 / 痛点分析 / 细分合理性 | Excel used_people 字段 |
| **场景分析** | 清晰度 / 覆盖完整性 / 需求分析 / 价值评估 | Excel used_scene 字段 |

### 专项评分（权重 60%）

每种分析器 4 个维度共 100 分，基于客观指标自动计算：

- **模块差距分** — 差距矩阵中高差距占比越低越好（感知度加权）
- **复用基础分** — 可复用模块 / (可复用 + 需新建) 的比例
- **路线图分** — P0 项占比评估（≤20% 最优，>60% 资源分散）
- **市场/风险分** — 基于销量验证、新建比例、已起量产品影响等

### 综合评分

```
综合分 = 人群分 × 20% + 场景分 × 20% + 专项分 × 60%
风险等级: ≥75 低 | ≥50 中 | <50 高
星级: ≥90 ★★★★★ | ≥75 ★★★★ | ≥60 ★★★ | ≥40 ★★ | <40 ★
```

---

## 📝 报告结构

生成的审核报告包含七大板块：

1. **立项信息** — 时间节点（立项/设计/打样/上架）
2. **市场与定价** — 市场规模、目标销量、定价、毛利率、竞品对比
3. **人群分析** — LLM 评分 + 明细 + 优劣势
4. **场景分析** — LLM 评分 + 明细 + 优劣势
5. **专项分析** — 爆品/竞品/迭代/缺失对应的分析报告，含产品明细（货号+品类+月度销量+CBB模块）
6. **同类产品及销售情况** — 品类下产品列表 + 销量趋势 + 模块明细 + AI 分析建议
7. **综合评估** — 加权评分 + 风险等级 + 星级 + 改进建议
