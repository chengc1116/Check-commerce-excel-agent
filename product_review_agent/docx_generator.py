# -*- coding: utf-8 -*-
"""
审核报告 Word 文档生成器

共享模块，供 Web API 和飞书 Bot 共同使用。
生成格式化的 .docx 审核报告文档。
"""

from __future__ import annotations

import tempfile
import os
from datetime import datetime
from typing import Optional

from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn


# ============================================================
# 主入口
# ============================================================

def generate_review_docx(
    file_name: str = "report",
    task_label: str = "",
    overall_score: int = 0,
    risk_level: str = "未知",
    project_data: Optional[dict] = None,
    specific_score: Optional[dict] = None,
    common_scores: Optional[dict] = None,
    report_text: str = "",
    save_dir: Optional[str] = None,
) -> str:
    """
    生成审核报告 Word 文档。

    Args:
        file_name: 原始文件名（用于命名输出文件）
        task_label: 审核类型标签
        overall_score: 综合评分
        risk_level: 风险等级
        project_data: Excel 解析出的项目数据
        specific_score: 专项分析评分
        common_scores: 公共分析评分（人群+场景）
        report_text: 完整审核报告文本
        save_dir: 保存目录，默认为临时目录

    Returns:
        生成的 .docx 文件绝对路径
    """
    project_data = project_data or {}
    specific_score = specific_score or {}
    common_scores = common_scores or {}

    doc = Document()

    # ---- 全局样式 ----
    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    style.paragraph_format.space_after = Pt(4)
    style.paragraph_format.line_spacing = 1.3

    # ---- 标题 ----
    title = doc.add_heading("产品立项审核报告", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in title.runs:
        run.font.color.rgb = RGBColor(0x1A, 0x1A, 0x2E)

    # ---- 产品概览表 ----
    doc.add_heading("产品概览", level=1)

    overview_items = [
        ("产品名称", project_data.get("project_name", "(未填写)")),
        ("品牌", project_data.get("brand", "(未填写)")),
        ("品类", " > ".join(filter(None, [
            project_data.get("categoryl1", ""),
            project_data.get("categoryl2", ""),
            project_data.get("categoryl3", ""),
        ])) or "(未填写)"),
        ("负责人", project_data.get("applicant", "(未填写)")),
        ("审核类型", task_label),
        ("综合评分", f"{overall_score}/100"),
        ("风险等级", risk_level),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]

    _add_kv_table(doc, overview_items)

    # ---- 立项信息 ----
    doc.add_heading("立项信息", level=1)
    time_items = [
        ("立项时间", project_data.get("project_time", "(未填写)")),
        ("设计时间", project_data.get("design_time", "(未填写)")),
        ("打样时间", project_data.get("proofing_time", "(未填写)")),
        ("上架时间", project_data.get("launch_time", "(未填写)")),
    ]
    _add_kv_table(doc, time_items)

    # ---- 市场与定价 ----
    doc.add_heading("市场与定价", level=1)
    market_items = [
        ("市场规模", project_data.get("market_size", "(未填写)")),
        ("目标销量", project_data.get("estimated_sales", "(未填写)")),
        ("定价", project_data.get("pricing", "(未填写)")),
        ("毛利率", project_data.get("gfm", "(未填写)")),
        ("ERP成本", project_data.get("ERP_price", "(未填写)")),
    ]
    _add_kv_table(doc, market_items)

    # ---- 人群分析 ----
    doc.add_heading("人群分析", level=1)
    audience = common_scores.get("audience", {})
    if audience and audience.get("total_score"):
        _add_score_section(doc, audience, "人群")
    else:
        doc.add_paragraph("(暂无人群评分数据)")

    # ---- 场景分析 ----
    doc.add_heading("场景分析", level=1)
    scenario = common_scores.get("scenario", {})
    if scenario and scenario.get("total_score"):
        _add_score_section(doc, scenario, "场景")
    else:
        doc.add_paragraph("(暂无场景评分数据)")

    # ---- 专项分析 ----
    doc.add_heading(f"{task_label or '专项'}分析", level=1)
    if specific_score:
        _add_score_section(doc, specific_score, "专项")
    else:
        doc.add_paragraph("(暂无专项评分数据)")

    # ---- 完整报告文本 ----
    if report_text:
        doc.add_heading("完整审核报告", level=1)
        for line in report_text.split("\n"):
            line = line.strip()
            if not line:
                doc.add_paragraph("")
                continue
            if line.startswith("====") or line.startswith("----"):
                continue
            if line.startswith(("一、", "二、", "三、", "四、", "五、", "六、", "七、", "附、")):
                h = doc.add_heading(line, level=2)
                for run in h.runs:
                    run.font.color.rgb = RGBColor(0x1A, 0x1A, 0x2E)
            elif line.startswith("  "):
                p = doc.add_paragraph(line.strip())
                p.paragraph_format.left_indent = Cm(0.5)
            elif line.startswith("> "):
                p = doc.add_paragraph(line[2:])
                p.paragraph_format.left_indent = Cm(0.5)
                for run in p.runs:
                    run.font.color.rgb = RGBColor(0x00, 0x78, 0xD4)
            elif line.startswith("[") and "]" in line:
                p = doc.add_paragraph()
                run = p.add_run(line)
                run.bold = True
                run.font.size = Pt(10.5)
            else:
                doc.add_paragraph(line)

    # ---- 保存文件 ----
    if save_dir is None:
        save_dir = tempfile.mkdtemp()
    base_name = file_name.replace(".xlsx", "").replace(".xls", "")
    docx_path = os.path.join(save_dir, f"审核报告_{base_name}.docx")
    doc.save(docx_path)

    return docx_path


# ============================================================
# 内部辅助函数
# ============================================================

def _add_kv_table(doc, items: list[tuple[str, str]]):
    """添加键值对表格"""
    table = doc.add_table(rows=len(items), cols=2, style="Light Grid Accent 1")
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, (label, value) in enumerate(items):
        table.rows[i].cells[0].text = label
        table.rows[i].cells[1].text = str(value)
        for cell in table.rows[i].cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(10)
                    run.font.name = "微软雅黑"
                    run.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
        # 标签列加粗
        for run in table.rows[i].cells[0].paragraphs[0].runs:
            run.bold = True


def _add_score_section(doc, score_data: dict, section_name: str):
    """向文档添加一个评分板块（评分明细+优势+不足+建议）"""
    total = score_data.get("total_score", 0)
    p = doc.add_paragraph()
    run = p.add_run(f"综合评分: {total}/100")
    run.bold = True
    run.font.size = Pt(11)

    # 评分维度表
    dimensions = score_data.get("dimensions", {})
    if dimensions:
        # dimensions 可能是 dict 或 list，统一转为 list
        if isinstance(dimensions, dict):
            dim_items = [(name, info) for name, info in dimensions.items()
                         if isinstance(info, dict)]
        elif isinstance(dimensions, list):
            dim_items = [(d.get("name", ""), d) for d in dimensions
                         if isinstance(d, dict)]
        else:
            dim_items = []
        if dim_items:
            t = doc.add_table(rows=len(dim_items) + 1, cols=3, style="Light Grid Accent 1")
            # 表头
            for j, header in enumerate(["维度", "得分", "评价"]):
                t.rows[0].cells[j].text = header
                for run in t.rows[0].cells[j].paragraphs[0].runs:
                    run.bold = True
                    run.font.size = Pt(9)
            # 数据行
            for i, (name, info) in enumerate(dim_items):
                t.rows[i + 1].cells[0].text = name
                max_s = info.get("max_score", 25)
                t.rows[i + 1].cells[1].text = f"{info.get('score', 0)}/{max_s}"
                t.rows[i + 1].cells[2].text = info.get("reason", "")
                for cell in t.rows[i + 1].cells:
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.font.size = Pt(9)
                            run.font.name = "微软雅黑"
                            run.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    # 优势/不足/建议
    for label, key, color in [
        ("✅ 优势", "strengths", RGBColor(0x00, 0x80, 0x00)),
        ("⚠️ 不足", "weaknesses", RGBColor(0xCC, 0x33, 0x00)),
        ("💡 改进建议", "suggestions", RGBColor(0x00, 0x78, 0xD4)),
    ]:
        items = score_data.get(key, [])
        if items:
            p = doc.add_paragraph()
            run = p.add_run(label)
            run.bold = True
            run.font.color.rgb = color
            for item in items:
                bullet = doc.add_paragraph(item, style="List Bullet")
                for run in bullet.runs:
                    run.font.size = Pt(10)
