#!/usr/bin/env python3
"""
orchestrator 兼容层 — V4 重构

⚠️ 已弃用：所有模块已迁移到 engine/、services/、utils/、phases/。
本文件仅为向后兼容保留，内部全部重新导出。
新代码请直接从目标位置导入：
    from engine.state_machine import State, Task
    from utils.config_loader import cfg_str
    from utils.logging_config import get_logger
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "orchestrator 包已弃用。请直接从 engine/、utils/、services/、phases/ 导入对应模块",
    DeprecationWarning,
    stacklevel=2,
)

# 状态机 → engine.state_machine
from engine.state_machine import (
    State,
    Task,
    init_db,
    save_task,
    get_task,
    transition,
    get_executable_tasks,
    approve_gate_resume,
    get_all_non_terminal_tasks,
    get_task_state_history,
    get_waiting_gates,
    get_waiting_clarifications,
    approve_clarification_reply,
    cancel_task,
    DB_FILE,
    VALID_TRANSITIONS,
)

# 配置加载 → utils.config_loader
from utils.config_loader import (
    Config,
    get_config,
    cfg,
    cfg_str,
    cfg_int,
    cfg_float,
    cfg_bool,
)

# 日志配置 → utils.logging_config
from utils.logging_config import get_logger

# 逃逸检测 → utils.escape_detector
from utils.escape_detector import detect_unsolvable, record_escape

# 资产嗅探 → services.asset_manager
from services.asset_manager import sanitize_asset_name

# 图谱桥接 → utils.graph_bridge
from utils.graph_bridge import get_impact_summary, extract_files_from_consensus

# 平台适配 → services.platform_figma（按需导出公共 API）
# from services.platform_figma import ...

# 审查工具 → phases._review_utils
from phases._review_utils import (
    parse_codex_verdict,
    list_changed_files,
    _run_cmd,
    workspace_context,
    read_changed_sources,
    build_codex_review_prompt,
    build_requirement_acceptance_prompt,
    build_red_team_prompt,
)

__all__ = [
    # state_machine
    "State",
    "Task",
    "init_db",
    "save_task",
    "get_task",
    "transition",
    "get_executable_tasks",
    "approve_gate_resume",
    "get_all_non_terminal_tasks",
    "get_task_state_history",
    "get_waiting_gates",
    "get_waiting_clarifications",
    "approve_clarification_reply",
    "cancel_task",
    "DB_FILE",
    "VALID_TRANSITIONS",
    # config_loader
    "Config",
    "get_config",
    "cfg",
    "cfg_str",
    "cfg_int",
    "cfg_float",
    "cfg_bool",
    # logging
    "get_logger",
    # escape_detector
    "detect_unsolvable",
    "record_escape",
    # asset_manager
    "sanitize_asset_name",
    # graph_bridge
    "get_impact_summary",
    "extract_files_from_consensus",
    # platform_figma
    # review_utils
    "parse_codex_verdict",
    "list_changed_files",
    "_run_cmd",
    "workspace_context",
    "read_changed_sources",
    "build_codex_review_prompt",
    "build_requirement_acceptance_prompt",
    "build_red_team_prompt",
]
