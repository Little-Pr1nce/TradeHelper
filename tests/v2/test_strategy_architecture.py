import ast
from pathlib import Path


def test_sp26_strategy_layer_has_no_downstream_or_v1_imports():
    root = Path("strategies")
    forbidden = {
        "alpha", "backtest", "core", "services", "report",
        "risk", "execution", "portfolio", "learning", "research", "ui",
    }
    findings = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = getattr(node, "module", "") or ""
            if module.split(".")[0] in forbidden:
                findings.append(f"{path}:{node.lineno}:{module}")
            for alias in getattr(node, "names", ()):
                name = getattr(alias, "name", "")
                if name.split(".")[0] in forbidden:
                    findings.append(f"{path}:{node.lineno}:{name}")
    assert findings == []
