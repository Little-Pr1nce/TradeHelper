from datetime import date, timedelta

from contracts import InstrumentId, Market
from data.quality import effective_start_date, evaluate_data_quality
from contracts.enums import DecisionMode


def test_g40_ipo_window_clips_and_old_bars_are_blocked(us_instrument, bar_factory, now) -> None:
    listing = date(2026, 6, 11)
    assert effective_start_date(date(2025, 7, 1), listing) == listing
    report = evaluate_data_quality([bar_factory(us_instrument, date(2026, 6, 10))], market=Market.US, mode=DecisionMode.EOD, as_of=now, listing_date=listing)
    assert "BEFORE_LISTING_DATE" in {issue.code for issue in report.issues}
