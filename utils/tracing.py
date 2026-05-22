#!/usr/bin/env python3
"""
LangSmith 可观测性集成 — 可选依赖

用法：
    设置环境变量 LANGCHAIN_API_KEY 即可启用追踪。
    未设置时自动降级为无操作，不影响主流程。

追踪范围：
    - 任务级别：AgentEngine.process_task
    - 阶段级别：AgentEngine._execute_phase
    - LLM 调用级别：AIClient.call / AIClient.call_codex
"""

from __future__ import annotations

import functools
import os
import time
from typing import Any, Callable

from utils.logging_config import get_logger

logger = get_logger(__name__)

# 懒加载 langsmith，避免未安装时崩溃
_ls_client = None

# 当前活跃的任务级 RunTree（单进程串行执行器下安全）
_current_run = None


def _get_client():
    """懒加载 LangSmith Client（线程安全由 GIL 保证）"""
    global _ls_client
    if _ls_client is not None:
        return _ls_client

    api_key = os.environ.get("LANGCHAIN_API_KEY", "")
    if not api_key:
        return None

    try:
        from langsmith import Client
        _ls_client = Client(api_key=api_key)
        logger.info("LangSmith tracing enabled (project=%s)", os.environ.get("LANGCHAIN_PROJECT", "default"))
        return _ls_client
    except ImportError:
        logger.warning("langsmith not installed, tracing disabled. Run: pip install langsmith")
        return None


def is_enabled() -> bool:
    """检查 LangSmith 是否可用且已配置"""
    return _get_client() is not None


class TaskTracer:
    """
    任务级追踪上下文管理器。

    用法：
        with TaskTracer(task_id="abc", requirement="...") as tracer:
            tracer.log_phase("planning", {"output": "..."})
            tracer.log_llm_call("claude", prompt="...", output="...", duration=12.3)
    """

    def __init__(self, task_id: str, requirement: str, level: str = "", **metadata):
        self.task_id = task_id
        self.requirement = requirement
        self.level = level
        self.metadata = metadata
        self._run = None
        self._client = _get_client()

    def __enter__(self):
        global _current_run
        if not self._client:
            return self
        try:
            from langsmith.run_trees import RunTree
            self._run = RunTree(
                name=f"task-{self.task_id}",
                run_type="chain",
                inputs={"requirement": self.requirement, "level": self.level, **self.metadata},
                extra={"task_id": self.task_id},
            )
            self._run.post()
            _current_run = self._run
        except Exception as e:
            logger.debug("LangSmith task trace start failed: %s", e)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global _current_run
        if self._run is _current_run:
            _current_run = None
        if not self._client or not self._run:
            return
        try:
            outputs = {}
            if exc_val:
                outputs["error"] = str(exc_val)
            self._run.end(outputs=outputs, error=str(exc_val) if exc_val else None)
        except Exception as e:
            logger.debug("LangSmith task trace end failed: %s", e)

    def log_phase(self, phase: str, inputs: dict | None = None, outputs: dict | None = None, error: str | None = None):
        """记录一个阶段执行"""
        if not self._client or not self._run:
            return
        try:
            from langsmith.run_trees import RunTree
            child = RunTree(
                name=phase,
                run_type="chain",
                inputs=inputs or {},
                outputs=outputs or {},
                error=error,
                parent_run=self._run,
            )
            child.post()
            child.end()
        except Exception as e:
            logger.debug("LangSmith phase trace failed: %s", e)

    def log_llm_call(self, model: str, prompt: str, output: str, duration: float, metadata: dict | None = None):
        """记录一次 LLM 调用"""
        if not self._client or not self._run:
            return
        try:
            from langsmith.run_trees import RunTree
            child = RunTree(
                name=f"llm-{model}",
                run_type="llm",
                inputs={"prompt": prompt[:8000]},  # 截断避免过大
                outputs={"output": output[:8000]},
                extra={"model": model, "duration_s": duration, **(metadata or {})},
                parent_run=self._run,
            )
            child.post()
            child.end()
        except Exception as e:
            logger.debug("LangSmith LLM trace failed: %s", e)


def trace_task(task_id: str, requirement: str, level: str = "", **metadata):
    """快捷函数：创建任务级追踪上下文"""
    return TaskTracer(task_id, requirement, level, **metadata)


def trace_llm_call(func: Callable) -> Callable:
    """
    装饰器：自动追踪 LLM 调用（prompt / output / duration）。
    适用于 AIClient.call / call_codex 等方法。
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        client = _get_client()
        if not client:
            return func(*args, **kwargs)

        start = time.time()
        try:
            result = func(*args, **kwargs)
            duration = time.time() - start
            _try_log_llm(args, kwargs, result, duration, error=None)
            return result
        except Exception as e:
            duration = time.time() - start
            _try_log_llm(args, kwargs, "", duration, error=str(e))
            raise

    return wrapper


def _try_log_llm(model: str, prompt: str, output: str, duration: float, error: str | None = None, metadata: dict | None = None):
    """辅助：尝试记录 LLM 调用到 LangSmith（自动关联当前任务）"""
    global _current_run
    if _current_run is None:
        return
    try:
        from langsmith.run_trees import RunTree
        child = RunTree(
            name=f"llm-{model}",
            run_type="llm",
            inputs={"prompt": str(prompt)[:8000]},
            outputs={"output": str(output)[:8000]},
            error=error,
            extra={"model": model, "duration_s": round(duration, 2), **(metadata or {})},
            parent_run=_current_run,
        )
        child.post()
        child.end()
    except Exception:
        pass  # 追踪失败不阻断主流程
