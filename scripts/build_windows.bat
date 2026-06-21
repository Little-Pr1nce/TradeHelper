@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0\.."

set PROJECT_DIR=%CD%
echo Project dir: %PROJECT_DIR%

if exist "venv\Scripts\python.exe" (
    set PYTHON=%PROJECT_DIR%\venv\Scripts\python.exe
    set PIP=%PROJECT_DIR%\venv\Scripts\pip.exe
    echo [0/5] Using existing venv: venv\
) else (
    echo [0/5] Creating Python venv...
    python -m venv venv
    if !ERRORLEVEL! NEQ 0 (
        echo ERROR: Please install Python 3.12+ and add python to PATH.
        pause
        exit /b 1
    )
    set PYTHON=%PROJECT_DIR%\venv\Scripts\python.exe
    set PIP=%PROJECT_DIR%\venv\Scripts\pip.exe
    echo venv created.
)

echo.
echo ============================================
echo   TradeHelper  Windows Build (all-in-one)
echo   Python: !PYTHON!
echo ============================================

echo.
echo [1/5] Installing dependencies (may take 5-10 min)...
!PIP! install --upgrade pip
if exist "requirements.txt" (
    !PIP! install -r requirements.txt
)
!PIP! install pyinstaller
if !ERRORLEVEL! NEQ 0 (
    echo ERROR: pip install failed. Check network connection.
    pause
    exit /b 1
)
echo Dependencies installed.

echo.
echo [2/5] Preparing FinBERT model (~300 MB download)...
if not exist "dist_data\finbert_model\config.json" (
    !PYTHON! scripts\prepare_model.py
    if !ERRORLEVEL! NEQ 0 (
        echo WARNING: FinBERT model download failed.
    )
) else (
    echo [FinBERT model ready, skipping download]
)

echo.
echo [3/5] Cleaning previous build artifacts...
if exist "dist\win" rmdir /s /q "dist\win"
if exist "build\win" rmdir /s /q "build\win"
echo Cleaned.

echo.
echo [4/5] PyInstaller packaging, please wait...
echo Bundle: FinBERT model + Flet icons + all Python dependencies

!PYTHON! -m PyInstaller tradehelper.spec ^
    --distpath "dist\win" ^
    --workpath "build\win" ^
    --noconfirm

if !ERRORLEVEL! NEQ 0 (
    echo ERROR: PyInstaller packaging failed. Check log above.
    pause
    exit /b 1
)

echo.
echo [5/5] Verifying output...
set OUTPUT_DIR=%PROJECT_DIR%\dist\win\TradeHelper
if exist "!OUTPUT_DIR!\TradeHelper.exe" (
    echo ========================================================================
    echo  Packaging successful!
    echo  Output: !OUTPUT_DIR!
    echo  Exe: TradeHelper.exe
    echo.
    echo  Usage: Copy the entire !OUTPUT_DIR! folder to users.
    echo  Users can run TradeHelper.exe directly without installing Python.
    echo ========================================================================
    echo.
    echo Cleaning build temp files...
    if exist "%PROJECT_DIR%\build\win" rmdir /s /q "%PROJECT_DIR%\build\win"
    echo Temp files cleaned.
    echo.
    echo Press any key to open output directory...
    pause >nul
    start "" "!OUTPUT_DIR!"
) else (
    echo ERROR: TradeHelper.exe not found. Packaging may have failed.
    pause
    exit /b 1
)

endlocal
