#!/bin/bash
set -euo pipefail
# ============================================================
# TradeHelper macOS 打包脚本
#
# 用法（PyCharm 中直接右键 Run）：
#   或在终端: bash scripts/build_macos.sh
#
# 输出：dist/mac/TradeHelper.app
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# 自动检测 Python：优先用项目 venv，其次尊重 PYENV_VERSION，最后尝试 pyenv dev-3.12
if [ -f "venv/bin/python3" ]; then
    PYTHON="venv/bin/python3"
    PIP="venv/bin/pip3"
elif [ -f "venv/bin/python" ]; then
    PYTHON="venv/bin/python"
    PIP="venv/bin/pip"
elif command -v pyenv >/dev/null 2>&1 && [ -n "${PYENV_VERSION:-}" ]; then
    PYTHON="pyenv exec python"
    PIP="pyenv exec pip"
elif command -v pyenv >/dev/null 2>&1 && pyenv versions --bare | grep -qx "dev-3.12"; then
    export PYENV_VERSION="dev-3.12"
    PYTHON="pyenv exec python"
    PIP="pyenv exec pip"
else
    PYTHON="python3"
    PIP="pip3"
fi

echo "========================================="
echo " TradeHelper macOS 打包"
echo " Python: $PYTHON ($($PYTHON --version))"
echo "========================================="

# ── 1. 环境检查 ──
echo ""
echo "[1/5] 检查依赖..."
if ! $PYTHON -c "import flet, transformers, tickflow, yfinance" 2>/dev/null; then
    echo "安装/补齐项目依赖..."
    $PIP install -r requirements-runtime.txt
fi
if ! $PYTHON -c "import PyInstaller" 2>/dev/null; then
    echo "安装 PyInstaller..."
    $PIP install -r requirements-dev.txt
fi

# ── 2. FinBERT 模型 ──
echo ""
echo "[2/5] 准备 FinBERT 模型..."
if [ ! -f "dist_data/finbert_model/config.json" ]; then
    $PYTHON scripts/prepare_model.py
else
    echo "模型已就绪，跳过。"
fi
$PYTHON scripts/write_release_manifest.py

# ── 3. 清理 + 打包 ──
echo ""
echo "[3/5] PyInstaller 打包（约 5 分钟）..."
rm -rf dist/mac build/mac 2>/dev/null || true
$PYTHON -m PyInstaller tradehelper.spec --distpath dist/mac --workpath build/mac --noconfirm

# ── 4. 验证 ──
echo ""
if [ -d "dist/mac/TradeHelper.app" ]; then
    APP_SIZE=$(du -sh dist/mac/TradeHelper.app | cut -f1)
    echo "✓ 打包成功！ dist/mac/TradeHelper.app ($APP_SIZE)"
    echo "[5/5] 运行临时 HOME 的离线 runtime smoke..."
    # PyInstaller 刚退出时给系统片刻回收峰值内存，再加载包内 FinBERT。
    sleep 3
    export TRADEHELPER_SMOKE_TEST=1
    export TRADEHELPER_REQUIRE_FINBERT=1
    export TRADEHELPER_REQUIRE_MANIFEST=1
    ORIGINAL_HOME="$HOME"
    SMOKE_HOME="$(mktemp -d)"
    export HOME="$SMOKE_HOME"
    set +e
    "dist/mac/TradeHelper.app/Contents/MacOS/TradeHelper"
    SMOKE_EXIT=$?
    set -e
    rm -rf "$SMOKE_HOME"
    export HOME="$ORIGINAL_HOME"
    unset TRADEHELPER_SMOKE_TEST
    unset TRADEHELPER_REQUIRE_FINBERT
    unset TRADEHELPER_REQUIRE_MANIFEST
    if [ $SMOKE_EXIT -ne 0 ]; then
        echo "✗ runtime smoke 失败（$SMOKE_EXIT）"
        exit $SMOKE_EXIT
    fi
    echo "✓ runtime smoke 通过"
else
    echo "✗ 打包失败！"
    exit 1
fi
