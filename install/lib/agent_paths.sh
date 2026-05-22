#!/bin/bash
# Headless Agent V4 — 统一路径定义
# 被 start.sh / stop.sh / status.sh 共用

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../.. && pwd)"

# 优先使用 .venv，否则回退系统 python3
if [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    AGENT_PYTHON="$SCRIPT_DIR/.venv/bin/python3"
elif [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    AGENT_PYTHON="$SCRIPT_DIR/.venv/bin/python"
else
    AGENT_PYTHON="python3"
fi

# V4: 项目根目录即当前仓库根目录（AICodeAgent 本身）
PROJECT_ROOT="$SCRIPT_DIR"

# 运行时目录
DATA_DIR="$PROJECT_ROOT/data"
PID_DIR="$DATA_DIR/pids"
LOG_DIR="$DATA_DIR/logs"
