#!/bin/bash
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
echo "[1/4] 检查依赖..."
if ! $PYTHON -c "import flet, transformers, tickflow, yfinance" 2>/dev/null; then
    echo "安装/补齐项目依赖..."
    $PIP install -r requirements.txt
fi
if ! $PYTHON -c "import PyInstaller" 2>/dev/null; then
    echo "安装 PyInstaller..."
    $PIP install pyinstaller
fi

# ── 2. FinBERT 模型 ──
echo ""
echo "[2/4] 准备 FinBERT 模型..."
if [ ! -f "dist_data/finbert_model/config.json" ]; then
    $PYTHON scripts/prepare_model.py
else
    echo "模型已就绪，跳过。"
fi

# ── 3. 清理 + 打包 ──
echo ""
echo "[3/4] PyInstaller 打包（约 5 分钟）..."
rm -rf dist/mac build/mac 2>/dev/null || true
$PYTHON -m PyInstaller tradehelper.spec --distpath dist/mac --workpath build/mac --noconfirm

# ── 4. 验证 ──
echo ""
if [ -d "dist/mac/TradeHelper.app" ]; then
    APP_SIZE=$(du -sh dist/mac/TradeHelper.app | cut -f1)
    echo "✓ 打包成功！ dist/mac/TradeHelper.app ($APP_SIZE)"
else
    echo "✗ 打包失败！"
    exit 1
fi
