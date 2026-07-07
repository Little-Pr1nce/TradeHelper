"""
数据源可用性检查测试。
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from data.stock_fetcher import (
    TickFlowFetcher,
    _filter_prices_for_cache,
    _parse_nasdaq_timestamp,
    check_tickflow_available,
    check_extended_quote_available,
    fetch_cached_prices,
)
from data.models import StockInfo, PriceData
from alpha.fundamental import _extract_financials_from_finnhub
from core.data_quality import evaluate_data_quality
from core.pipeline import run_pipeline
from data.database import Database
from services.intraday_data_service import normalize_intraday_frame
from utils.market import _A_DIRECTORY_CACHE, search_a_stock, search_a_stock_fallback


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
        if start == "2026-01-02":
            return [PriceData(code=code, date="2026-01-02", open=1, high=2, low=1, close=2, volume=100)]
        if end == "2026-01-15":
            return [PriceData(code=code, date="2026-01-15", open=3, high=4, low=3, close=4, volume=100)]
        return []


class MemoryPriceDB:
    def __init__(self):
        self.prices = [
            PriceData(code="AAPL", date="2026-01-09", open=2, high=3, low=2, close=3, volume=100),
            PriceData(code="AAPL", date="2026-01-12", open=2, high=3, low=2, close=3, volume=100),
        ]

    def get_prices(self, code, start_date="", end_date=""):
        return sorted(
            [p for p in self.prices if p.code == code and p.date >= start_date and p.date <= end_date],
            key=lambda p: p.date,
        )

    def insert_prices(self, prices):
        self.prices.extend(prices)


class DirtyTailDB:
    def __init__(self):
        self.prices = [
            PriceData(code="MU", date="2026-01-02", open=100, high=102, low=98, close=100, volume=100),
            PriceData(code="MU", date="2026-01-05", open=101, high=104, low=99, close=103, volume=100),
            PriceData(code="MU", date="2026-01-06", open=1000, high=1010, low=990, close=1005, volume=100),
        ]

    def get_prices(self, code, start_date="", end_date=""):
        return sorted(
            [p for p in self.prices if p.code == code and p.date >= start_date and p.date <= end_date],
            key=lambda p: p.date,
        )

    def insert_prices(self, prices):
        self.prices.extend(prices)


class IpoPriceDB:
    def __init__(self):
        self.prices = [
            PriceData(code="SPCX", date="2026-06-10", open=25, high=26, low=24, close=25.5, volume=100),
        ]

    def get_prices(self, code, start_date="", end_date=""):
        return sorted(
            [p for p in self.prices if p.code == code and p.date >= start_date and p.date <= end_date],
            key=lambda p: p.date,
        )

    def insert_prices(self, prices):
        self.prices.extend(prices)


class IpoFetcher:
    def __init__(self):
        self.calls = []

    def fetch_price_history(self, code, start, end):
        self.calls.append((start, end))
        return [
            PriceData(code=code, date="2026-06-11", open=145, high=160, low=140, close=155, volume=1000),
            PriceData(code=code, date="2026-06-12", open=155, high=162, low=150, close=158, volume=1200),
            PriceData(code=code, date="2026-06-15", open=158, high=165, low=154, close=161, volume=900),
        ]


class CleanRebuildFetcher:
    def fetch_price_history(self, code, start, end):
        return [
            PriceData(code=code, date="2026-01-02", open=100, high=102, low=99, close=101, volume=1000),
            PriceData(code=code, date="2026-01-05", open=101, high=103, low=100, close=102, volume=1200),
        ]


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


class FakeTickFlowQuotes:
    def __init__(self):
        self.calls = []

    def get(self, symbols):
        self.calls.append(symbols)
        return [
            {
                "symbol": symbol,
                "last_price": 100 + index,
                "prev_close": 99 + index,
                "open": 99,
                "high": 102,
                "low": 98,
                "volume": 1000,
                "timestamp": 1_800_000_000_000,
            }
            for index, symbol in enumerate(symbols)
        ]


def test_tickflow_fetch_quotes_batches_symbols_and_reuses_cache():
    fetcher = TickFlowFetcher.__new__(TickFlowFetcher)
    fetcher._api_key = "test"
    fetcher._quote_cache = {}
    fetcher._tf = type("FakeClient", (), {"quotes": FakeTickFlowQuotes()})()

    first = fetcher.fetch_quotes(["AAPL", "NVDA"])
    second = fetcher.fetch_quotes(["AAPL", "NVDA"])

    assert list(first) == ["AAPL", "NVDA"]
    assert first["AAPL"]["latest"] == 100
    assert first["NVDA"]["latest"] == 101
    assert second == first
    assert fetcher._tf.quotes.calls == [["AAPL.US", "NVDA.US"]]


def test_tickflow_fetch_quotes_respects_five_symbol_batch_limit():
    fetcher = TickFlowFetcher.__new__(TickFlowFetcher)
    fetcher._api_key = "test"
    fetcher._quote_cache = {}
    fetcher._tf = type("FakeClient", (), {"quotes": FakeTickFlowQuotes()})()
    codes = [f"S{i}" for i in range(11)]

    result = fetcher.fetch_quotes(codes)

    assert len(result) == 11
    assert [len(call) for call in fetcher._tf.quotes.calls] == [5, 5, 1]


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


def test_a_share_search_matches_code_and_preserves_leading_zero():
    calls = []

    def directory():
        calls.append(1)
        return pd.DataFrame({
            "code": [600519, 1],
            "name": ["贵州茅台", "平安银行"],
        })

    fake_akshare = SimpleNamespace(
        stock_info_a_code_name=directory,
    )
    _A_DIRECTORY_CACHE.update({"loaded_at": 0.0, "frame": None})
    with patch.dict(sys.modules, {"akshare": fake_akshare}):
        assert search_a_stock("600519")[0] == {
            "code": "600519", "name": "贵州茅台", "market": "A",
        }
        assert search_a_stock("000001")[0] == {
            "code": "000001", "name": "平安银行", "market": "A",
        }
    assert len(calls) == 1

    assert search_a_stock_fallback("600519")[0]["name"] == "贵州茅台"
    _A_DIRECTORY_CACHE.update({"loaded_at": 0.0, "frame": None})


def test_fetch_cached_prices_backfills_head_and_tail():
    fetcher = RecordingFetcher()
    db = MemoryPriceDB()
    with patch("data.stock_fetcher.get_stock_fetcher", return_value=fetcher):
        df = fetch_cached_prices("AAPL", "US", "2026-01-02", "2026-01-15", db=db)

    assert ("2026-01-02", "2026-01-08") in fetcher.calls
    assert ("2026-01-13", "2026-01-15") in fetcher.calls
    assert df["date"].min().strftime("%Y-%m-%d") == "2026-01-02"
    assert df["date"].max().strftime("%Y-%m-%d") == "2026-01-15"


def test_us_daily_history_falls_back_when_tickflow_tail_is_empty():
    fetcher = RecordingFetcher()
    fetcher.fetch_price_history = lambda code, start, end: []
    db = MemoryPriceDB()
    fallback = [
        PriceData(
            code="AAPL", date="2026-01-15",
            open=3, high=4, low=2.5, close=3.5, volume=100,
        )
    ]
    with (
        patch("data.stock_fetcher.get_stock_fetcher", return_value=fetcher),
        patch("data.stock_fetcher._fetch_yfinance_price_history", return_value=fallback) as yf,
    ):
        df = fetch_cached_prices(
            "AAPL", "US", "2026-01-09", "2026-01-15", db=db,
            listing_date="2020-01-01",
        )

    yf.assert_called_once_with("AAPL", "2026-01-13", "2026-01-15")
    assert df["date"].max().strftime("%Y-%m-%d") == "2026-01-15"


def test_filter_prices_for_cache_drops_unfinished_and_invalid_bars():
    prices = [
        PriceData(code="AAPL", date="2026-01-02", open=1, high=2, low=1, close=1.5, volume=100),
        PriceData(code="AAPL", date="2026-01-03", open=1, high=0.8, low=1.2, close=1, volume=100),
        PriceData(code="AAPL", date="2026-01-04", open=1, high=2, low=1, close=1.5, volume=100),
    ]

    filtered = _filter_prices_for_cache(
        prices,
        "US",
        latest_completed_date="2026-01-02",
    )

    assert [p.date for p in filtered] == ["2026-01-02"]


def test_dirty_persistent_cache_is_cleared_and_rebuilt_as_a_whole():
    Database._instance = None
    db = Database.init(tempfile.mktemp(suffix=".db"))
    db.insert_prices([
        PriceData("AAPL", "2026-01-02", 100, 102, 99, 101, 1000),
        PriceData("AAPL", "2026-01-04", 101, 103, 100, 102, 1000),
    ])
    with patch("data.stock_fetcher.get_stock_fetcher", return_value=CleanRebuildFetcher()):
        df = fetch_cached_prices(
            "AAPL", "US", "2026-01-02", "2026-01-05",
            db=db, listing_date="2020-01-01",
        )

    dates = [p.date for p in db.get_prices("AAPL")]
    assert dates == ["2026-01-02", "2026-01-05"]
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == dates


def test_filter_prices_for_cache_quarantines_jump_tail():
    prices = [
        PriceData(code="MU", date="2026-01-02", open=100, high=102, low=98, close=100, volume=100),
        PriceData(code="MU", date="2026-01-05", open=101, high=104, low=99, close=103, volume=100),
        PriceData(code="MU", date="2026-01-06", open=1000, high=1010, low=990, close=1005, volume=100),
        PriceData(code="MU", date="2026-01-05", open=1006, high=1020, low=1000, close=1015, volume=100),
    ]

    filtered = _filter_prices_for_cache(
        prices,
        "US",
        latest_completed_date="2026-01-05",
    )

    assert [p.date for p in filtered] == ["2026-01-02", "2026-01-05"]


def test_fetch_cached_prices_ignores_dirty_cached_tail():
    db = DirtyTailDB()
    fetcher = RecordingFetcher()

    with patch("data.stock_fetcher.get_stock_fetcher", return_value=fetcher):
        df = fetch_cached_prices("MU", "US", "2026-01-02", "2026-01-06", db=db)

    assert df["date"].max().strftime("%Y-%m-%d") == "2026-01-05"
    assert ("2026-01-06", "2026-01-06") in fetcher.calls


def test_fetch_cached_prices_clamps_window_to_ipo_and_allows_short_history():
    db = IpoPriceDB()
    fetcher = IpoFetcher()

    with patch("data.stock_fetcher.get_stock_fetcher", return_value=fetcher):
        df = fetch_cached_prices(
            "SPCX", "US", "2025-06-30", "2026-06-25",
            db=db, min_records=20, listing_date="2026-06-11",
        )

    assert fetcher.calls == [("2026-06-11", "2026-06-25")]
    assert df["date"].min().strftime("%Y-%m-%d") == "2026-06-11"
    assert len(df) == 3
    assert df.attrs["history_limited_by_listing"] is True


def test_fetch_cached_prices_resolves_listing_date_for_every_caller():
    db = IpoPriceDB()
    fetcher = IpoFetcher()

    with (
        patch("data.stock_fetcher.resolve_listing_date", return_value="2026-06-11") as resolver,
        patch("data.stock_fetcher.get_stock_fetcher", return_value=fetcher),
    ):
        df = fetch_cached_prices(
            "SPCX", "US", "2025-06-30", "2026-06-25",
            db=db, min_records=20,
        )

    resolver.assert_called_once_with("SPCX", "US", db=db)
    assert fetcher.calls == [("2026-06-11", "2026-06-25")]
    assert df["date"].min().strftime("%Y-%m-%d") == "2026-06-11"


def test_new_listing_quality_explains_short_history_without_old_price_conflict():
    dates = pd.date_range("2026-06-11", periods=13, freq="B")
    df = pd.DataFrame({
        "date": dates,
        "open": [150.0] * 13,
        "high": [160.0] * 13,
        "low": [145.0] * 13,
        "close": [155.0] * 13,
        "volume": [1000.0] * 13,
    })

    report = evaluate_data_quality(
        df,
        current_price=156.0,
        market="US",
        listing_date="2026-06-11",
        requested_start="2025-06-30",
    )

    assert report.status == "blocked"
    assert any("新股上市后K线样本不足" in item for item in report.issues)
    assert not any("实时价与最新K线收盘价偏离" in item for item in report.issues)
    assert any("上市前数据未参与计算" in item for item in report.notes)


def test_new_listing_short_history_runs_through_quant_pipeline():
    Database.init(tempfile.mktemp(suffix=".db"))
    dates = pd.date_range("2026-06-11", periods=13, freq="B")
    df = pd.DataFrame({
        "date": dates,
        "open": [150.0 + i for i in range(13)],
        "high": [152.0 + i for i in range(13)],
        "low": [149.0 + i for i in range(13)],
        "close": [151.0 + i for i in range(13)],
        "volume": [1_000_000.0] * 13,
    })
    df.attrs["listing_date"] = "2026-06-11"
    df.attrs["requested_start"] = "2025-06-30"

    result = run_pipeline(
        df,
        market="US",
        stock_code="SPCX",
        current_price=164.0,
        expand_pool=False,
        skip_param_tuning=True,
    )

    assert len(result.df) == 13
    assert result.data_quality["status"] == "blocked"
    assert "新股上市后K线样本不足" in result.data_quality["issues"][0]


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


def test_minute_normalizer_keeps_only_valid_regular_session_bars():
    frame = pd.DataFrame({
        "时间": [
            "2026-07-01 09:30:00", "2026-07-01 12:00:00",
            "2026-07-01 13:00:00", "2026-07-01 15:01:00",
        ],
        "开盘": [100, 100, 101, 102], "最高": [101, 101, 102, 103],
        "最低": [99, 99, 100, 101], "收盘": [100, 100, 101, 102],
        "成交量": [10, 20, 30, 40],
    }, index=[10, 20, 30, 40])
    bars = normalize_intraday_frame(
        frame, code="600000", market="A", source="test",
        quality_status="supplemental",
    )

    assert [bar.session_date for bar in bars] == ["2026-07-01", "2026-07-01"]
    assert [datetime.fromtimestamp(
        bar.timestamp_ms / 1000, ZoneInfo("Asia/Shanghai")
    ).strftime("%H:%M") for bar in bars] == ["09:30", "13:00"]


if __name__ == "__main__":
    test_tickflow_availability_ok()
    test_extended_quote_availability_ok()
    test_a_share_search_matches_code_and_preserves_leading_zero()
    test_fetch_cached_prices_backfills_head_and_tail()
    test_us_daily_history_falls_back_when_tickflow_tail_is_empty()
    test_filter_prices_for_cache_drops_unfinished_and_invalid_bars()
    test_dirty_persistent_cache_is_cleared_and_rebuilt_as_a_whole()
    test_filter_prices_for_cache_quarantines_jump_tail()
    test_fetch_cached_prices_ignores_dirty_cached_tail()
    test_fetch_cached_prices_clamps_window_to_ipo_and_allows_short_history()
    test_fetch_cached_prices_resolves_listing_date_for_every_caller()
    test_new_listing_quality_explains_short_history_without_old_price_conflict()
    test_new_listing_short_history_runs_through_quant_pipeline()
    test_tickflow_fetch_price_history_uses_date_window()
    test_tickflow_fetch_quotes_batches_symbols_and_reuses_cache()
    test_tickflow_fetch_quotes_respects_five_symbol_batch_limit()
    test_finnhub_debt_ratio_uses_normalized_metric()
    test_nasdaq_timestamp_is_parsed_as_eastern_time()
    test_minute_normalizer_keeps_only_valid_regular_session_bars()
    print("19/19 passed")
