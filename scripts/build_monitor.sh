#!/bin/bash
# build_monitor.sh
# 用法: ./build_monitor.sh [testDebugUnitTest|assembleDebug|lintDebug]

TASK=${1:-assembleDebug}
TIMEOUT_SEC=900
LOG_FILE="/tmp/headless_agent_build_$(date +%s).log"

echo "[BUILD MONITOR] ./gradlew app:${TASK}"
timeout ${TIMEOUT_SEC} ./gradlew "app:${TASK}" --console=plain > "${LOG_FILE}" 2>&1 &
PID=$!
tail -f "${LOG_FILE}" &
TAIL_PID=$!
wait $PID
EXIT_CODE=$?
kill $TAIL_PID 2>/dev/null

if [ $EXIT_CODE -eq 124 ]; then
    echo "[BUILD MONITOR] 超时 (${TIMEOUT_SEC}s)"
    exit 124
elif [ $EXIT_CODE -ne 0 ]; then
    echo "[BUILD MONITOR] 失败 (exit $EXIT_CODE)"
    grep -E "(error:|exception|failed|compilation error|unresolved reference)" "${LOG_FILE}" | tail -20
    exit $EXIT_CODE
else
    echo "[BUILD MONITOR] 成功"
    exit 0
fi
