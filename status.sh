#!/bin/bash
set -euo pipefail

# Headless Agent V4 状态脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=install/lib/agent_paths.sh
source "$SCRIPT_DIR/install/lib/agent_paths.sh"

echo "========================================"
echo "  Headless Agent V4 — Status"
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
echo "  Workspace:  $PROJECT_ROOT/workspace/"
echo "  Logs:       $LOG_DIR/"
echo "  Web UI:     http://localhost:6789"
echo "========================================"
