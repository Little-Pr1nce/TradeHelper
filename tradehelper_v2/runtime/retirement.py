"""V2-12 最终退出检查；只读扫描，不删除用户文件。"""
from __future__ import annotations
from pathlib import Path
from typing import Iterable

V1_RUNTIME_DIRS=("alpha","backtest","config","core","data","indicators","report","services","strategies","ui","run_backtest.py")
def production_imports_are_v2(main_path: Path | str="main.py") -> bool:
    text=Path(main_path).read_text(encoding="utf-8-sig")
    return "from tradehelper_v2" in text and not any(token in text for token in ("alpha", "core", "services", "strategies"))
def bundle_excludes_v1(spec_path: Path | str="tradehelper.spec") -> bool:
    text=Path(spec_path).read_text(encoding="utf-8-sig")
    return all((f"'{name}'" in text or f'"{name}"' in text) for name in V1_RUNTIME_DIRS if name != "run_backtest.py")
