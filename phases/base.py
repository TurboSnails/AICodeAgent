#!/usr/bin/env python3
"""
阶段处理器基类 — V4 重构
定义 PhaseHandler 接口和 PhaseResult，所有阶段处理器必须继承此类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.state_machine import State, Task


@dataclass
class PhaseResult:
    """
    阶段处理器执行结果。

    :param next_state: 目标状态
    :param reason: 流转原因（用于 state_history 记录）
    :param artifacts: 阶段产生的中间产物（如 fix_prompt、build_log 等），供后续阶段使用
    """
    next_state: State
    reason: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)


class PhaseHandler(ABC):
    """
    阶段处理器抽象基类。

    每个状态（coding, building, codex_review 等）对应一个 PhaseHandler。
    Engine 根据当前状态 dispatch 到对应的 handler，handler 执行完成后返回 PhaseResult，
    Engine 负责调用 transition 进行状态流转。
    """

    @abstractmethod
    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        """
        执行阶段逻辑。

        :param task: 当前任务对象
        :param workspace: 任务工作区路径
        :param kwargs: 引擎注入的额外依赖（如 ai_client, build_service 等）
        :return: PhaseResult 包含下一状态和原因
        :raises AgentRecoverableError: 可恢复错误，引擎自动流转到 correcting
        :raises AgentFatalError: 致命错误，引擎自动流转到 failed
        """
        ...

    def can_handle(self, task: Task) -> bool:
        """是否可处理此任务（默认始终可处理）。子类可覆盖以实现条件处理。"""
        return True

    def on_enter(self, task: Task, workspace: Path) -> None:
        """
        进入阶段前的钩子（可选）。
        可用于初始化、清理、准备上下文等。
        """
        pass

    def on_exit(self, task: Task, workspace: Path, result: PhaseResult) -> None:
        """
        离开阶段后的钩子（可选）。
        可用于保存中间结果、记录审计日志等。
        """
        pass
