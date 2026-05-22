#!/usr/bin/env python3
"""
状态机引擎 — V4 重构
从 Executor dequeue 任务，根据当前状态 dispatch 到对应 PhaseHandler。
- 显式状态处理器注册表
- 统一异常捕获与流转
- 等待态自动退出（waiting_gate / waiting_clarification）
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from engine.exceptions import AgentFatalError, AgentRecoverableError
from utils.logging_config import get_logger
from engine.state_machine import State, Task, get_task, save_task, transition
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)

# 终态：处理到此停止
TERMINAL_STATES = {
    State.COMPLETED,
    State.FAILED,
    State.CANCELLED,
}

# 等待态：需要外部事件触发续跑
WAITING_STATES = {
    State.WAITING_GATE,
    State.WAITING_CLARIFICATION,
}


class AgentEngine:
    """
    V4 状态机引擎。

    使用方式：
        engine = AgentEngine()
        engine.register(State.CODING, CodingHandler())
        engine.register(State.BUILDING, BuildingHandler())
        ...
        engine.process_task(task)
    """

    def __init__(self, workspace_root: Path | None = None):
        self._handlers: dict[State, PhaseHandler] = {}
        self._workspace_root = workspace_root or Path(__file__).resolve().parents[2] / "workspace"

    # ------------------------------------------------------------------
    # 注册与管理
    # ------------------------------------------------------------------

    def register(self, state: State, handler: PhaseHandler) -> None:
        """注册状态处理器"""
        if state in self._handlers:
            logger.warning("Overriding handler for state %s", state.value)
        self._handlers[state] = handler
        logger.debug("Registered handler for %s", state.value)

    def unregister(self, state: State) -> None:
        """注销状态处理器"""
        self._handlers.pop(state, None)

    def get_handler(self, state: State) -> Optional[PhaseHandler]:
        """获取状态处理器"""
        return self._handlers.get(state)

    def list_registered(self) -> list[str]:
        """返回已注册的状态列表"""
        return [s.value for s in self._handlers]

    # ------------------------------------------------------------------
    # 核心处理流程
    # ------------------------------------------------------------------

    def process_task(self, task: Task) -> None:
        """
        处理单个任务直到进入终态或等待态。

        这是取代 V3 中 `run_coding_build_pr` while 循环的核心方法。
        """
        task_id = task.task_id
        workspace = self._workspace_root / task_id
        workspace.mkdir(parents=True, exist_ok=True)

        logger.info("=" * 60)
        logger.info("ENGINE START %s | %s | %s", task_id, task.level, task.raw_requirement[:60])
        logger.info("=" * 60)

        while True:
            current = State(task.current_state)
            handler = self._handlers.get(current)

            if not handler:
                logger.error("No handler registered for state %s", current.value)
                transition(task_id, State.FAILED, f"no handler for {current.value}", task)
                break

            if not handler.can_handle(task):
                logger.error("Handler for %s refused task %s", current.value, task_id)
                transition(task_id, State.FAILED, f"handler refused {current.value}", task)
                break

            # 执行阶段
            result = self._execute_phase(handler, task, workspace)

            if result is None:
                # 执行失败且无结果，已内部处理流转
                break

            # 处理结果
            if result.next_state in TERMINAL_STATES:
                transition(task_id, result.next_state, result.reason, task)
                logger.info("ENGINE END %s -> %s | %s", task_id, result.next_state.value, result.reason)
                break

            if result.next_state in WAITING_STATES:
                transition(task_id, result.next_state, result.reason, task)
                logger.info("ENGINE PAUSE %s -> %s | %s", task_id, result.next_state.value, result.reason)
                break

            # 正常流转
            ok = transition(task_id, result.next_state, result.reason, task)
            if not ok:
                logger.error("Illegal transition %s -> %s", current.value, result.next_state.value)
                transition(task_id, State.FAILED, f"illegal transition {current.value} -> {result.next_state.value}", task)
                break

    def _execute_phase(
        self,
        handler: PhaseHandler,
        task: Task,
        workspace: Path,
    ) -> Optional[PhaseResult]:
        """执行单个阶段，捕获异常并转换为状态流转。"""
        task_id = task.task_id
        current = State(task.current_state)

        try:
            handler.on_enter(task, workspace)
            result = handler.handle(task, workspace)
            handler.on_exit(task, workspace, result)
            return result

        except AgentRecoverableError as e:
            logger.warning("Recoverable error in %s: %s", current.value, e)
            task.error_log = str(e)
            save_task(task)
            return PhaseResult(State.CORRECTING, f"recoverable: {e}")

        except AgentFatalError as e:
            logger.error("Fatal error in %s: %s", current.value, e)
            task.error_log = str(e)
            save_task(task)
            return PhaseResult(State.FAILED, f"fatal: {e}")

        except Exception as e:
            logger.exception("Unexpected error in %s: %s", current.value, e)
            task.error_log = f"unexpected: {e}"
            save_task(task)
            return PhaseResult(State.FAILED, f"unexpected: {e}")

    # ------------------------------------------------------------------
    # 快捷方法：处理从等待态恢复的任务
    # ------------------------------------------------------------------

    def resume_from_gate(self, task: Task) -> None:
        """L2 /continue 核准后续跑"""
        task.resume_from_gate = 0
        save_task(task)
        transition(task.task_id, State.CODING, "resume after L2 gate", task)
        self.process_task(task)

    def resume_after_clarification(self, task: Task) -> None:
        """需求澄清后续跑"""
        task.resume_after_clarification = 0
        save_task(task)
        transition(task.task_id, State.PLANNING, "resume after clarification", task)
        self.process_task(task)
