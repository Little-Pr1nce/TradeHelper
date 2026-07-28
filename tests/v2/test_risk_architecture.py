from pathlib import Path


def test_rk39_risk_layer_has_no_downstream_or_v1_imports():
    text = "\n".join(path.read_text() for path in Path("risk").rglob("*.py"))
    assert "from core" not in text and "execution" not in text
    assert "portfolio" not in text and "learning" not in text
