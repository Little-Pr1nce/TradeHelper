"""
数据源可用性检查测试。
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.stock_fetcher import check_tickflow_available, check_extended_quote_available
from data.models import StockInfo, PriceData


class DummyFetcher:
    has_realtime = True

    def fetch_stock_info(self, code):
        return StockInfo(code=code, name=code, market="US")

    def fetch_price_history(self, code, start, end):
        return [PriceData(code=code, date="2024-01-02", open=1, high=2, low=1, close=2, volume=100)]

    def fetch_quote(self, code):
        return {"latest": 2.0}


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


if __name__ == "__main__":
    test_tickflow_availability_ok()
    test_extended_quote_availability_ok()
    print("2/2 passed")
