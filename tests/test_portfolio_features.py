"""
组合与防抖相关测试。
"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.database import Database
from data.models import AnalysisReport, Holding, WatchItem, AccountBalance
from services.signal_stabilizer import SignalStabilizer


def fresh_db():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    return Database.init(os.path.join(tmpdir, "test.db"))


def test_filter_reports_by_market_mode_period_rating():
    db = fresh_db()
    db.insert_report(AnalysisReport(
        code="AAPL", name="Apple", market="US", backtest_period="1y",
        create_time=datetime.now().isoformat(), content="x", rating=5, mode="intraday",
    ))
    db.insert_report(AnalysisReport(
        code="600519", name="茅台", market="A", backtest_period="6m",
        create_time=datetime.now().isoformat(), content="y", rating=3, mode="eod",
    ))
    reports = db.filter_reports(market="US", mode="intraday", period="1y", min_rating=4)
    assert len(reports) == 1
    assert reports[0].code == "AAPL"


def test_signal_stabilizer_reuses_small_move():
    db = fresh_db()
    db.insert_report(AnalysisReport(
        code="AAPL", name="Apple", market="US", backtest_period="1y",
        create_time=datetime.now().isoformat(),
        content="| 最新价 | **100.00**（+0.10%） |",
        mode="intraday",
    ))
    decision = SignalStabilizer(tolerance_pct=0.003, min_interval_minutes=10).should_emit("AAPL", 100.1)
    assert decision.should_emit is False
    assert decision.previous_report is not None


def test_holdings_watchlist_balance_crud():
    db = fresh_db()
    # 测试持仓
    db.upsert_holding(Holding(
        code="AAPL", name="Apple", market="US", shares=100, cost_price=150.0,
    ))
    holdings = db.list_holdings("US")
    assert len(holdings) == 1
    assert holdings[0].code == "AAPL"

    # 测试关注
    db.upsert_watch_item(WatchItem(code="NVDA", name="NVIDIA", market="US"))
    watchlist = db.list_watchlist("US")
    assert len(watchlist) == 1
    assert watchlist[0].code == "NVDA"

    # 测试余额
    db.save_balance(AccountBalance(us_balance=50000.0, a_balance=100000.0))
    b = db.get_balance()
    assert b.us_balance == 50000.0
    assert b.a_balance == 100000.0


if __name__ == "__main__":
    tests = [
        test_filter_reports_by_market_mode_period_rating,
        test_signal_stabilizer_reuses_small_move,
        test_holdings_watchlist_balance_crud,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
