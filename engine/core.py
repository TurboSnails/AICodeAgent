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
from typing import TYPE_CHECKING, Optional

from engine.exceptions import AgentFatalError, AgentRecoverableError, TaskCancelledError
from utils.logging_config import get_logger
from utils.paths import WORKSPACE_ROOT
from utils.phase_status import write_phase_status
from utils.tracing import trace_task, is_enabled
from utils.task_cancel import is_task_cancelled, raise_if_cancelled
from engine.state_machine import State, Task, get_task, save_task, transition

if TYPE_CHECKING:
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
                        # LangSmith：写入任务终态
                        tracer.finish(
                            terminal_state=result.next_state.value,
                            error_log=task.error_log or "",
                            branch=task.branch or "",
                            pr_url=task.pr_url or "",
                        )
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

        from phases.base import PhaseResult  # local import breaks engine↔phases circular dep
        try:
            raise_if_cancelled(task_id)
            write_phase_status(workspace, current.value, f"正在执行：{_phase_label(current.value)}")
            # LangSmith：开始阶段 span，写入对排查有用的上下文
            if tracer:
                tracer.log_phase_start(current.value, inputs=_phase_inputs(task, current.value, workspace))
            handler.on_enter(task, workspace)
            result = handler.handle(task, workspace)
            raise_if_cancelled(task_id)
            handler.on_exit(task, workspace, result)
            write_phase_status(
                workspace,
                result.next_state.value,
                result.reason or _phase_label(result.next_state.value),
            )
            # LangSmith：关闭阶段 span，写入结果
            if tracer:
                tracer.log_phase_end(
                    current.value,
                    outputs=_phase_outputs(task, result, workspace),
                )
            return result

        except TaskCancelledError:
            write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
            if tracer:
                tracer.log_phase_end(current.value, error="cancelled")
            raise

        except AgentRecoverableError as e:
            if is_task_cancelled(task_id):
                write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
                if tracer:
                    tracer.log_phase_end(current.value, error="cancelled")
                raise TaskCancelledError(f"task {task_id} cancelled during {current.value}") from e
            logger.warning("Recoverable error in %s: %s", current.value, e)
            task.error_log = str(e)
            save_task(task)
            write_phase_status(workspace, current.value, f"可恢复错误：{e}", extra={"error": str(e)})
            if tracer:
                tracer.log_phase_end(current.value, error=str(e))
            return PhaseResult(State.CORRECTING, f"recoverable: {e}")

        except AgentFatalError as e:
            if is_task_cancelled(task_id):
                write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
                if tracer:
                    tracer.log_phase_end(current.value, error="cancelled")
                raise TaskCancelledError(f"task {task_id} cancelled during {current.value}") from e
            logger.error("Fatal error in %s: %s", current.value, e)
            task.error_log = str(e)
            save_task(task)
            if tracer:
                tracer.log_phase_end(current.value, error=str(e))
            return PhaseResult(State.FAILED, f"fatal: {e}")

        except Exception as e:
            if is_task_cancelled(task_id):
                write_phase_status(workspace, State.CANCELLED.value, "任务已取消")
                if tracer:
                    tracer.log_phase_end(current.value, error="cancelled")
                raise TaskCancelledError(f"task {task_id} cancelled during {current.value}") from e
            logger.exception("Unexpected error in %s: %s", current.value, e)
            task.error_log = f"unexpected: {e}"
            save_task(task)
            if tracer:
                tracer.log_phase_end(current.value, error=f"unexpected: {type(e).__name__}: {e}")
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


# ─── LangSmith 阶段 inputs/outputs 构建 ──────────────────────────────────────

def _phase_inputs(task: "Task", phase: str, workspace: "Path") -> dict:
    """
    为每个阶段收集对排查真正有用的上下文。
    出现在 LangSmith 的 Run inputs 面板中。
    """
    import json as _json

    base: dict = {
        "task_id":    task.task_id,
        "level":      task.level,
        "phase":      phase,
        "requirement": task.raw_requirement[:500],
    }
    if task.site_hint:
        base["site_hint"] = task.site_hint

    # coding → 显示 fix_prompt（如果有）
    if phase in ("coding", "correcting"):
        fix_path = workspace / "single_fix_prompt.md"
        if fix_path.is_file():
            try:
                base["fix_prompt_snippet"] = fix_path.read_text(errors="replace")[:1500]
            except OSError:
                pass

    # building → 显示上一次构建错误（如果有）
    if phase == "building":
        err_path = workspace / "build_error.txt"
        if err_path.is_file():
            try:
                base["prev_build_error"] = err_path.read_text(errors="replace")[-1500:]
            except OSError:
                pass

    # correcting → 显示 fix_plan 摘要
    if phase == "correcting":
        fp_path = workspace / "fix_plan.json"
        if fp_path.is_file():
            try:
                fp = _json.loads(fp_path.read_text())
                items = fp if isinstance(fp, list) else fp.get("items", [])
                base["fix_items_count"] = len(items)
                base["fix_priorities"]  = [i.get("priority") for i in items[:5]]
            except Exception:
                pass

    # review phases → 显示 review 结论
    if phase in ("codex_review", "architect_review", "red_team_review", "requirement_review", "self_review"):
        report_map = {
            "codex_review":        "codex_review.md",
            "architect_review":    "architect_review.md",
            "red_team_review":     "red_team_review.md",
            "requirement_review":  "requirement_review.md",
            "self_review":         "self_review.md",
        }
        rp = workspace / report_map.get(phase, "")
        if rp.is_file():
            try:
                base["prev_review_snippet"] = rp.read_text(errors="replace")[:1000]
            except OSError:
                pass

    return base


def _phase_outputs(task: "Task", result: "PhaseResult", workspace: "Path") -> dict:
    """
    记录阶段执行结果，供 LangSmith 展示。
    出现在 Run outputs 面板中。
    """
    import json as _json

    out: dict = {
        "next_state": result.next_state.value,
        "reason":     result.reason or "",
    }

    # building → 显示构建成功/失败
    if task.current_state == "building":
        err_path = workspace / "build_error.txt"
        if err_path.is_file():
            try:
                out["build_error"] = err_path.read_text(errors="replace")[-2000:]
            except OSError:
                pass

    # coding → 显示 apply_diag
    if task.current_state == "coding":
        diag_path = workspace / "coding_apply_diag.json"
        if diag_path.is_file():
            try:
                diag = _json.loads(diag_path.read_text())
                out["files_applied"]    = diag.get("applied_count", 0)
                out["file_markers"]     = diag.get("file_markers_found", 0)
            except Exception:
                pass

    return out
