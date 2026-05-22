#!/usr/bin/env python3
"""
engine 包 — V4 重构核心引擎

提供：
  - AgentEngine: 状态机引擎（显式处理器注册表）
  - AgentException 层次: 统一异常体系
  - ConfigValidator: 配置预校验
  - runner: V4 执行器入口（替换旧 executor.py）

用法：
    from engine import AgentEngine, validate_config
    from engine.runner import run_loop
"""

from __future__ import annotations

from engine.config_validator import ConfigValidator, validate_config
from engine.core import AgentEngine
from engine.exceptions import (
    AgentCliUnavailableError,
    AgentContextLengthError,
    AgentEmptyOutputError,
    AgentException,
    AgentFatalError,
    AgentRateLimitError,
    AgentRecoverableError,
    AgentTimeoutError,
    BuildFailureError,
    ConsensusValidationError,
    DebateTimeoutError,
    DependencyViolationError,
    GitCommandError,
    PrCreationError,
)

__all__ = [
    "AgentEngine",
    "ConfigValidator",
    "validate_config",
    # 异常层次
    "AgentException",
    "AgentRecoverableError",
    "AgentFatalError",
    "AgentTimeoutError",
    "AgentContextLengthError",
    "AgentEmptyOutputError",
    "AgentRateLimitError",
    "AgentCliUnavailableError",
    "BuildFailureError",
    "GitCommandError",
    "PrCreationError",
    "DependencyViolationError",
    "DebateTimeoutError",
    "ConsensusValidationError",
]
