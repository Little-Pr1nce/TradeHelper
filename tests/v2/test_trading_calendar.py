from datetime import date

import pytest

from tradehelper_v2.contracts import Market
from tradehelper_v2.data.calendar import StaticTradingCalendar, TradingCalendarUnavailable


def test_g33_calendar_never_falls_back_to_weekdays() -> None:
    with pytest.raises(TradingCalendarUnavailable):
        StaticTradingCalendar(()).latest_completed_session(Market.US, __import__("conftest").NOW)


def test_g34_injected_calendar_target_dates() -> None:
    cal = StaticTradingCalendar(tuple(date(2026, 7, day) for day in (1, 2, 6, 7, 8, 9)))
    assert cal.target_dates(Market.US, date(2026, 7, 1), (1, 3, 5)) == {1: date(2026, 7, 2), 3: date(2026, 7, 7), 5: date(2026, 7, 9)}
