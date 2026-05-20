#!/bin/bash
set -euo pipefail

# Headless Agent V3 启动脚本
# 启动所有服务：Web UI、Telegram Bot、Serial Executor

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$PROJECT_ROOT/AICodeAgent/data"
PID_DIR="$DATA_DIR/pids"
LOG_DIR="$DATA_DIR/logs"

echo "========================================"
echo "  Headless Agent V3 — Starting..."
echo "========================================"

# --- 环境检查 ---
: "${CLAUDE_CODE_AUTO_ALLOW_BASH:=true}"
: "${ANDROID_HOME:?请设置 ANDROID_HOME 环境变量}"
: "${JAVA_HOME:?请设置 JAVA_HOME 环境变量}"

# --- 依赖检查 ---
_check_cmd() {
    if ! command -v "$1" &> /dev/null; then
        echo "[ERROR] 依赖缺失: $1 未安装或未在 PATH 中"
        exit 1
    fi
}

echo "[CHECK] 验证核心依赖..."
_check_cmd python3
python3 --version

_check_cmd claude
claude --version || { echo "[ERROR] claude CLI 不可用"; exit 1; }

_check_cmd gh
gh --version || { echo "[ERROR] gh CLI 不可用"; exit 1; }

_check_cmd java
java -version 2>&1 | head -n 1

if command -v node &> /dev/null; then
    echo "[CHECK] node $(node -v)（Figma download:site 需要）"
else
    echo "[WARN] node 未安装，带 site_hint 的 Figma 资产拉取将失败"
fi

if [ -f "$PROJECT_ROOT/figma-tools/package.json" ]; then
    echo "[CHECK] figma-tools 已就绪"
else
    echo "[WARN] figma-tools 目录不存在"
fi

if [ -n "${FIGMA_TOKEN:-}" ] || [ -f "$PROJECT_ROOT/figma-tools/.env" ]; then
    echo "[CHECK] FIGMA_TOKEN 或 figma-tools/.env 已配置"
else
    echo "[WARN] 未设置 FIGMA_TOKEN，UI 类任务可能缺少设计资产"
fi

if [ -n "${AGENT_API_KEY:-}" ]; then
    echo "[CHECK] AGENT_API_KEY 已设置（Web UI 认证启用）"
else
    echo "[WARN] AGENT_API_KEY 未设置，Web UI /api/trigger 等端点将无认证保护"
fi

echo "[CHECK] 所有依赖检查通过"
echo ""

# --- 创建目录 ---
mkdir -p "$DATA_DIR" "$PID_DIR" "$LOG_DIR" "$PROJECT_ROOT/AICodeAgent/workspace" "$PROJECT_ROOT/AICodeAgent/db"

# --- 停止旧进程 ---
if [ -d "$PID_DIR" ]; then
    for pidfile in "$PID_DIR"/*.pid; do
        [ -f "$pidfile" ] || continue
        old_pid=$(cat "$pidfile" 2>/dev/null || true)
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            echo "[STOP] Killing old process $old_pid ($(basename "$pidfile" .pid))"
            kill "$old_pid" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$pidfile"
    done
fi

# --- 启动 Web UI Gateway ---
echo "[START] Web UI Gateway (port 6789)..."
cd "$PROJECT_ROOT"
nohup python3 "$PROJECT_ROOT/AICodeAgent/gateway/web_ui_v2.py" \
    > "$LOG_DIR/web_ui.log" 2>&1 &
echo $! > "$PID_DIR/web_ui.pid"
sleep 1

# --- 启动 Telegram Bot Gateway ---
echo "[START] Telegram Bot Gateway..."
nohup python3 "$PROJECT_ROOT/AICodeAgent/gateway/telegram_bot_v2.py" \
    > "$LOG_DIR/telegram_bot.log" 2>&1 &
echo $! > "$PID_DIR/telegram_bot.pid"
sleep 1

# --- 可选：Code Review Graph HTTP（供 graph_bridge 语义检索/影响面）---
if [ "${CRG_AUTO_START:-0}" = "1" ]; then
  echo "[START] Code Review Graph HTTP (port ${CRG_HTTP_PORT:-5555})..."
  nohup code-review-graph serve --http --repo "$PROJECT_ROOT" --port "${CRG_HTTP_PORT:-5555}" \
    >> "$LOG_DIR/crg_http.log" 2>&1 &
  echo $! > "$PID_DIR/crg_http.pid"
  sleep 2
fi

# --- 启动 Serial Executor ---
echo "[START] Serial Executor..."
nohup python3 "$PROJECT_ROOT/AICodeAgent/orchestrator/executor.py" \
    > "$LOG_DIR/executor.log" 2>&1 &
echo $! > "$PID_DIR/executor.pid"
sleep 1

# --- 状态确认 ---
echo ""
echo "========================================"
echo "  All services started!"
echo "========================================"
echo "  Web UI:     http://localhost:6789"
echo "  Logs:       $LOG_DIR/"
echo "  PIDs:       $PID_DIR/"
echo "  Workspace:  $PROJECT_ROOT/AICodeAgent/workspace/"
echo "========================================"
echo ""
echo "Commands:"
echo "  tail -f $LOG_DIR/executor.log    # 查看编排器日志"
echo "  tail -f $LOG_DIR/web_ui.log      # 查看 Web UI 日志"
echo "  $SCRIPT_DIR/stop.sh              # 停止所有服务"
