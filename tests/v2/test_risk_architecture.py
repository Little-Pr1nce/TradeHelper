from pathlib import Path


def test_rk39_risk_layer_has_no_downstream_or_v1_imports():
    text = "\n".join(path.read_text() for path in Path("tradehelper_v2/risk").rglob("*.py"))
    assert "from core" not in text and "tradehelper_v2.execution" not in text
    assert "tradehelper_v2.portfolio" not in text and "tradehelper_v2.learning" not in text
