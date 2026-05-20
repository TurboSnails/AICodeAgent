#!/bin/bash
# figma_fetch.sh
# 用法: ./figma_fetch.sh <platform-site> <output_dir> [en_name]
#   platform-site: enName / 中文简称 / cnName（见 figma-tools/configs/platform-figma-list.json）
#   output_dir:    workspace/{task_id}/figma
#   en_name:       可选，已由 Python 解析出的 siteRes 目录名

set -euo pipefail

SITE_QUERY="${1:-}"
OUTPUT_DIR="${2:-}"
EN_NAME="${3:-}"
FIGMA_TOOLS="figma-tools"

if [ ! -d "$FIGMA_TOOLS" ]; then
    echo "[FIGMA] figma-tools 目录不存在"
    exit 1
fi

cd "$FIGMA_TOOLS"

if [ -n "$SITE_QUERY" ]; then
    echo "[FIGMA] 从 platform-figma-list 拉取站点: $SITE_QUERY"
    npm run download:site -- --platform-site "$SITE_QUERY" 2>&1 || echo "[FIGMA] download:site 失败"

    if [ -z "$EN_NAME" ] && command -v node >/dev/null 2>&1; then
        EN_NAME=$(node -e "
const { resolvePlatformSite } = require('./scripts/platform/resolvePlatformSite');
resolvePlatformSite(process.argv[1]).then(r => {
  if (r && r.enName) process.stdout.write(r.enName);
}).catch(() => process.exit(0));
" "$SITE_QUERY" 2>/dev/null || true)
    fi

    if [ -n "$EN_NAME" ]; then
        MULTI_BASE="output/.multi-site/$EN_NAME"
        echo "[FIGMA] enName=$EN_NAME → $MULTI_BASE"
        mkdir -p "$OUTPUT_DIR"
        if [ -f "$MULTI_BASE/colors/colors.json" ]; then
            cp "$MULTI_BASE/colors/colors.json" "$OUTPUT_DIR/"
        elif [ -f "output/colors.json" ]; then
            cp output/colors.json "$OUTPUT_DIR/"
        fi
        if [ -d "$MULTI_BASE/assets" ]; then
            rm -rf "$OUTPUT_DIR/assets"
            cp -R "$MULTI_BASE/assets" "$OUTPUT_DIR/"
        fi
        echo "[FIGMA] 已复制到 $OUTPUT_DIR"
        exit 0
    fi
fi

echo "[FIGMA] 拉取全局颜色..."
npm run colors:download 2>/dev/null || echo "[FIGMA] colors:download 失败或已是最新"

mkdir -p "$OUTPUT_DIR"
if [ -f "output/colors.json" ]; then
    cp output/colors.json "$OUTPUT_DIR/"
fi
if [ -d "output/assets" ]; then
    rm -rf "$OUTPUT_DIR/assets"
    cp -R output/assets "$OUTPUT_DIR/"
fi

echo "[FIGMA] 完成"
