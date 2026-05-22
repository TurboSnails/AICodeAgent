#!/usr/bin/env python3
"""
统一任务服务层 — V4 重构
所有网关（Web/Telegram/未来 Slack）调用的唯一入口。
- 任务 CRUD
- 核准/取消/续跑
- 状态查询
- 与 NotificationService 解耦（事件驱动，可选）
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from utils.config_loader import cfg_bool
from utils.logging_config import get_logger
from engine.state_machine import (
    Task,
    approve_clarification_reply,
    approve_gate_resume,
    cancel_task,
    get_task,
    get_waiting_clarifications,
    get_waiting_gates,
    save_task,
    transition,
    State,
)

logger = get_logger(__name__)


class TaskService:
    """
    任务生命周期管理。

    职责：
    1. 提交任务（自动入队到 pending）
    2. L2 核准（/continue）
    3. 需求澄清回复（/reply）
    4. 取消任务
    5. 查询任务列表/状态
    """

    def __init__(self):
        pass

    # ------------------------------------------------------------------
    # 任务提交
    # ------------------------------------------------------------------

    def submit_task(
        self,
        requirement: str,
        level: str = "auto",
        site_hint: str = "",
        source: str = "web",
        chat_id: str = "",
    ) -> Task:
        """提交新任务，自动判定等级（auto → L0/L1/L2）"""
        task_id = str(uuid.uuid4())[:8]
        if level == "auto":
            level = self._auto_level(requirement)

        task = Task(
            task_id=task_id,
            raw_requirement=requirement.strip(),
            level=level,
            site_hint=site_hint,
            source=source,
            chat_id=chat_id,
        )
        save_task(task)
        logger.info("Submitted task %s [%s]: %s", task_id, level, requirement[:80])
        return task

    # ------------------------------------------------------------------
    # 核准与续跑
    # ------------------------------------------------------------------

    def approve_gate(self, task_id: str) -> bool:
        """L2 任务核准（/continue），原子地回到 pending 队列"""
        task = get_task(task_id)
        if not task:
            logger.warning("approve_gate: task %s not found", task_id)
            return False
        if task.current_state != State.WAITING_GATE.value:
            logger.warning("approve_gate: task %s not in waiting_gate (%s)", task_id, task.current_state)
            return False
        return approve_gate_resume(task_id)

    def submit_clarification(self, task_id: str, reply: str) -> bool:
        """用户澄清回复（/reply），合并到需求后回到 pending"""
        reply = (reply or "").strip()
        if not reply:
            return False
        task = get_task(task_id)
        if not task:
            logger.warning("submit_clarification: task %s not found", task_id)
            return False
        if task.current_state != State.WAITING_CLARIFICATION.value:
            logger.warning("submit_clarification: task %s not in waiting_clarification", task_id)
            return False
        return approve_clarification_reply(task_id, reply)

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel(self, task_id: str, reason: str = "user cancelled") -> bool:
        """取消任务"""
        return cancel_task(task_id, reason)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> Optional[Task]:
        return get_task(task_id)

    def list_tasks(self, state_filter: Optional[str] = None, limit: int = 50) -> list[Task]:
        """查询任务列表"""
        import sqlite3
        from engine.state_machine import DB_FILE

        conn = sqlite3.connect(str(DB_FILE))
        conn.row_factory = sqlite3.Row
        if state_filter:
            cursor = conn.execute(
                "SELECT * FROM task_queue WHERE current_state = ? ORDER BY created_at DESC LIMIT ?",
                (state_filter, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM task_queue ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = cursor.fetchall()
        conn.close()
        return [self._row_to_task(r) for r in rows]

    def get_waiting_gates(self) -> list[Task]:
        return get_waiting_gates()

    def get_waiting_clarifications(self) -> list[Task]:
        return get_waiting_clarifications()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _auto_level(requirement: str) -> str:
        """根据需求文本自动判定任务等级"""
        req = requirement.lower()
        l0_keywords = ["文案", "颜色", "间距", "字体大小", "string", "bug 修复", "修复崩溃", "typo"]
        if any(k in req for k in l0_keywords):
            return "L0"
        l2_keywords = ["重构", "架构", "状态机", "跨模块", "核心", "网络层", "数据库迁移", "全局"]
        if any(k in req for k in l2_keywords):
            return "L2"
        return "L1"

    @staticmethod
    def _row_to_task(row) -> Task:
        return Task(
            task_id=row["task_id"],
            raw_requirement=row["raw_requirement"],
            level=row["level"],
            site_hint=row["site_hint"] or "",
            source=row["source"],
            chat_id=row["chat_id"] or "",
            status=row["status"] or "pending",
            current_state=row["current_state"] or "pending",
            attempt_count=row["attempt_count"] or 0,
            max_retries=row["max_retries"] or 3,
            pr_url=row["pr_url"] or "",
            branch=row["branch"] or "",
            base_branch=row["base_branch"] or "",
            error_log=row["error_log"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            gate_deadline=row["gate_deadline"] or "",
            resume_from_gate=row["resume_from_gate"] or 0,
            clarification_deadline=row["clarification_deadline"] if "clarification_deadline" in row.keys() else "",
            resume_after_clarification=row["resume_after_clarification"] if "resume_after_clarification" in row.keys() else 0,
        )
