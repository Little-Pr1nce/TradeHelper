from __future__ import annotations

import ast
from pathlib import Path


FORBIDDEN = {"core", "services", "strategies", "backtest", "report", "ui", "alpha", "indicators", "utils", "data"}
ROOT = Path(__file__).parents[2]


def _illegal_imports(path: Path) -> list[str]:
    if path.name == "compatibility.py":
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        module = getattr(node, "module", None)
        if isinstance(module, str) and module.split(".")[0] in FORBIDDEN:
            found.append(f"{path}:{node.lineno}:{module}")
        for alias in getattr(node, "names", ()):
            name = getattr(alias, "name", "")
            if name.split(".")[0] in FORBIDDEN:
                found.append(f"{path}:{node.lineno}:{name}")
    return found


def test_g00_architecture_rejects_illegal_v1_import(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("from core.pipeline import run_pipeline\n", encoding="utf-8")
    assert _illegal_imports(candidate) == [f"{candidate}:1:core.pipeline"]


def test_v2_main_path_has_no_v1_business_imports() -> None:
    findings = [finding for path in (ROOT / "tradehelper_v2").rglob("*.py") for finding in _illegal_imports(path)]
    assert findings == []
