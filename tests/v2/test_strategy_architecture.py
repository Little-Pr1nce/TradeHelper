from pathlib import Path


def test_sp26_strategy_layer_has_no_downstream_or_v1_imports():
    root = Path("tradehelper_v2/strategies")
    text = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert "from strategies" not in text and "tradehelper_v2.risk" not in text
    assert "tradehelper_v2.execution" not in text and "tradehelper_v2.portfolio" not in text
