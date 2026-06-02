# -*- coding: utf-8 -*-
"""
飞书多维表格写入模块

将每次审核结果实时写入飞书多维表格(Bitable)，
支持后续月度汇总、图表展示。

表格字段设计（需在飞书多维表格中手动创建）:
    - 产品名称      文本
    - 一级品类      单选（如：护具、枕头、坐垫 等）
    - 审核类型      单选（爆品升级/竞品升级/未起量迭代/品类地图缺失）
    - 综合评分      数字
    - 风险等级      单选（低/中/高）
    - 提交人        文本
    - 文件名        文本
    - 审核时间      日期
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

import lark_oapi as lark

logger = logging.getLogger(__name__)


def write_review_record(
    client: lark.Client,
    product_name: str,
    category_l1: str,
    task_label: str,
    overall_score: int,
    risk_level: str,
    submitter: str,
    file_name: str,
    app_token: str = "",
    table_id: str = "",
) -> bool:
    """
    将一条审核结果写入飞书多维表格。

    Args:
        client: 飞书客户端
        product_name: 产品名称
        category_l1: 一级品类
        task_label: 审核类型标签（如 "🔥 爆品升级"）
        overall_score: 综合评分
        risk_level: 风险等级（低/中/高）
        submitter: 提交人姓名
        file_name: 原始文件名
        app_token: 多维表格app_token，为空则用环境变量
        table_id: 数据表ID，为空则用环境变量

    Returns:
        是否写入成功
    """
    app_token = app_token or os.getenv("BITABLE_APP_TOKEN", "")
    table_id = table_id or os.getenv("BITABLE_TABLE_ID", "")

    if not app_token or not table_id:
        logger.warning("[Bitable] app_token 或 table_id 未配置，跳过写入")
        return False

    logger.info(f"[Bitable] 准备写入: app_token={app_token}, table_id={table_id}")
    logger.info(f"[Bitable] 数据: 产品={product_name}, 品类={category_l1}, "
                f"类型={task_label}, 评分={overall_score}, 风险={risk_level}, "
                f"提交人={submitter}, 文件={file_name}")

    try:
        from lark_oapi.api.bitable.v1 import (
            CreateAppTableRecordRequest,
            AppTableRecord,
        )

        # 构建记录字段
        fields = {
            "产品名称": product_name or "未知",
            "一级品类": category_l1 or "未分类",
            "审核类型": task_label,
            "综合评分": overall_score,
            "风险等级": risk_level,
            "提交人": submitter,
            "文件名": file_name,
            "审核日期": int(datetime.now().timestamp() * 1000),  # 毫秒时间戳
        }

        logger.info(f"[Bitable] fields内容: {json.dumps(fields, ensure_ascii=False)}")

        record = AppTableRecord.builder() \
            .fields(fields) \
            .build()

        request = CreateAppTableRecordRequest.builder() \
            .app_token(app_token) \
            .table_id(table_id) \
            .request_body(record) \
            .build()

        response = client.bitable.v1.app_table_record.create(request)

        if response.success():
            record_id = response.data.record.record_id if response.data and response.data.record else "?"
            logger.info(f"[Bitable] 写入成功: record_id={record_id}")
            return True
        else:
            logger.error(f"[Bitable] 写入失败: code={response.code}, msg={response.msg}")
            logger.error(f"[Bitable] 请求参数: app_token={app_token}, table_id={table_id}")
            if response.data:
                logger.error(f"[Bitable] response.data: {response.data}")
            return False

    except Exception as e:
        logger.error(f"[Bitable] 写入异常: {e}", exc_info=True)
        return False


def build_monthly_summary_text(records: list[dict]) -> str:
    """
    将查询到的审核记录按一级品类汇总，生成月度报告文本。

    Args:
        records: 审核记录列表，每条包含 fields 字段

    Returns:
        格式化的汇总文本
    """
    if not records:
        return "本月暂无审核记录。"

    # 按品类分组
    by_category: dict[str, list[dict]] = {}
    for r in records:
        fields = r.get("fields", r)
        cat = fields.get("一级品类", "未分类")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(fields)

    total = len(records)
    lines = [
        f"📋 本月立项审核月报",
        f"━━━━━━━━━━━━━━",
        f"审核总数: {total}",
        "",
    ]

    for cat, items in sorted(by_category.items()):
        scores = [it.get("综合评分", 0) for it in items if it.get("综合评分")]
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0
        risk_counts = {"低": 0, "中": 0, "高": 0}
        for it in items:
            risk = it.get("风险等级", "")
            if risk in risk_counts:
                risk_counts[risk] += 1

        lines.append(f"【{cat}】 共{len(items)}个，均分{avg_score}")
        lines.append(f"  风险分布: 低{risk_counts['低']} / 中{risk_counts['中']} / 高{risk_counts['高']}")

    # 审核类型分布
    by_type: dict[str, int] = {}
    for r in records:
        fields = r.get("fields", r)
        t = fields.get("审核类型", "未知")
        by_type[t] = by_type.get(t, 0) + 1

    lines.append("")
    lines.append("审核类型分布:")
    for t, cnt in sorted(by_type.items(), key=lambda x: -x[1]):
        lines.append(f"  {t}: {cnt}")

    return "\n".join(lines)
