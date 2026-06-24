"""
数据源可用性检查测试。
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from data.stock_fetcher import (
    TickFlowFetcher,
    _parse_nasdaq_timestamp,
    check_tickflow_available,
    check_extended_quote_available,
    fetch_cached_prices,
)
from data.models import StockInfo, PriceData
from alpha.fundamental import _extract_financials_from_finnhub


class DummyFetcher:
    has_realtime = True

    def fetch_stock_info(self, code):
        return StockInfo(code=code, name=code, market="US")

    def fetch_price_history(self, code, start, end):
        return [PriceData(code=code, date="2024-01-02", open=1, high=2, low=1, close=2, volume=100)]

    def fetch_quote(self, code):
        return {"latest": 2.0}


class RecordingFetcher:
    def __init__(self):
        self.calls = []

    def fetch_price_history(self, code, start, end):
        self.calls.append((start, end))
        if start == "2026-01-01":
            return [PriceData(code=code, date="2026-01-01", open=1, high=2, low=1, close=2, volume=100)]
        if end == "2026-01-15":
            return [PriceData(code=code, date="2026-01-15", open=3, high=4, low=3, close=4, volume=100)]
        return []


class MemoryPriceDB:
    def __init__(self):
        self.prices = [
            PriceData(code="AAPL", date="2026-01-10", open=2, high=3, low=2, close=3, volume=100),
            PriceData(code="AAPL", date="2026-01-12", open=2, high=3, low=2, close=3, volume=100),
        ]

    def get_prices(self, code, start_date="", end_date=""):
        return sorted(
            [p for p in self.prices if p.code == code and p.date >= start_date and p.date <= end_date],
            key=lambda p: p.date,
        )

    def insert_prices(self, prices):
        self.prices.extend(prices)


class FakeTickFlowKlines:
    def __init__(self):
        self.kwargs = None

    def get(self, symbol, **kwargs):
        self.kwargs = kwargs
        return pd.DataFrame([
            {
                "trade_date": "2024-01-02",
                "open": 10,
                "high": 12,
                "low": 9,
                "close": 11,
                "volume": 100,
            },
            {
                "trade_date": "2024-02-01",
                "open": 20,
                "high": 22,
                "low": 19,
                "close": 21,
                "volume": 200,
            },
        ])


class FakeTickFlow:
    def __init__(self):
        self.klines = FakeTickFlowKlines()


def test_tickflow_availability_ok():
    with patch("data.stock_fetcher.get_stock_fetcher", return_value=DummyFetcher()):
        result = check_tickflow_available("US", "AAPL")
    assert result["ok"] is True
    assert result["history_ok"] is True
    assert result["realtime_ok"] is True


def test_extended_quote_availability_ok():
    with patch("data.stock_fetcher.fetch_us_extended_quote", return_value={"latest": 100.0}):
        result = check_extended_quote_available("AAPL")
    assert result["ok"] is True
    assert result["extended_quote_ok"] is True


def test_fetch_cached_prices_backfills_head_and_tail():
    fetcher = RecordingFetcher()
    db = MemoryPriceDB()
    with patch("data.stock_fetcher.get_stock_fetcher", return_value=fetcher):
        df = fetch_cached_prices("AAPL", "US", "2026-01-01", "2026-01-15", db=db)

    assert ("2026-01-01", "2026-01-09") in fetcher.calls
    assert ("2026-01-13", "2026-01-15") in fetcher.calls
    assert df["date"].min().strftime("%Y-%m-%d") == "2026-01-01"
    assert df["date"].max().strftime("%Y-%m-%d") == "2026-01-15"


def test_tickflow_fetch_price_history_uses_date_window():
    fake_tf = FakeTickFlow()
    fetcher = TickFlowFetcher.__new__(TickFlowFetcher)
    fetcher._tf = fake_tf
    fetcher._api_key = "test"
    fetcher._quote_cache = {}

    prices = fetcher.fetch_price_history("AAPL", "2024-01-01", "2024-01-31")

    assert fake_tf.klines.kwargs["start_time"] > 0
    assert fake_tf.klines.kwargs["end_time"] > fake_tf.klines.kwargs["start_time"]
    assert [p.date for p in prices] == ["2024-01-02"]


def test_finnhub_debt_ratio_uses_normalized_metric():
    result = _extract_financials_from_finnhub({
        "metric": {
            "roeRfy": 151.91,
            "grossMarginAnnual": 46.91,
            "totalDebt/totalEquityAnnual": 1.3547,
            "epsGrowthTTMYoy": 29.01,
            "revenueGrowthTTMYoy": 12.76,
        }
    })

    assert abs(result["roe"] - 1.5191) < 1e-9
    assert abs(result["gross_margin"] - 0.4691) < 1e-9
    assert result["debt_ratio"] == 1.3547


def test_nasdaq_timestamp_is_parsed_as_eastern_time():
    ts = _parse_nasdaq_timestamp("Jun 22, 2026 4:19 AM ET")
    assert ts == 1782116340000


if __name__ == "__main__":
    test_tickflow_availability_ok()
    test_extended_quote_availability_ok()
    test_fetch_cached_prices_backfills_head_and_tail()
    test_tickflow_fetch_price_history_uses_date_window()
    test_finnhub_debt_ratio_uses_normalized_metric()
    test_nasdaq_timestamp_is_parsed_as_eastern_time()
    print("6/6 passed")
