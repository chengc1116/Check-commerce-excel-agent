# Product-Review-Agent（产品立项审核助手）

自动解析电商产品立项项目书（Word/Excel），对「品类市场体量」「使用场景」「使用人群」三大维度进行准确性验证与量化评分，输出专业审核报告。

## 技术栈

- **语言**: Python 3.10+
- **Excel解析**: openpyxl + pandas
- **Word解析**: python-docx
- **HTTP服务**: FastAPI（飞书Webhook集成）
- **飞书集成**: lark-oapi

## 项目结构

```
product_review_agent/
├── __init__.py
├── main.py                    # FastAPI 入口 + 飞书Webhook
├── config.py                  # 全局配置
├── parsers/                   # 模块A: 文档解析引擎
│   ├── __init__.py
│   ├── base.py               # 解析器基类
│   ├── excel_parser.py       # Excel解析器
│   ├── word_parser.py        # Word解析器
│   └── table_classifier.py   # 表格智能识别
├── connectors/               # 模块B: 外部数据接口
│   ├── __init__.py
│   ├── market_data.py        # 品类市场体量验证接口
│   └── knowledge_base.py     # 知识库预留接口
├── evaluators/               # 模块C/D: 评估引擎
│   ├── __ init__.py
│   ├── scenario_evaluator.py # 使用场景评估
│   ├── persona_evaluator.py  # 使用人群评估
│   └── report_generator.py   # 审核报告生成
├── models/                   # 数据模型
│   ├── __init__.py
│   ├── document.py           # 文档解析结果模型
│   └── evaluation.py         # 评估结果模型
├── feishu/                   # 飞书集成
│   ├── __init__.py
│   ├── bot.py                # 飞书机器人
│   └── card_builder.py       # 消息卡片构建
└── utils/                    # 工具函数
    ├── __init__.py
    ├── text_parser.py        # 文本解析工具（百分比、时长等）
    └── validators.py         # 通用校验工具
```

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python -m product_review_agent.main

# 或直接调用解析器
python scripts/parse_document.py sample.xlsx
```
