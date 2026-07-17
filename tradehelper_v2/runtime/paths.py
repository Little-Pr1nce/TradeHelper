"""桌面包资源路径和用户可写工作目录分离。"""
from __future__ import annotations
import os
from pathlib import Path
import sys

def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))

def model_path(configured: str = "") -> Path | None:
    if configured:
        value=Path(configured).expanduser()
        if value.exists(): return value
    bundled=bundle_root()/"dist_data"/"finbert_model"
    return bundled if bundled.exists() else None

def ensure_work_dir(path: Path | str) -> Path:
    value=Path(path).expanduser(); value.mkdir(parents=True,exist_ok=True); return value

def default_source_path(work_dir: Path) -> Path:
    candidates=(
        work_dir/"tradehelper.db",
        Path.home()/"TradeHelperData"/"tradehelper.db",
        work_dir.parent/"tradehelper.db",
        Path.cwd()/"tradehelper.db",
        Path.home()/"TradeHelper"/"tradehelper.db",
    )
    return next((item for item in candidates if item.is_file()), candidates[0])

def default_legacy_config_path() -> Path:
    candidates=(
        Path.home()/"Library"/"Application Support"/"TradeHelper"/"config.json",
        Path(os.environ.get("APPDATA", ""))/"TradeHelper"/"config.json" if os.environ.get("APPDATA") else None,
        Path.home()/".tradehelper"/"config.json",
    )
    existing=next((item for item in candidates if item is not None and item.is_file()),None)
    return existing or candidates[0]
