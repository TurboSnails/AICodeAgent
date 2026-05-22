#!/bin/bash
# 安装 TencentDB Agent Memory standalone Gateway 到 ~/.memory-tencentdb
set -euo pipefail

MEMORY_TENCENTDB_ROOT="${MEMORY_TENCENTDB_ROOT:-$HOME/.memory-tencentdb}"
TDAI_INSTALL_DIR="${TDAI_INSTALL_DIR:-$MEMORY_TENCENTDB_ROOT/tdai-memory-openclaw-plugin}"
TDAI_DATA_DIR="${TDAI_DATA_DIR:-$MEMORY_TENCENTDB_ROOT/memory-tdai}"

command -v node >/dev/null || { echo "需要 Node >= 22"; exit 1; }
command -v npm >/dev/null || { echo "需要 npm"; exit 1; }

echo "[1/4] npm 下载 @tencentdb-agent-memory/memory-tencentdb ..."
TEMP=$(mktemp -d)
cd "$TEMP"
npm init -y --silent >/dev/null 2>&1
npm install @tencentdb-agent-memory/memory-tencentdb@latest --omit=dev
PACK="$TEMP/node_modules/@tencentdb-agent-memory/memory-tencentdb"

echo "[2/4] 安装到 $TDAI_INSTALL_DIR ..."
rm -rf "$TDAI_INSTALL_DIR"
mkdir -p "$(dirname "$TDAI_INSTALL_DIR")"
cp -R "$PACK" "$TDAI_INSTALL_DIR"
cd "$TDAI_INSTALL_DIR" && npm install --omit=dev
rm -rf "$TEMP"

mkdir -p "$HOME/.local/bin"
ln -sf "$TDAI_INSTALL_DIR/scripts/memory-tencentdb-ctl.sh" "$HOME/.local/bin/memory-tencentdb-ctl"
chmod +x "$TDAI_INSTALL_DIR/scripts/memory-tencentdb-ctl.sh"

echo "[3/4] 配置 LLM（使用当前 shell 的 ANTHROPIC_*）..."
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "请 export ANTHROPIC_API_KEY 后执行:"
  echo "  memory-tencentdb-ctl config llm --api-key ... --base-url ... --model ..."
else
  BASE="${ANTHROPIC_BASE_URL:-https://api.openai.com/v1}"
  BASE="${BASE%/}"
  MODEL="${MEMORY_TENCENTDB_LLM_MODEL:-kimi-k2.5}"
  memory-tencentdb-ctl config llm \
    --api-key "$ANTHROPIC_API_KEY" \
    --base-url "$BASE" \
    --model "$MODEL" \
    --restart
fi

echo "[4/4] 启动 Gateway ..."
memory-tencentdb-ctl start
memory-tencentdb-ctl health

echo ""
echo "完成。在 AICodeAgent/config/local.yaml 设 memory.tencentdb.enabled: true"
echo "或: export MEMORY_TENCENTDB_ENABLED=true"
