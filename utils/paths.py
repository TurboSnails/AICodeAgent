#!/usr/bin/env python3
"""
统一路径管理模块

用法：
    from utils.paths import AGENT_ROOT, PROJECT_ROOT, WORKSPACE_ROOT, DATA_DIR

部署在 wm/AICodeAgent/ 时，PROJECT_ROOT 自动指向上级 Android 工程根目录。
"""

from pathlib import Path

# AICodeAgent 包根目录（本仓库内的 agent 代码）
AGENT_ROOT = Path(__file__).resolve().parents[1]

_parent = AGENT_ROOT.parent
if (_parent / "app").is_dir() and (
    (_parent / "build.gradle.kts").exists() or (_parent / "settings.gradle.kts").exists()
):
    PROJECT_ROOT = _parent
else:
    PROJECT_ROOT = AGENT_ROOT

# 运行时数据与工作区（始终在 AICodeAgent 下）
DATA_DIR = AGENT_ROOT / "data"
WORKSPACE_ROOT = AGENT_ROOT / "workspace"
DB_FILE = DATA_DIR / "agent.db"
