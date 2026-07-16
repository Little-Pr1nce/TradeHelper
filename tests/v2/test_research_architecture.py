"""LL48：研究层只能单向依赖冻结合同。"""
from pathlib import Path

def test_ll48_research_core_has_no_v1_ui_report_or_network_imports():
    root=Path(__file__).resolve().parents[2]/"tradehelper_v2"/"research"
    text="\n".join(path.read_text() for path in root.glob("*.py") if path.name != "client.py")
    for forbidden in ("tradehelper_v1", "report.", "import flet", "import requests", "from requests", "import sqlite3", "services.", "core."):
        assert forbidden not in text
