# -*- mode: python ; coding: utf-8 -*-
"""TradeHelper 2.0 cross-platform PyInstaller specification.

入口只有 V2 ``main.py``；旧业务包显式排除，迁移 reader 使用自身只读合同。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

datas = []
icon = Path("assets/tradehelper.ico")
if icon.exists():
    datas.append((str(icon), "assets"))
model_dir = Path("dist_data/finbert_model")
if model_dir.exists():
        datas.extend((str(item), str(Path("dist_data/finbert_model") / item.relative_to(model_dir).parent)) for item in model_dir.rglob("*") if item.is_file())
manifest = Path("dist_data/release-manifest.json")
if manifest.exists():
    datas.append((str(manifest), "dist_data"))
lock_file = Path("requirements-lock.txt")
if lock_file.exists():
    datas.append((str(lock_file), "."))
try:
    import flet
    flet_root = Path(flet.__file__).parent
    for relative in ("controls/material/icons.json", "controls/cupertino/cupertino_icons.json"):
        source = flet_root / relative
        if source.exists():
            datas.append((str(source), str(source.parent.relative_to(flet_root.parent))))
except Exception:
    pass
datas += collect_data_files("akshare")
for package in ("tickflow", "baostock", "httpx", "h11", "httpcore", "anyio", "setuptools", "jsonpath", "markdown-it-py"):
    try:
        datas += copy_metadata(package)
    except Exception:
        pass

hiddenimports = [
    "tradehelper_v2", "tradehelper_v2.runtime", "tradehelper_v2.migration",
    "flet", "akshare", "tickflow", "baostock", "yfinance", "requests",
    "transformers", "transformers.pipelines", "transformers.pipelines.text_classification",
    "tokenizers", "torch", "huggingface_hub", "openai", "reportlab",
    "markdown_it", "numpy", "pandas", "scipy", "sklearn", "exchange_calendars", "mplfinance",
    "scipy._external.array_api_compat.numpy.fft",
]
hiddenimports += collect_submodules("jaraco")
a = Analysis(["main.py"], pathex=[], binaries=[], datas=datas, hiddenimports=hiddenimports,
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[
                 "alpha", "backtest", "config", "core", "data", "indicators", "report",
                 "services", "strategies", "ui", "run_backtest", "tkinter", "test", "tests", "pytest", "xmlrpc",
             ])
pyz = PYZ(a.pure)
common = dict(debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
              console=False, disable_windowed_traceback=False, argv_emulation=False,
              target_arch=None, codesign_identity=None, entitlements_file=None)
if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TradeHelper", **common)
    app = BUNDLE(exe, a.binaries, a.zipfiles, a.datas, name="TradeHelper.app",
                 bundle_identifier="com.tradehelper.app", info_plist={
                     "CFBundleName": "TradeHelper", "CFBundleDisplayName": "TradeHelper 2.0",
                     "CFBundleShortVersionString": "2.0.0", "NSHighResolutionCapable": True,
                 })
elif sys.platform == "win32":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TradeHelper",
              icon="assets\\tradehelper.ico" if icon.exists() else None, **common)
    coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=True, upx_exclude=[], name="TradeHelper")
