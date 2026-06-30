"""
交易时段与模式边界测试。
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.analysis_service import (
    _fetch_quote_inputs_for_mode,
    _requires_realtime_token,
)
from services.portfolio_service import _should_fetch_realtime_quote
from utils.session import _infer_by_time
from report.generator import generate_report


def test_us_session_uses_new_york_time():
    tz = ZoneInfo("America/New_York")

    assert _infer_by_time(datetime(2026, 6, 24, 8, 0, tzinfo=tz), "US") == "pre"
    assert _infer_by_time(datetime(2026, 6, 24, 10, 0, tzinfo=tz), "US") == "intraday"
    assert _infer_by_time(datetime(2026, 6, 24, 17, 0, tzinfo=tz), "US") == "post"
    assert _infer_by_time(datetime(2026, 6, 24, 21, 0, tzinfo=tz), "US") == "closed"


def test_weekend_is_closed():
    tz = ZoneInfo("America/New_York")
    assert _infer_by_time(datetime(2026, 6, 27, 10, 0, tzinfo=tz), "US") == "closed"


def test_realtime_token_requirements_by_mode():
    assert _requires_realtime_token("US", "pre") is False
    assert _requires_realtime_token("US", "intraday") is True
    assert _requires_realtime_token("A", "pre") is True
    assert _requires_realtime_token("A", "eod") is False


def test_portfolio_realtime_quote_requirements_by_mode():
    assert _should_fetch_realtime_quote("US", "pre") is True
    assert _should_fetch_realtime_quote("US", "eod") is False


def _epoch_ms(hour: int, minute: int = 0) -> int:
    return int(datetime(
        2026, 6, 30, hour, minute,
        tzinfo=ZoneInfo("America/New_York"),
    ).timestamp() * 1000)


class CountingTickFlow:
    def __init__(self):
        self.calls = []

    def fetch_quote(self, code):
        self.calls.append(("quote", code))
        return {"latest": 101.0, "timestamp": _epoch_ms(10)}

    def fetch_stock_tick(self, code):
        self.calls.append(("tick", code))
        return {"latest": 101.0, "timestamp": _epoch_ms(10)}


def test_us_premarket_mode_never_calls_tickflow():
    fetcher = CountingTickFlow()
    extended = {
        "price": 100.5, "latest": 100.5,
        "timestamp": _epoch_ms(7), "source": "Nasdaq.com",
        "volume": 1000,
    }
    with patch("services.analysis_service.fetch_us_extended_quote", return_value=extended):
        tick, quote, session = _fetch_quote_inputs_for_mode(
            "AAPL", "US", "pre", fetcher,
        )

    assert fetcher.calls == []
    assert quote["source"] == "Nasdaq.com"
    assert tick["latest"] == 100.5
    assert session == "pre"


def test_us_eod_mode_uses_extended_source_and_rejects_regular_quote():
    fetcher = CountingTickFlow()
    post = {
        "price": 102.0, "timestamp": _epoch_ms(17),
        "source": "Nasdaq.com", "volume": 2000,
    }
    with patch("services.analysis_service.fetch_us_extended_quote", return_value=post):
        _, quote, session = _fetch_quote_inputs_for_mode("AAPL", "US", "eod", fetcher)
    assert fetcher.calls == []
    assert quote["latest"] == 102.0
    assert session == "post"

    regular = dict(post, timestamp=_epoch_ms(10))
    with patch("services.analysis_service.fetch_us_extended_quote", return_value=regular):
        tick, quote, session = _fetch_quote_inputs_for_mode("AAPL", "US", "eod", fetcher)
    assert tick is None and quote is None
    assert session == "intraday"


def test_us_intraday_mode_uses_only_tickflow():
    fetcher = CountingTickFlow()
    with patch(
        "services.analysis_service.fetch_us_extended_quote",
        side_effect=AssertionError("intraday must not call extended quote source"),
    ):
        tick, quote, session = _fetch_quote_inputs_for_mode(
            "AAPL", "US", "intraday", fetcher,
        )
    assert fetcher.calls == [("quote", "AAPL"), ("tick", "AAPL")]
    assert tick["latest"] == quote["latest"] == 101.0
    assert session == "intraday"


def test_t1_context_can_force_local_template_without_second_llm_call():
    with patch("openai.OpenAI", side_effect=AssertionError("LLM must not be called")):
        report = generate_report(
            {"code": "AAPL", "name": "Apple", "market": "US"},
            "技术面样本",
            {"summary": "暂无新闻", "top_news": "", "sentiment_score": 0.0},
            {},
            data_range="2026-01-01 ~ 2026-06-29",
            use_llm=False,
        )

    assert "Apple" in report


if __name__ == "__main__":
    test_us_session_uses_new_york_time()
    test_weekend_is_closed()
    test_realtime_token_requirements_by_mode()
    test_portfolio_realtime_quote_requirements_by_mode()
    test_us_premarket_mode_never_calls_tickflow()
    test_us_eod_mode_uses_extended_source_and_rejects_regular_quote()
    test_us_intraday_mode_uses_only_tickflow()
    test_t1_context_can_force_local_template_without_second_llm_call()
    print("8/8 passed")
