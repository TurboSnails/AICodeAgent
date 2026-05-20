#!/bin/bash
set -euo pipefail

# Headless Agent V3 状态脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_DIR="$PROJECT_ROOT/AICodeAgent/data/pids"
LOG_DIR="$PROJECT_ROOT/AICodeAgent/data/logs"

echo "========================================"
echo "  Headless Agent V3 — Status"
echo "========================================"

if [ -d "$PID_DIR" ]; then
    for pidfile in "$PID_DIR"/*.pid; do
        [ -f "$pidfile" ] || continue
        name=$(basename "$pidfile" .pid)
        pid=$(cat "$pidfile" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "  ✅ $name  |  PID $pid  |  running"
        else
            echo "  ❌ $name  |  PID $pid  |  dead"
        fi
    done
else
    echo "  No PID directory found."
fi

echo ""
echo "  Workspace:  $PROJECT_ROOT/AICodeAgent/workspace/"
echo "  Logs:       $LOG_DIR/"
echo "  Web UI:     http://localhost:6789"
echo "========================================"
