# -*- coding: utf-8 -*-
"""
Excel解析Agent — 项目书 → project_reviews 结构化JSON

核心理念：
  - Python只负责"搬运"：把Excel原始内容原样读出来
  - 全部交给LLM用语义理解去做字段映射
  - 一次性将4个sheet全部传入LLM
  - 输出纯JSON

用法:
    python scripts/excel_parsing_agent.py <xlsx文件路径>
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ============================================================
# 环境设置
# ============================================================
sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ExcelParsingAgent")


# ============================================================
# Excel原始内容提取（纯搬运，零理解）
# ============================================================

def read_excel_raw(file_path: str) -> list[dict]:
    """
    将Excel文件的所有sheet转为纯文本。
    返回 [{"sheet_name": str, "raw_text": str, "rows": int, "cols": int}, ...]
    不做任何字段理解，只忠实按行列输出，交给LLM处理。
    """
    from openpyxl import load_workbook
    
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    wb = load_workbook(file_path, data_only=True)
    sheets_info = []

    for sheet_name in wb.sheetnames:
        if sheet_name.startswith("~$"):
            continue

        ws = wb[sheet_name]
        lines = []
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=False):
            row_values = []
            for cell in row:
                val = cell.value
                if val is not None:
                    row_values.append(str(val).strip())
                else:
                    row_values.append("")
            line = " | ".join(row_values)
            if line.strip(" |"):
                lines.append(line)
            else:
                lines.append("")

        raw_text = "\n".join(lines)

        sheets_info.append({
            "sheet_name": sheet_name,
            "raw_text": raw_text,
            "rows": ws.max_row,
            "cols": ws.max_column,
        })

        logger.info(f"  [{sheet_name}] {ws.max_row}行 x {ws.max_column}列, 文字约{len(raw_text)}字符")

    wb.close()
    return sheets_info


# ============================================================
# Prompt定义
# ============================================================

SYSTEM_PROMPT = """你是一个电商产品立项数据解析专家。你的任务是：将一份项目立项Excel表格的内容，转换成结构化JSON数据，用于存入「project_reviews」数据库表。

== 核心规则 ==
1. 你看到的Excel是原始表格内容，格式可能因人而异（列位置不同、写法不同、合并方式不同），你需要用语义理解来识别信息，不要依赖固定的列位置或列名。
2. 如果某个信息在多个地方出现，以最详细的那个为准。
3. 找不到的信息设为null，绝对不要编造数据。
4. 数字型字段尽量提取为数值（去掉单位符号）；日期保持原格式即可。
5. 返回纯JSON，不要markdown代码块。"""

USER_PROMPT_TEMPLATE = """以下是Excel项目书的前4个sheet的完整内容：

{sheets_content}

---

请从以上Excel内容中提取以下字段，返回JSON。

== 字段提取说明（按大致位置提示，但请用语义理解识别，不要硬匹配列名）==
有可能给出的表格只有一个sheet，那么就只在sheet1中去匹配以下的信息。
【A. 基础信息】— 主要来自Sheet1（模板/品类缺失表）
- project_name: 产品名称
- brand: 品牌
- project_time: 立项时间
- design_time: 设计时间
- proofing_time: 打样时间
- launch_time: 上架时间
- categoryl1: 一级品类
- categoryl2: 二级品类
- categoryl3: 三级品类
- applicant: 负责人/申请人
- market_size: 市场规模描述
- estimated_sales: 目标销量额（提取数字，去掉单位）
【B. 市场与定价】— 主要来自Sheet2（立项详情）中 "价格/毛利" 的相关区域也有可能来自sheet1中
- pricing: 定价（不变更形式）
- gfm: 毛利率（如72表示72%）
- ERP_price: ERP成本价（数字）
- core_config: 核心配置内容，不变更形式
【C. 设计要求】design_require 对象 — 来自Sheet1中"设计要求"相关区域:
  design_require 对象内容包括：
  - content: 设计目的概述
  - outlook: 改外观/品牌的具体描述
  - material: 改材料的具体描述
  - function: 改功能的具体描述
【D. 对比产品信息】product_comparison 对象 — 来自Sheet2中"对手分析"区域:
  product_comparison
  - comparison_name: 对手商品名称
  - image_url: 竞品图片
  - selling_point: 对手的卖点（我方要复制的）
  - improving_point: 我方要超越/改进的点
【E. 使用人群】used_people 数组 — 来自Sheet3中"人群场景解析"（这部分内容会包含人群）:
  针对表格内容自定义相关字段，根据表头去补充该部分字段
【F. 使用场景】used_scene 数组 — 来自Sheet3中"场景详细解析":
  针对表格内容自定义相关字段，根据表头去补充该部分字段
【G. 模块清单】module_list 数组 — 来自Sheet4（KANO+材质拆解）中的材质模块拆解:
  根据这一部分的表头自定义相关参数

== 注意事项 ==
- 找不到的字段设为null，数组字段找不到则设为空数组[]
- 返回纯JSON，不要加```json```包裹"""


# ============================================================
# LLM 调用（含429重试）
# ============================================================

async def call_llm_with_retry(
    llm,
    system_prompt: str,
    user_prompt: str,
    max_retries: int = 3,
    base_delay: float = 10.0,
    use_fast_model: bool = False,
) -> dict:
    """调用LLM，自动处理429 Rate Limit重试"""
    call_fn = llm.acall_text_fast if use_fast_model else llm.acall_text
    for attempt in range(max_retries + 1):
        try:
            print(f"  [LLM调用] 第{attempt + 1}次尝试...")
            result = await call_fn(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format="json",
            )

            if isinstance(result, dict) and not result.get("_parse_error"):
                return result
            else:
                logger.warning(f"[ExcelParsingAgent] LLM返回解析异常: {type(result)}")
                return {"_error": f"parse_error: {str(result)[:200]}"}

        except Exception as e:
            error_name = type(e).__name__
            error_msg = str(e)

            if "429" in error_msg or "rate" in error_msg.lower() or "RateLimit" in error_name:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    print(f"  [429限流] 等待{delay:.0f}秒后重试... ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay)
                    continue
                else:
                    return {"_error": f"rate_limit_exceeded: {error_msg}"}

            elif "timeout" in error_msg.lower() or "Timeout" in error_name:
                if attempt < max_retries:
                    delay = base_delay
                    print(f"  [超时] 等待{delay:.0f}秒后重试... ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay)
                    continue
                else:
                    return {"_error": f"timeout_exceeded: {error_msg}"}

            else:
                print(f"  [异常] {error_name}: {error_msg[:200]}")
                return {"_error": f"{error_name}: {error_msg}"}

    return {"_error": "max_retries_exceeded"}


# ============================================================
# 主解析流程
# ============================================================

async def parse_excel_to_project_review(file_path: str) -> dict:
    """Excel → 结构化JSON（一次性4个sheet传入LLM）"""
    start_time = time.time()
    file_path = str(file_path)
    filename = Path(file_path).name

    # Step 1: 读取Excel原始内容
    logger.info(f"[ExcelParsingAgent] 开始解析: {filename}")
    sheets_data = read_excel_raw(file_path)
    logger.info(f"[ExcelParsingAgent] 共读取 {len(sheets_data)} 个sheet")

    # 只使用前4个sheet
    useful_sheets = sheets_data[:min(4, len(sheets_data))]
    logger.info(f"[ExcelParsingAgent] 使用前 {len(useful_sheets)} 个sheet")

    # Step 2: 组装4个sheet的内容
    sheets_parts = []
    for i, s in enumerate(useful_sheets):
        sheets_parts.append(
            f"== Sheet {i + 1}: {s['sheet_name']} ==\n\n{s['raw_text']}"
        )
    sheets_content = "\n\n\n".join(sheets_parts)

    total_chars = len(sheets_content)
    logger.info(f"[ExcelParsingAgent] 传入LLM内容总长: {total_chars}字符")
    print(f"\n  📊 传入LLM: {len(useful_sheets)}个sheet, 共{total_chars}字符")

    # Step 3: 调用LLM
    from product_review_agent.agents.llm_client import get_llm_client
    llm = get_llm_client()

    if not llm.is_available:
        return {
            "_status": "llm_unavailable",
            "_error": "LLM未配置，请在.env中设置LLM_API_KEY",
            "filename": filename,
        }

    # Excel解析是简单的字段映射任务，使用快速模型即可
    original_max_tokens = llm.max_tokens
    llm.max_tokens = 8192

    try:
        print(f"\n  🚀 开始调用LLM（快速模型: {llm.fast_model}）...")
        logger.info(f"[ExcelParsingAgent] 调用快速模型: {llm.fast_model}")

        result = await call_llm_with_retry(
            llm,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT_TEMPLATE.format(sheets_content=sheets_content),
            use_fast_model=True,
        )
    finally:
        llm.max_tokens = original_max_tokens

    # Step 4: 补充元数据
    elapsed = round(time.time() - start_time, 1)
    has_error = result.get("_error")

    extracted = result if isinstance(result, dict) else {}
    extracted.update({
        "_status": "success" if not has_error else "failed",
        "filename": filename,
        "sheets_used": [s["sheet_name"] for s in useful_sheets],
        "_elapsed_seconds": elapsed,
        "_parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

    if has_error:
        extracted["_error"] = has_error

    return extracted


# ============================================================
# CLI 入口
# ============================================================

async def main():
    args = sys.argv[1:]

    if not args or "--help" in args or "-h" in args:
        print("""
用法: python scripts/excel_parsing_agent.py <xlsx文件路径>

示例:
  python scripts/excel_parsing_agent.py data/excel/xxx.xlsx

说明:
  一次性将前4个sheet传入LLM，用语义理解自动映射到数据库字段。
  输出纯JSON文件到 output/ 目录。
""")
        return

    file_path = args[0]

    print("\n" + "=" * 55)
    print("   Excel解析Agent — 项目书→结构化JSON")
    print("=" * 55 + "\n")

    result = await parse_excel_to_project_review(file_path)

    # 保存JSON
    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = Path(file_path).stem

    json_path = output_dir / f"{stem}_parsed_{timestamp}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 打印结果
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n💾 JSON已保存: {json_path}")


if __name__ == "__main__":
    asyncio.run(main())
