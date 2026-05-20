#!/bin/bash
# smoke_l0.sh — 提交 L0 任务并轮询直到终态（需已启动 executor + web_ui）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PORT="${AGENT_WEB_PORT:-6789}"
BASE="http://127.0.0.1:${PORT}"
POLL_SEC="${SMOKE_POLL_SEC:-15}"
TIMEOUT_SEC="${SMOKE_TIMEOUT_SEC:-7200}"
REQUIREMENT="${SMOKE_REQUIREMENT:-在 app/src/main/res/values/strings.xml 增加名为 agent_smoke_test 的字符串，值为 Agent Smoke Test}"

AUTH_HEADER=()
if [ -n "${AGENT_API_KEY:-}" ]; then
  AUTH_HEADER=(-H "Authorization: Bearer ${AGENT_API_KEY}")
fi

echo "[SMOKE] 检查 Web UI ${BASE}/health ..."
if ! curl -sf "${BASE}/health" >/dev/null; then
  echo "[ERROR] Web UI 未响应。请先: AICodeAgent/start.sh"
  exit 1
fi

echo "[SMOKE] 提交 L0 任务 ..."
PAYLOAD=$(REQUIREMENT="$REQUIREMENT" python3 -c 'import json, os; print(json.dumps({"raw_requirement": os.environ["REQUIREMENT"], "level": "L0"}))')
RESP=$(curl -sf -X POST "${BASE}/api/trigger" \
  "${AUTH_HEADER[@]}" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

TASK_ID=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('task_id',''))")
if [ -z "$TASK_ID" ]; then
  echo "[ERROR] 提交失败: $RESP"
  exit 1
fi
echo "[SMOKE] task_id=$TASK_ID"

START=$(date +%s)
while true; do
  ELAPSED=$(( $(date +%s) - START ))
  if [ "$ELAPSED" -gt "$TIMEOUT_SEC" ]; then
    echo "[ERROR] 超时 (${TIMEOUT_SEC}s)，最后状态未知"
    exit 1
  fi

  DETAIL=$(curl -sf "${BASE}/api/task/${TASK_ID}" "${AUTH_HEADER[@]}" 2>/dev/null || echo '{}')
  STATE=$(echo "$DETAIL" | python3 -c "
import sys, json
d = json.load(sys.stdin)
t = d.get('task') or {}
print(t.get('current_state', 'not_found'))
" 2>/dev/null || echo "not_found")

  echo "[SMOKE] ${ELAPSED}s  state=${STATE}"

  case "$STATE" in
    completed)
      PR=$(echo "$DETAIL" | python3 -c "
import sys, json
t = json.load(sys.stdin).get('task') or {}
print(t.get('pr_url', ''))
")
      echo "[SMOKE] ✅ 通过 task_id=${TASK_ID}"
      [ -n "$PR" ] && echo "[SMOKE] PR: $PR"
      exit 0
      ;;
    failed|cancelled)
      echo "[SMOKE] ❌ 失败 state=${STATE}，查看 AICodeAgent/data/logs/executor.log"
      exit 1
      ;;
    not_found)
      echo "[WARN] 任务未出现在列表，继续等待..."
      ;;
  esac

  sleep "$POLL_SEC"
done
