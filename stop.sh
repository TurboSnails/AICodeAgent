#!/bin/bash
set -euo pipefail

# Headless Agent V3 停止脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_DIR="$PROJECT_ROOT/AICodeAgent/data/pids"

echo "========================================"
echo "  Headless Agent V3 — Stopping..."
echo "========================================"

if [ ! -d "$PID_DIR" ]; then
    echo "[WARN] PID directory not found, nothing to stop."
    exit 0
fi

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
