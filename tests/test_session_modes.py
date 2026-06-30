"""
交易时段与模式边界测试。
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.analysis_service import _requires_realtime_token
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
    test_t1_context_can_force_local_template_without_second_llm_call()
    print("5/5 passed")
