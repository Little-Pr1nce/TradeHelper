#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PORT="${TRADEHELPER_WEB_PORT:-8765}"

if [ -x "$PROJECT_DIR/venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python"
else
    PYTHON="${PYTHON:-python3}"
fi

export FLET_FORCE_WEB_SERVER=true
export FLET_SERVER_IP=127.0.0.1
export FLET_SERVER_PORT="$PORT"

echo "TradeHelper Web 预览已启动：http://localhost:$PORT"
echo "该命令不会自动打开系统浏览器，可避免 Safari 的仅限 HTTPS 导览失败提示。"
exec "$PYTHON" "$PROJECT_DIR/main.py"
