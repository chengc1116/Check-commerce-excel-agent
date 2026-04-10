# -*- coding: utf-8 -*-
"""
飞书文件处理 - 下载/上传文件

通过飞书API下载用户上传的Excel文件，
上传审核报告（如有需要）。
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import lark_oapi as lark

logger = logging.getLogger(__name__)


def download_file(
    client: lark.Client,
    file_key: str,
    file_type: str = "file",
    save_dir: Optional[str] = None,
) -> Optional[str]:
    """
    通过飞书API下载文件到本地。

    Args:
        client: lark_oapi.Client 实例
        file_key: 飞书文件key（从消息事件中获取）
        file_type: 文件类型 "file" / "image" / "ppt" 等
        save_dir: 保存目录，默认为系统临时目录

    Returns:
        下载后的本地文件路径，失败返回None
    """
    if save_dir is None:
        save_dir = tempfile.mkdtemp(prefix="feishu_review_")

    save_path = os.path.join(save_dir, f"{file_key}.xlsx")

    try:
        # 使用飞书API下载文件
        # im/v1/messages/{message_id}/resources/{file_key}
        # 这里用更通用的 drive/v1/files/{file_key}/download
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = GetMessageResourceRequest.builder() \
            .file_key(file_key) \
            .type(file_type) \
            .build()

        response = client.im.v1.message_resource.get(request)

        if not response.success():
            logger.error(f"下载文件失败: code={response.code}, msg={response.msg}")
            return None

        # 写入文件
        with open(save_path, "wb") as f:
            f.write(response.file.read())

        logger.info(f"文件下载成功: {save_path}")
        return save_path

    except Exception as e:
        logger.error(f"下载文件异常: {e}")
        return None


def download_message_file(
    client: lark.Client,
    message_id: str,
    file_key: str,
    save_dir: Optional[str] = None,
) -> Optional[str]:
    """
    下载消息中的文件附件。

    Args:
        client: lark_oapi.Client 实例
        message_id: 消息ID
        file_key: 文件key
        save_dir: 保存目录

    Returns:
        下载后的本地文件路径，失败返回None
    """
    if save_dir is None:
        save_dir = tempfile.mkdtemp(prefix="feishu_review_")

    save_path = os.path.join(save_dir, f"{file_key}.xlsx")

    try:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("file") \
            .build()

        response = client.im.v1.message_resource.get(request)

        if not response.success():
            logger.error(f"下载消息文件失败: code={response.code}, msg={response.msg}")
            return None

        with open(save_path, "wb") as f:
            f.write(response.file.read())

        logger.info(f"消息文件下载成功: {save_path}")
        return save_path

    except Exception as e:
        logger.error(f"下载消息文件异常: {e}")
        return None


def is_excel_file(filename: str) -> bool:
    """判断文件名是否为Excel文件"""
    ext = Path(filename).suffix.lower()
    return ext in (".xlsx", ".xls")
