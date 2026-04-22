# -*- coding: utf-8 -*-
"""
用户会话状态管理

管理每个用户与机器人的交互状态，用于多步操作流程：
    1. 用户发文本 → 弹出任务选择卡片
    2. 用户点击选择 → 记录任务类型，提示上传文件
    3. 用户上传文件 → 根据任务类型执行对应审核
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 任务类型定义
# ============================================================

class TaskType(str, Enum):
    """四种审核任务类型"""
    HOT_UPGRADE = "hot_upgrade"          # 爆品升级
    COMPETITOR_UPGRADE = "competitor_upgrade"  # 竞品升级
    LOW_SALE_ITERATE = "low_sale_iterate"      # 未起量迭代
    CATEGORY_GAP = "category_gap"              # 品类地图缺失

    @property
    def label(self) -> str:
        """中文标签"""
        labels = {
            TaskType.HOT_UPGRADE: "爆品升级",
            TaskType.COMPETITOR_UPGRADE: "竞品升级",
            TaskType.LOW_SALE_ITERATE: "未起量迭代",
            TaskType.CATEGORY_GAP: "品类地图缺失",
        }
        return labels[self]

    @property
    def emoji(self) -> str:
        """对应emoji"""
        emojis = {
            TaskType.HOT_UPGRADE: "🔥",
            TaskType.COMPETITOR_UPGRADE: "⚔️",
            TaskType.LOW_SALE_ITERATE: "📉",
            TaskType.CATEGORY_GAP: "🗺️",
        }
        return emojis[self]

    @property
    def description(self) -> str:
        """任务描述"""
        descriptions = {
            TaskType.HOT_UPGRADE: "针对已有爆款产品进行升级迭代审核",
            TaskType.COMPETITOR_UPGRADE: "基于竞品分析的产品升级审核",
            TaskType.LOW_SALE_ITERATE: "针对未起量产品进行迭代改进审核",
            TaskType.CATEGORY_GAP: "填补品类地图空缺的新品审核",
        }
        return descriptions[self]


# 从卡片回调的 action.value 反查 TaskType
TASK_TYPE_MAP = {t.value: t for t in TaskType}


# ============================================================
# 会话状态
# ============================================================

class SessionState(str, Enum):
    """用户会话状态"""
    IDLE = "idle"                    # 空闲，等待交互
    WAITING_FILE = "waiting_file"    # 已选任务类型，等待上传文件


@dataclass
class UserSession:
    """单个用户的会话数据"""
    user_id: str
    chat_id: str
    state: SessionState = SessionState.IDLE
    task_type: Optional[TaskType] = None
    selected_at: float = 0.0        # 选择任务的时间戳
    message_id: str = ""            # 最近一条消息ID（用于回复）

    @property
    def is_waiting_file(self) -> bool:
        return self.state == SessionState.WAITING_FILE and self.task_type is not None

    @property
    def is_expired(self) -> bool:
        """会话是否过期（5分钟未操作）"""
        if self.selected_at == 0:
            return False
        return (time.time() - self.selected_at) > 300  # 5分钟


# ============================================================
# 会话管理器（内存存储，重启清空）
# ============================================================

class SessionManager:
    """管理所有用户的会话状态"""

    def __init__(self):
        self._sessions: dict[str, UserSession] = {}

    def get_or_create(self, user_id: str, chat_id: str = "") -> UserSession:
        """获取或创建用户会话"""
        if user_id in self._sessions:
            session = self._sessions[user_id]
            # 过期则重置
            if session.is_expired:
                logger.info(f"会话过期，重置: user_id={user_id}")
                session.state = SessionState.IDLE
                session.task_type = None
                session.selected_at = 0
            return session

        session = UserSession(user_id=user_id, chat_id=chat_id)
        self._sessions[user_id] = session
        return session

    def set_task(self, user_id: str, task_type: TaskType, chat_id: str = "", message_id: str = ""):
        """设置用户选择的任务类型"""
        session = self.get_or_create(user_id, chat_id)
        session.state = SessionState.WAITING_FILE
        session.task_type = task_type
        session.selected_at = time.time()
        session.chat_id = chat_id
        session.message_id = message_id
        logger.info(f"用户 {user_id} 选择了任务: {task_type.label}")

    def consume_task(self, user_id: str) -> Optional[TaskType]:
        """消费用户的任务选择（上传文件后调用，返回任务类型并重置状态）"""
        session = self._sessions.get(user_id)
        if not session or not session.is_waiting_file:
            return None

        task_type = session.task_type
        # 重置状态
        session.state = SessionState.IDLE
        session.task_type = None
        session.selected_at = 0
        return task_type

    def peek_task(self, user_id: str) -> Optional[TaskType]:
        """查看当前任务类型（不消费）"""
        session = self._sessions.get(user_id)
        if not session or not session.is_waiting_file:
            return None
        return session.task_type

    def reset(self, user_id: str):
        """重置用户会话"""
        if user_id in self._sessions:
            session = self._sessions[user_id]
            session.state = SessionState.IDLE
            session.task_type = None
            session.selected_at = 0

    def stats(self) -> dict:
        """统计信息"""
        waiting = sum(1 for s in self._sessions.values() if s.is_waiting_file)
        return {
            "total_sessions": len(self._sessions),
            "waiting_file": waiting,
        }


# 全局单例
session_manager = SessionManager()
