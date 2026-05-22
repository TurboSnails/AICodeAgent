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

from engine.exceptions import AgentFatalError, AgentRecoverableError, TaskCancelledError
from utils.logging_config import get_logger
from utils.paths import WORKSPACE_ROOT
from utils.phase_status import write_phase_status
from utils.tracing import trace_task
from utils.task_cancel import is_task_cancelled, raise_if_cancelled
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
        self._workspace_root = workspace_root or WORKSPACE_ROOT
        self._current_tracer = None

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

        with trace_task(
            task_id=task_id,
            requirement=task.raw_requirement,
            level=task.level,
            site_hint=task.site_hint,
        ) as tracer:
            self._current_tracer = tracer
            try:
                while True:
                    if self._stop_if_cancelled(task_id, task, workspace):
                        break

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
                    try:
                        result = self._execute_phase(handler, task, workspace)
                    except TaskCancelledError:
                        write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
                        logger.info("ENGINE STOP %s | user cancelled", task_id)
                        break

                    if self._stop_if_cancelled(task_id, task, workspace):
                        break

                    if result is None:
                        # 执行失败且无结果，已内部处理流转
                        break

                    # 处理结果
                    if result.next_state in TERMINAL_STATES:
                        transition(task_id, result.next_state, result.reason, task)
                        if result.next_state in (State.FAILED, State.CANCELLED):
                            write_phase_status(
                                workspace,
                                result.next_state.value,
                                _phase_label(result.next_state.value)
                                + (f"：{result.reason}" if result.reason else ""),
                            )
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
            finally:
                self._current_tracer = None

    def _stop_if_cancelled(self, task_id: str, task: Task, workspace: Path) -> bool:
        fresh = get_task(task_id)
        if not fresh:
            return True
        if fresh.current_state != State.CANCELLED.value:
            return False
        task.current_state = fresh.current_state
        task.status = fresh.status
        write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
        logger.info("ENGINE STOP %s | cancelled in DB", task_id)
        return True

    def _execute_phase(
        self,
        handler: PhaseHandler,
        task: Task,
        workspace: Path,
    ) -> Optional[PhaseResult]:
        """执行单个阶段，捕获异常并转换为状态流转。"""
        task_id = task.task_id
        current = State(task.current_state)
        tracer = getattr(self, "_current_tracer", None)

        try:
            raise_if_cancelled(task_id)
            write_phase_status(workspace, current.value, f"正在执行：{_phase_label(current.value)}")
            if tracer:
                tracer.log_phase(current.value, inputs={"task_id": task_id, "state": current.value})
            handler.on_enter(task, workspace)
            result = handler.handle(task, workspace)
            raise_if_cancelled(task_id)
            handler.on_exit(task, workspace, result)
            write_phase_status(
                workspace,
                result.next_state.value,
                result.reason or _phase_label(result.next_state.value),
            )
            if tracer:
                tracer.log_phase(
                    current.value,
                    outputs={"next_state": result.next_state.value, "reason": result.reason},
                )
            return result

        except TaskCancelledError:
            write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
            raise

        except AgentRecoverableError as e:
            if is_task_cancelled(task_id):
                write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
                raise TaskCancelledError(f"task {task_id} cancelled during {current.value}") from e
            logger.warning("Recoverable error in %s: %s", current.value, e)
            task.error_log = str(e)
            save_task(task)
            write_phase_status(workspace, current.value, f"可恢复错误：{e}", extra={"error": str(e)})
            if tracer:
                tracer.log_phase(current.value, error=str(e))
            return PhaseResult(State.CORRECTING, f"recoverable: {e}")

        except AgentFatalError as e:
            if is_task_cancelled(task_id):
                write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
                raise TaskCancelledError(f"task {task_id} cancelled during {current.value}") from e
            logger.error("Fatal error in %s: %s", current.value, e)
            task.error_log = str(e)
            save_task(task)
            if tracer:
                tracer.log_phase(current.value, error=str(e))
            return PhaseResult(State.FAILED, f"fatal: {e}")

        except Exception as e:
            if is_task_cancelled(task_id):
                write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
                raise TaskCancelledError(f"task {task_id} cancelled during {current.value}") from e
            logger.exception("Unexpected error in %s: %s", current.value, e)
            task.error_log = f"unexpected: {e}"
            save_task(task)
            if tracer:
                tracer.log_phase(current.value, error=str(e))
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
        """需求澄清后续跑（保留 resume 标志至 planning 结束，避免重复分类/澄清）"""
        transition(task.task_id, State.PLANNING, "resume after clarification", task)
        self.process_task(task)


def _phase_label(state: str) -> str:
    labels = {
        "pending": "排队等待",
        "planning": "规划与分类",
        "waiting_clarification": "等待用户澄清",
        "debating": "多 Agent 辩论",
        "consensus": "生成共识",
        "architect_planning": "架构规划",
        "waiting_gate": "等待 L2 核准",
        "direct_answer": "生成问答",
        "design_output": "输出设计方案",
        "coding": "Claude 编码",
        "building": "Gradle 构建",
        "self_review": "自审查",
        "codex_review": "Codex 审查",
        "architect_review": "架构审查",
        "red_team_review": "红队审查",
        "requirement_review": "需求验收",
        "correcting": "修复与重试",
        "git_committing": "Git 提交",
        "creating_pr": "创建 Pull Request",
        "notifying": "发送通知",
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
    }
    return labels.get(state, state)
