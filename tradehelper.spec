# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 鎵撳寘閰嶇疆 鈥?TradeHelper 璺ㄥ钩鍙板彲鎵ц鏂囦欢銆?

鐢ㄦ硶锛?
    pyinstaller tradehelper.spec

骞冲彴锛?
    macOS  鈫?杈撳嚭 TradeHelper.app
    Windows 鈫?杈撳嚭 TradeHelper.exe + 渚濊禆鏂囦欢澶?
"""

import sys
from pathlib import Path

# 鈹€鈹€ 鏁版嵁鏂囦欢锛欶inBERT 妯″瀷 + Flet 鍥炬爣璧勬簮 鈹€鈹€
datas = []

# Windows 搴旂敤鍥炬爣
_icon_path = Path("assets/tradehelper.ico")
if _icon_path.exists():
    datas.append((str(_icon_path), "assets"))
finbert_dir = Path("dist_data/finbert_model")
if finbert_dir.exists():
    for f in finbert_dir.rglob("*"):
        if f.is_file():
            datas.append((str(f), "dist_data/finbert_model"))

# Flet 鍥炬爣 JSON 鏁版嵁锛圥yInstaller 涓嶄細鑷姩鏀堕泦鍒版纭綅缃級
import flet
_flet_pkg = Path(flet.__file__).parent
for _icon_file in ["controls/material/icons.json", "controls/cupertino/cupertino_icons.json"]:
    _src = _flet_pkg / _icon_file
    if _src.exists():
        # 鐩爣鐩綍涓嶅甫鏂囦欢鍚嶏紝浠呯洰褰曡矾寰?
        datas.append((str(_src), str((_flet_pkg / _icon_file).parent.relative_to(_flet_pkg.parent))))

# 鈹€鈹€ 鍖呭厓鏁版嵁锛堥伩鍏?"no package metadata was found" 閿欒锛?鈹€鈹€
# tickflow/baostock 鐢?importlib.metadata 鏌ョ増鏈紝PyInstaller 闇€鏄惧紡鎵撳寘 METADATA
from PyInstaller.utils.hooks import copy_metadata

# 閫愪釜 try锛岄伩鍏嶆煇涓€涓寘娌?metadata 灏卞叏灞€鎶ラ敊
for _pkg in ('tickflow', 'baostock', 'httpx', 'h11', 'httpcore', 'anyio'):
    try:
        datas += copy_metadata(_pkg)
    except Exception:
        pass  # 閮ㄥ垎鍖?Windows 涓婄粨鏋勪笉鍚岋紝蹇界暐鍗冲彲

# 鈹€鈹€ 闅愯棌瀵煎叆锛坱ransformers/torch 绛夊ぇ鍨嬪簱闇€鏄惧紡澹版槑锛?鈹€鈹€
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
    "akshare",
    "mplfinance",
    "matplotlib",
    "matplotlib.backends.backend_agg",
    "openai",
    "reportlab",
    "reportlab.graphics",
    "PIL",
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
    "sentencepiece",          # transformers 鍙兘鐢ㄥ埌
    "safetensors",            # 妯″瀷鍔犺浇
    "json",                   # tickflow 鐢?
    "httpcore",               # httpx 渚濊禆
    "h11",                    # httpcore 渚濊禆
    "anyio",                  # httpx 渚濊禆
    "asyncio",                # httpx 渚濊禆
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
        'setuptools',
    ],
)

pyz = PYZ(a.pure)

# 鈹€鈹€ 鏍规嵁骞冲彴閫夋嫨杈撳嚭鏍煎紡 鈹€鈹€
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
            'CFBundleDisplayName': 'TradeHelper - 鑲＄エ鍒嗘瀽鍔╂墜',
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
        icon='assets\\tradehelper.ico',
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
