# -*- coding: utf-8 -*-
"""
产品立项审核 - 命令行入口（复用 reviewer.py 核心逻辑）

使用方法:
    python scripts/review_from_template.py                           # 默认解析米度狗眼罩
    python scripts/review_from_template.py 米度狗宽版重力眼罩_研发输入表.xlsx
    python scripts/review_from_template.py seruna新护腰立项.xlsx
"""

import asyncio
import io
import json
import sys
from datetime import datetime
from pathlib import Path

# -- 路径 & 编码 --
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 .env 环境变量（在所有 import 之前）
from product_review_agent.load_env import load_env
load_env(PROJECT_ROOT / ".env")

from product_review_agent.reviewer import review_excel


SEPARATOR = "=" * 78


async def async_main():
    # 确定输入文件
    if len(sys.argv) > 1:
        file_path = PROJECT_ROOT / sys.argv[1]
    else:
        file_path = PROJECT_ROOT / "米度狗宽版重力眼罩_研发输入表.xlsx"

    if not file_path.exists():
        print(f"[错误] 文件不存在: {file_path}")
        print("可用文件:")
        for f in PROJECT_ROOT.glob("*.xlsx"):
            if not f.name.startswith("~$"):
                print(f"  - {f.name}")
        return

    print(SEPARATOR)
    print("  产品立项审核 - 异步并行评分")
    print(SEPARATOR)
    print(f"  输入文件: {file_path.name}")
    print()

    # 执行审核
    result = await review_excel(file_path)

    if result.error:
        print(f"\n[错误] {result.error}")
        return

    # 输出报告
    print(result.report)

    # 保存报告
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_filename = f"审核报告_{file_path.stem}_{timestamp}.txt"
    report_path = PROJECT_ROOT / report_filename
    report_path.write_text(result.report, encoding="utf-8")
    print(f"\n  报告已保存: {report_filename}")

    # 保存解析JSON
    json_filename = f"解析结果_{file_path.stem}.json"
    json_path = PROJECT_ROOT / json_filename
    output = {
        "file": result.parse_result.file_name,
        "parsed_at": datetime.now().isoformat(),
        "data": result.parse_result.data,
        "scores": result.scores,
        "warnings": result.parse_result.warnings,
        "overall_score": result.overall_score,
        "risk_level": result.risk_level,
    }
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  JSON已保存: {json_filename}")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
