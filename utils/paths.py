#!/usr/bin/env python3
"""
统一路径管理模块

用法：
    from utils.paths import PROJECT_ROOT, WORKSPACE_ROOT, DATA_DIR
"""

from pathlib import Path

# AICodeAgent 根目录（即当前 git 仓库根目录）
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 运行时数据目录
DATA_DIR = PROJECT_ROOT / "data"

# 任务工作区根目录
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"

# SQLite 数据库文件路径
DB_FILE = DATA_DIR / "agent.db"
