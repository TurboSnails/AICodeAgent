#!/bin/bash
# notify.sh
# 用法: ./notify.sh "标题" "内容"

TITLE=$1
BODY=$2
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-""}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID:-""}

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    echo "[NOTIFY SKIP] 未配置 Telegram"
    exit 0
fi

MESSAGE="<b>${TITLE}</b>%0A${BODY}"
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    -d "text=${MESSAGE}" \
    -d "parse_mode=HTML" \
    -d "disable_web_page_preview=true"
echo "[NOTIFY] 已发送"
