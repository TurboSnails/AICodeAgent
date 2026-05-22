#!/bin/bash
set -euo pipefail

# Headless Agent V4 停止脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=install/lib/agent_paths.sh
source "$SCRIPT_DIR/install/lib/agent_paths.sh"

echo "========================================"
echo "  Headless Agent V4 — Stopping..."
echo "========================================"

if [ ! -d "$PID_DIR" ]; then
    echo "[WARN] PID directory not found, nothing to stop."
    exit 0
fi

# 孤儿 executor：占 executor.lock 但不在 pid 文件（会导致新任务永远排队）
if command -v fuser >/dev/null 2>&1 && [ -f "$DATA_DIR/executor.lock" ]; then
    orphan=$(fuser "$DATA_DIR/executor.lock" 2>/dev/null | tr -d ' ')
    if [ -n "$orphan" ]; then
        echo "[STOP] orphan executor holding lock (PID $orphan)"
        kill "$orphan" 2>/dev/null || true
        sleep 1
        kill -9 "$orphan" 2>/dev/null || true
    fi
fi
pkill -f "$PROJECT_ROOT/engine/runner.py" 2>/dev/null || true
# 取消/超时后常残留的 headless claude --print（占 Kimi 并发）
pkill -f "claude -p" 2>/dev/null || true
pkill -f "claude --print" 2>/dev/null || true
echo "" > "$DATA_DIR/executor.current_task" 2>/dev/null || true

for pidfile in "$PID_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    name=$(basename "$pidfile" .pid)
    old_pid=$(cat "$pidfile" 2>/dev/null || true)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        echo "[STOP] $name (PID $old_pid)"
        kill "$old_pid" 2>/dev/null || true
        sleep 1
        # 强制终止
        if kill -0 "$old_pid" 2>/dev/null; then
            kill -9 "$old_pid" 2>/dev/null || true
        fi
    else
        echo "[SKIP] $name (not running)"
    fi
    rm -f "$pidfile"
done

echo "========================================"
echo "  All services stopped."
echo "========================================"
