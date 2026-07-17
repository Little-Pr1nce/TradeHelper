"""UX57：presentation/UI 不能回流业务或网络。"""
from pathlib import Path
def test_ux57_presentation_has_no_v1_network_or_sql_imports():
 text="\n".join(path.read_text() for root in ("tradehelper_v2/presentation","tradehelper_v2/ui") for path in Path(root).rglob("*.py"))
 for forbidden in ("tradehelper_v1","import requests","from requests","import sqlite3","data.providers"): assert forbidden not in text
