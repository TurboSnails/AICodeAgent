#!/usr/bin/env python3
"""
任务取消协作：DB 终态 + 中断 Executor 正在跑的子进程。
"""

from __future__ import annotations

import subprocess
from typing import Optional

from engine.exceptions import TaskCancelledError
from engine.state_machine import State, get_task
from utils.logging_config import get_logger
from utils.paths import DATA_DIR

logger = get_logger(__name__)

CURRENT_TASK_FILE = DATA_DIR / "executor.current_task"

_active_task_id: str = ""


def set_active_task(task_id: str) -> None:
    global _active_task_id
    _active_task_id = (task_id or "").strip()


def clear_active_task() -> None:
    global _active_task_id
    _active_task_id = ""


def get_active_task_id() -> str:
    return _active_task_id


def read_executor_current_task() -> str:
    try:
        return CURRENT_TASK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_task_cancelled(task_id: str) -> bool:
    if not task_id:
        return False
    task = get_task(task_id)
    return task is not None and task.current_state == State.CANCELLED.value


def raise_if_cancelled(task_id: Optional[str] = None) -> None:
    tid = (task_id or _active_task_id or read_executor_current_task()).strip()
    if tid and is_task_cancelled(tid):
        raise TaskCancelledError(f"task {tid} cancelled")


def interrupt_running_work(task_id: str) -> bool:
    """
    若 Executor 正在处理该任务，终止 claude / gradle 子进程以便尽快释放锁。
    返回是否执行了中断尝试。
    """
    task_id = (task_id or "").strip()
    if not task_id:
        return False

    current = read_executor_current_task()
    active = _active_task_id
    if task_id != current and task_id != active:
        logger.debug(
            "interrupt_running_work: %s not current (file=%s active=%s)",
            task_id,
            current,
            active,
        )
        return False

    logger.info("interrupt_running_work: stopping subprocesses for %s", task_id)
    patterns = [
        "claude -p",
        "claude --print",
        "gradlew",
    ]
    for pattern in patterns:
        try:
            subprocess.run(
                ["pkill", "-f", pattern],
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            logger.debug("pkill %r: %s", pattern, e)
    return True
