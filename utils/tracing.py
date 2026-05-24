#!/usr/bin/env python3
"""
LangSmith 可观测性集成 — 可选依赖

激活方式：
    export LANGCHAIN_API_KEY="ls__..."
    export LANGCHAIN_PROJECT="AICodeAgent"      # 可选，默认 "AICodeAgent"
    export LANGCHAIN_TRACING_V2="true"          # 可选，影响 LangChain 自动追踪

追踪树结构（LangSmith 中看到的层级）：
    task-{task_id}  [chain]             ← 整个任务生命周期
      ├─ planning   [chain]             ← 每个阶段（inputs + outputs 在同一 Run）
      │    └─ llm-claude  [llm]         ← LLM 调用（prompt / output / duration）
      ├─ coding     [chain]
      │    └─ llm-claude  [llm]
      ├─ building   [chain]
      └─ completed  [chain]

Bug fixes vs 上一版：
    1. log_phase 之前每次调用都创建独立 RunTree → 现在用 open/close 模式，
       一个阶段 = 一个 Run，inputs 在 open 时写入，outputs/error 在 close 时写入。
    2. trace_llm_call 装饰器错误地把 (args, kwargs) 当 model/prompt 传给 _try_log_llm。
    3. 任务 __exit__ 时没有写入终态信息（terminal_state / error_log）。
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from utils.logging_config import get_logger

logger = get_logger(__name__)

# ─── 全局状态（单进程串行执行器，无需锁）─────────────────────────────────────
_ls_client = None          # 懒加载 langsmith.Client
_current_run = None        # 当前活跃的任务级 RunTree

# LLM 内容截断上限（避免 payload 过大被 LangSmith 拒绝）
_MAX_CHARS = 12_000


# ─── Client 初始化 ──────────────────────────────────────────────────────────

def _get_client():
    """懒加载 LangSmith Client；无 API Key 或未安装时返回 None。"""
    global _ls_client
    if _ls_client is not None:
        return _ls_client

    api_key = os.environ.get("LANGCHAIN_API_KEY", "").strip()
    if not api_key:
        return None

    try:
        from langsmith import Client
        project = os.environ.get("LANGCHAIN_PROJECT", "AICodeAgent")
        _ls_client = Client(api_key=api_key)
        logger.info("LangSmith tracing enabled  project=%s", project)
        return _ls_client
    except ImportError:
        logger.warning("langsmith 未安装，tracing 关闭。运行: pip install langsmith")
        return None
    except Exception as e:
        logger.warning("LangSmith Client 初始化失败: %s", e)
        return None


def is_enabled() -> bool:
    """返回 LangSmith 是否可用且已配置。"""
    return _get_client() is not None


# ─── TaskTracer ──────────────────────────────────────────────────────────────

class TaskTracer:
    """
    任务级追踪上下文管理器。

    用法（engine/core.py 中已集成）：
        with trace_task(task_id, requirement, level=task.level) as tracer:
            tracer.log_phase_start("planning", {"prompt_snippet": "..."})
            ...
            tracer.log_phase_end("planning", {"next_state": "coding"})

    LangSmith 上看到的层级：
        task-{id} → planning → llm-claude → ...
    """

    def __init__(self, task_id: str, requirement: str, level: str = "", **metadata):
        self.task_id   = task_id
        self.requirement = requirement
        self.level     = level
        self.metadata  = metadata
        self._run      = None
        self._client   = _get_client()
        self._phase_runs: dict[str, Any] = {}  # phase_name → RunTree（开放中）
        self._start_times: dict[str, float] = {}

    def __enter__(self) -> "TaskTracer":
        global _current_run
        if not self._client:
            return self
        try:
            from langsmith.run_trees import RunTree
            project = os.environ.get("LANGCHAIN_PROJECT", "AICodeAgent")
            self._run = RunTree(
                name=f"task-{self.task_id}",
                run_type="chain",
                project_name=project,
                inputs={
                    "requirement": self.requirement[:2000],
                    "level":       self.level,
                    "task_id":     self.task_id,
                    **{k: str(v)[:500] for k, v in self.metadata.items()},
                },
            )
            self._run.post()
            _current_run = self._run
        except Exception as e:
            logger.debug("LangSmith task start failed: %s", e)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global _current_run
        if self._run is _current_run:
            _current_run = None
        if not self._run:
            return

        # 关闭所有未关闭的 phase（异常退出时）
        for phase_name, phase_run in list(self._phase_runs.items()):
            try:
                phase_run.end(
                    outputs={},
                    error=str(exc_val) if exc_val else "task exited unexpectedly",
                )
                phase_run.patch()
            except Exception:
                pass
        self._phase_runs.clear()

        try:
            self._run.end(
                outputs={"error": str(exc_val)} if exc_val else {},
                error=str(exc_val) if exc_val else None,
            )
            self._run.patch()
        except Exception as e:
            logger.debug("LangSmith task end failed: %s", e)

    def finish(self, terminal_state: str, error_log: str = "", branch: str = "", pr_url: str = "") -> None:
        """
        任务到达终态时调用，写入最终结果到根 Run。
        engine/core.py 在 process_task 退出前调用。
        """
        if not self._run:
            return
        try:
            outputs: dict = {"terminal_state": terminal_state}
            if branch:   outputs["branch"]    = branch
            if pr_url:   outputs["pr_url"]    = pr_url
            if error_log: outputs["error_log"] = error_log[:2000]
            self._run.end(
                outputs=outputs,
                error=error_log[:500] if terminal_state in ("failed", "cancelled") else None,
            )
            self._run.patch()
        except Exception as e:
            logger.debug("LangSmith task finish failed: %s", e)

    # ── Phase tracing ────────────────────────────────────────────────────────

    def log_phase_start(self, phase: str, inputs: dict | None = None) -> None:
        """
        开始一个阶段 span。与 log_phase_end 配对使用。
        inputs 里放对定位有用的信息：prompt 片段、fix items count 等。
        """
        if not self._run:
            return
        try:
            from langsmith.run_trees import RunTree
            child = RunTree(
                name=phase,
                run_type="chain",
                inputs=_trunc_dict(inputs or {}),
                parent_run=self._run,
            )
            child.post()
            self._phase_runs[phase] = child
            self._start_times[phase] = time.monotonic()
        except Exception as e:
            logger.debug("LangSmith phase_start failed (%s): %s", phase, e)

    def log_phase_end(
        self,
        phase: str,
        outputs: dict | None = None,
        error: str | None = None,
    ) -> None:
        """
        结束一个已开始的阶段 span，写入 outputs / error。
        """
        child = self._phase_runs.pop(phase, None)
        if not child:
            # 没有对应的 start → fallback：创建完整 span
            self._log_phase_single(phase, {}, outputs or {}, error)
            return
        try:
            elapsed = time.monotonic() - self._start_times.pop(phase, time.monotonic())
            out = dict(outputs or {})
            out["duration_s"] = round(elapsed, 2)
            child.end(outputs=_trunc_dict(out), error=error)
            child.patch()
        except Exception as e:
            logger.debug("LangSmith phase_end failed (%s): %s", phase, e)

    def _log_phase_single(
        self,
        phase: str,
        inputs: dict,
        outputs: dict,
        error: str | None,
    ) -> None:
        """创建并立即关闭一个 phase span（向后兼容旧 log_phase 调用）。"""
        if not self._run:
            return
        try:
            from langsmith.run_trees import RunTree
            child = RunTree(
                name=phase,
                run_type="chain",
                inputs=_trunc_dict(inputs),
                parent_run=self._run,
            )
            child.post()
            child.end(outputs=_trunc_dict(outputs), error=error)
            child.patch()
        except Exception as e:
            logger.debug("LangSmith phase single failed (%s): %s", phase, e)

    # ── 向后兼容旧接口（engine/core.py 仍在使用）───────────────────────────

    def log_phase(
        self,
        phase: str,
        inputs: dict | None = None,
        outputs: dict | None = None,
        error: str | None = None,
    ) -> None:
        """
        向后兼容接口。
        - 只有 inputs  → log_phase_start
        - 只有 outputs → log_phase_end
        - 两者都有     → 单次完整 span
        - error        → log_phase_end with error
        """
        has_inputs  = bool(inputs)
        has_outputs = bool(outputs)
        has_error   = error is not None

        if has_error:
            if phase in self._phase_runs:
                self.log_phase_end(phase, outputs={}, error=error)
            else:
                self._log_phase_single(phase, inputs or {}, {}, error)
        elif has_inputs and not has_outputs:
            self.log_phase_start(phase, inputs)
        elif has_outputs and not has_inputs:
            self.log_phase_end(phase, outputs)
        else:
            self._log_phase_single(phase, inputs or {}, outputs or {}, None)


# ─── 顶层快捷函数 ──────────────────────────────────────────────────────────

def trace_task(task_id: str, requirement: str, level: str = "", **metadata) -> TaskTracer:
    """快捷函数：创建任务级追踪上下文，与 with 语句配合使用。"""
    return TaskTracer(task_id, requirement, level, **metadata)


def _try_log_llm(
    model: str,
    prompt: str,
    output: str,
    duration: float,
    error: str | None = None,
    metadata: dict | None = None,
) -> None:
    """
    尝试将一次 LLM 调用记录到当前活跃的任务 Run 下。
    追踪失败不阻断主流程。
    """
    global _current_run
    if _current_run is None:
        return
    try:
        from langsmith.run_trees import RunTree
        child = RunTree(
            name=f"llm-{model}",
            run_type="llm",
            inputs={"prompt": str(prompt)[:_MAX_CHARS]},
            parent_run=_current_run,
        )
        child.post()
        child.end(
            outputs={"output": str(output)[:_MAX_CHARS]},
            error=error,
            extra={"model": model, "duration_s": round(duration, 2), **(metadata or {})},
        )
        child.patch()
    except Exception:
        pass  # 追踪失败绝不影响主流程


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def _trunc_dict(d: dict, max_chars: int = _MAX_CHARS) -> dict:
    """截断 dict 中过长的字符串值，避免 LangSmith payload 超限。"""
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > max_chars:
            out[k] = v[:max_chars] + f"… (truncated, total {len(v)} chars)"
        else:
            out[k] = v
    return out
