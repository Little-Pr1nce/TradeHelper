# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 — TradeHelper 跨平台可执行文件。

用法：
    pyinstaller tradehelper.spec

平台：
    macOS  → 输出 TradeHelper.app
    Windows → 输出 TradeHelper.exe + 依赖文件夹
"""

import sys
from pathlib import Path

# ── 数据文件：FinBERT 模型 + Flet 图标资源 ──
datas = []
finbert_dir = Path("dist_data/finbert_model")
if finbert_dir.exists():
    for f in finbert_dir.rglob("*"):
        if f.is_file():
            datas.append((str(f), "dist_data/finbert_model"))

# Flet 图标 JSON 数据（PyInstaller 不会自动收集到正确位置）
import flet
_flet_pkg = Path(flet.__file__).parent
for _icon_file in ["controls/material/icons.json", "controls/cupertino/cupertino_icons.json"]:
    _src = _flet_pkg / _icon_file
    if _src.exists():
        # 目标目录不带文件名，仅目录路径
        datas.append((str(_src), str((_flet_pkg / _icon_file).parent.relative_to(_flet_pkg.parent))))

# ── 包元数据（避免 "no package metadata was found" 错误） ──
# tickflow/baostock 用 importlib.metadata 查版本，PyInstaller 需显式打包 METADATA
from PyInstaller.utils.hooks import copy_metadata

datas += copy_metadata('tickflow')
datas += copy_metadata('baostock')
datas += copy_metadata('httpx')

# ── 隐藏导入（transformers/torch 等大型库需显式声明） ──
hiddenimports = [
    "transformers",
    "transformers.pipelines",
    "transformers.pipelines.text_classification",
    "transformers.pipelines.base",
    "transformers.models.distilbert",
    "transformers.models.distilbert.modeling_distilbert",
    "transformers.models.roberta",
    "transformers.models.roberta.modeling_roberta",
    "tokenizers",
    "tokenizers.decoders",
    "torch",
    "torchvision",
    "huggingface_hub",
    "flet",
    "flet_core",
    "akshare",
    "mplfinance",
    "matplotlib",
    "matplotlib.backends.backend_agg",
    "openai",
    "reportlab",
    "reportlab.graphics",
    "PIL",
    "PIL._imagingtk",
    "ta",
    "ta.trend",
    "ta.momentum",
    "ta.volatility",
    "ta.volume",
    "numpy",
    "pandas",
    "tickflow",
    "httpx",
    "yfinance",
    "scipy",
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "packaging",
    "dateutil",
    "tqdm",
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'test',
        'xmlrpc',
        'pydoc',
        'distutils',
        'setuptools',
    ],
)

pyz = PYZ(a.pure)

# ── 根据平台选择输出格式 ──
if sys.platform == 'darwin':
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='TradeHelper',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    app = BUNDLE(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        name='TradeHelper.app',
        icon=None,
        bundle_identifier='com.tradehelper.app',
        info_plist={
            'CFBundleName': 'TradeHelper',
            'CFBundleDisplayName': 'TradeHelper - 股票分析助手',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
        },
    )

elif sys.platform == 'win32':
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='TradeHelper',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='TradeHelper',
    )
