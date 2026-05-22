#!/bin/bash
set -euo pipefail

# Headless Agent V4 启动脚本
# 启动所有服务：Web UI、Telegram Bot、Serial Executor

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=install/lib/agent_paths.sh
source "$SCRIPT_DIR/install/lib/agent_paths.sh"

echo "========================================"
echo "  Headless Agent V4 — Starting..."
echo "========================================"

# --- 环境检查 ---
: "${CLAUDE_CODE_AUTO_ALLOW_BASH:=true}"
# 第三方网关若未支持 claude-opus-4-7，用 4.6；与 .claude/settings.json 一致
: "${CLAUDE_MODEL:=claude-sonnet-4-6}"
: "${ANTHROPIC_DEFAULT_OPUS_MODEL:=claude-opus-4-6}"
: "${ANTHROPIC_DEFAULT_SONNET_MODEL:=claude-sonnet-4-6}"
export CLAUDE_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL

# orchestrator 的 claude --print：仅用当前 shell 的 ANTHROPIC_*（与 cc-use / IDE 切换无关）
if [[ -n "${ANTHROPIC_BASE_URL:-}" ]]; then
  echo "[CHECK] ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL CLAUDE_MODEL=${CLAUDE_MODEL:-default}"
  if command -v curl &>/dev/null; then
    code=$(curl -s -o /dev/null -w "%{http_code}" -m 5 \
      "${ANTHROPIC_BASE_URL%/}/v1/models" \
      -H "Authorization: Bearer ${ANTHROPIC_API_KEY:-}" 2>/dev/null || echo "000")
    if [[ "$code" != "200" ]]; then
      echo "[WARN] 端点 /v1/models 返回 HTTP $code，claude --print 可能失败" >&2
    fi
  fi
else
  echo "[CHECK] 未设 ANTHROPIC_BASE_URL，claude --print 将使用官方 Anthropic API"
fi

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
_check_cmd "$AGENT_PYTHON"
"$AGENT_PYTHON" --version

_check_cmd claude
claude --version || { echo "[ERROR] claude CLI 不可用"; exit 1; }

_check_cmd gh
gh --version || { echo "[ERROR] gh CLI 不可用"; exit 1; }

_check_cmd java
java -version 2>&1 | head -n 1

if [ -n "${FIGMA_TOKEN:-}" ]; then
    echo "[CHECK] FIGMA_TOKEN 已配置"
else
    echo "[WARN] 未设置 FIGMA_TOKEN，UI 类任务可能缺少设计资产"
fi

if [ -n "${AGENT_API_KEY:-}" ]; then
    echo "[CHECK] AGENT_API_KEY 已设置（Web UI 认证启用）"
else
    echo "[WARN] AGENT_API_KEY 未设置，Web UI /api/trigger 等端点将无认证保护"
fi

echo "[CHECK] 所有依赖检查通过"

# --- TencentDB Agent Memory Gateway（config/local.yaml enabled 时建议运行）---
if command -v memory-tencentdb-ctl &>/dev/null; then
    if memory-tencentdb-ctl health &>/dev/null; then
        echo "[CHECK] TencentDB memory gateway OK (127.0.0.1:8420)"
    else
        echo "[START] TencentDB memory gateway..."
        memory-tencentdb-ctl start 2>/dev/null || echo "[WARN] memory gateway 启动失败，任务将降级无记忆"
    fi
else
    echo "[WARN] memory-tencentdb-ctl 未在 PATH，跳过记忆 Gateway（见 README TencentDB 章节）"
fi
echo ""

# --- 创建目录 ---
mkdir -p "$DATA_DIR" "$PID_DIR" "$LOG_DIR" "$PROJECT_ROOT/workspace" "$PROJECT_ROOT/data/db"

# --- 停止旧进程（含孤儿 executor，避免占锁导致任务永远 pending）---
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
if command -v fuser >/dev/null 2>&1 && [ -f "$DATA_DIR/executor.lock" ]; then
    orphan=$(fuser "$DATA_DIR/executor.lock" 2>/dev/null | tr -d ' ')
    if [ -n "$orphan" ] && kill -0 "$orphan" 2>/dev/null; then
        echo "[STOP] Killing orphan executor on lock (PID $orphan)"
        kill "$orphan" 2>/dev/null || true
        sleep 1
        kill -9 "$orphan" 2>/dev/null || true
    fi
fi
pkill -f "$PROJECT_ROOT/engine/runner.py" 2>/dev/null || true
echo "" > "$DATA_DIR/executor.current_task" 2>/dev/null || true
sleep 1

# --- 启动 Web UI Gateway ---
echo "[START] Web UI Gateway (port 6789)..."
cd "$PROJECT_ROOT"
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$PROJECT_ROOT"
nohup "$AGENT_PYTHON" "$PROJECT_ROOT/gateway/web_ui.py" \
    > "$LOG_DIR/web_ui.log" 2>&1 &
echo $! > "$PID_DIR/web_ui.pid"
sleep 1

# --- 启动 Telegram Bot Gateway ---
echo "[START] Telegram Bot Gateway..."
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$PROJECT_ROOT"
nohup "$AGENT_PYTHON" "$PROJECT_ROOT/gateway/telegram_bot.py" \
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

# --- 启动 V4 Serial Executor ---
echo "[START] V4 Serial Executor (engine/runner)..."
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$PROJECT_ROOT"
nohup "$AGENT_PYTHON" "$PROJECT_ROOT/engine/runner.py" \
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
echo "  Workspace:  $PROJECT_ROOT/workspace/"
echo "========================================"
echo ""
echo "Commands:"
echo "  tail -f $LOG_DIR/executor.log    # 查看编排器日志"
echo "  tail -f $LOG_DIR/web_ui.log      # 查看 Web UI 日志"
echo "  $SCRIPT_DIR/stop.sh              # 停止所有服务"
