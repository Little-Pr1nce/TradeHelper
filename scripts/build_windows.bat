@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM TradeHelper Windows 打包脚本
REM 用法：双击或在 PyCharm 终端中运行
REM 输出：dist/win/TradeHelper/TradeHelper.exe
REM ============================================================

cd /d "%~dp0\.."

REM 自动检测 Python（优先 venv）
if exist "venv\Scripts\python.exe" (
    set PYTHON=venv\Scripts\python.exe
    set PIP=venv\Scripts\pip.exe
) else (
    set PYTHON=python
    set PIP=pip
)

echo =========================================
echo  TradeHelper Windows 打包
echo  Python: !PYTHON!
echo =========================================

REM 1. 环境检查
echo.
echo [1/4] 检查依赖...
!PYTHON! -c "import PyInstaller" 2>nul
if !ERRORLEVEL! NEQ 0 (
    echo 安装 PyInstaller...
    !PIP! install pyinstaller
)

REM 2. FinBERT 模型
echo.
echo [2/4] 准备 FinBERT 模型...
if not exist "dist_data\finbert_model\config.json" (
    !PYTHON! scripts\prepare_model.py
) else (
    echo 模型已就绪，跳过。
)

REM 3. 打包
echo.
echo [3/4] PyInstaller 打包（约 5 分钟）...
if exist "dist\win" rmdir /s /q "dist\win"
if exist "build\win" rmdir /s /q "build\win"
!PYTHON! -m PyInstaller tradehelper.spec --distpath dist/win --workpath build/win --noconfirm

REM 4. 验证
echo.
if exist "dist\win\TradeHelper\TradeHelper.exe" (
    echo ========================================
    echo 打包成功！
    echo 位置: dist\win\TradeHelper\TradeHelper.exe
    echo ========================================
) else (
    echo 打包失败！
    pause
    exit /b 1
)
pause
