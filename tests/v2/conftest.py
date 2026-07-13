from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tradehelper_v2.contracts import (
    AdjustmentMode, CanonicalBar, FreshnessStatus, InstrumentId, Market,
    QuoteSnapshot, TradingSession,
)
from tradehelper_v2.data.calendar import StaticTradingCalendar

NOW = datetime(2026, 7, 10, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def us_instrument() -> InstrumentId:
    return InstrumentId.from_code("aapl", Market.US, "XNAS")


@pytest.fixture
def a_instrument() -> InstrumentId:
    return InstrumentId.from_code("600519", Market.A)


@pytest.fixture
def calendar() -> StaticTradingCalendar:
    sessions = tuple(date(2026, 7, day) for day in (1, 2, 6, 7, 8, 9, 10, 13, 14, 15))
    return StaticTradingCalendar(sessions=sessions, completed_sessions=sessions[:6])


def make_bar(instrument: InstrumentId, day: date, close: float = 100.0, volume: int = 1000) -> CanonicalBar:
    return CanonicalBar(
        instrument=instrument, trading_date=day, open=close - 1, high=close + 2, low=close - 2,
        close=close, volume=volume, adjustment_mode=AdjustmentMode.FRONT_ADJUSTED,
        source="fixture", fetched_at=NOW,
    )


def make_quote(
    instrument: InstrumentId,
    observed_at: datetime = NOW,
    session: TradingSession = TradingSession.REGULAR,
    **overrides: object,
) -> QuoteSnapshot:
    values: dict[str, object] = {
        "instrument": instrument, "session": session, "price": 217.0, "prev_close": 210.0,
        "open": 212.0, "high": 218.0, "low": 211.0, "volume": 1200,
        "bid": None, "ask": None, "observed_at": observed_at, "fetched_at": NOW,
        "source": "fixture", "freshness_status": FreshnessStatus.NOT_REQUIRED,
    }
    values.update(overrides)
    return QuoteSnapshot(**values)  # type: ignore[arg-type]


@pytest.fixture
def bar_factory():
    return make_bar


@pytest.fixture
def quote_factory():
    return make_quote
