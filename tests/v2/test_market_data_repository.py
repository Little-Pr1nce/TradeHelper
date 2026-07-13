from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from tradehelper_v2.contracts import (
    AdjustmentMode, CanonicalBar, ContractViolation, FreshnessStatus, FundamentalSnapshot,
    FundamentalValue, Market, NewsSnapshot, QualityStatus,
)
from tradehelper_v2.contracts.enums import TradingSession
from tradehelper_v2.data.repository import SQLiteRepository


def test_g50_quote_does_not_pollute_daily_bars(tmp_path, us_instrument, bar_factory, quote_factory, now) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    repo.upsert_daily_bars((bar_factory(us_instrument, date(2026, 7, 9), 210),))
    repo.save_quote_snapshot(quote_factory(us_instrument, price=217))
    bars = repo.list_daily_bars(us_instrument, date(2026, 7, 1), date(2026, 7, 10))
    assert len(bars) == 1 and bars[0].close == 210
    assert repo.get_latest_quote(us_instrument, TradingSession.REGULAR).price == 217
    repo.close()


def test_g51_idempotent_and_conflicting_daily_bar(tmp_path, us_instrument, bar_factory) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    bar = bar_factory(us_instrument, date(2026, 7, 9), 210)
    assert repo.upsert_daily_bars((bar,)).inserted == 1
    assert repo.upsert_daily_bars((bar,)).idempotent == 1
    assert repo.upsert_daily_bars((bar_factory(us_instrument, date(2026, 7, 9), 211),)).conflicts == 1
    assert repo.list_daily_bars(us_instrument, date(2026, 7, 1), date(2026, 7, 10))[0].close == 210
    assert repo._connection.execute("SELECT COUNT(*) FROM quarantine_records").fetchone()[0] == 1
    repo.close()


def test_g52_daily_batch_contract_failure_rolls_back(tmp_path, us_instrument, bar_factory, now) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    valid = [bar_factory(us_instrument, date(2026, 6, day), 100 + day) for day in range(1, 10)]
    # Simulate a malformed object arriving through a corrupted deserializer.
    # Direct construction correctly fails earlier; the repository must still
    # defend its transactional boundary.
    invalid = object.__new__(CanonicalBar)
    for field, value in {
        "instrument": us_instrument, "trading_date": date(2026, 6, 10), "open": 100.0, "high": 105.0,
        "low": 95.0, "close": 108.0, "volume": 1, "adjustment_mode": AdjustmentMode.FRONT_ADJUSTED,
        "source": "fixture", "fetched_at": now, "corporate_action_version": None, "schema_version": 1,
    }.items():
        object.__setattr__(invalid, field, value)
    with pytest.raises(ContractViolation, match="INVALID_OHLC"):
        repo.upsert_daily_bars(tuple(valid) + (invalid,))
    assert repo.list_daily_bars(us_instrument, date(2026, 6, 1), date(2026, 6, 30)) == ()
    repo.close()


def test_g56_point_in_time_news_and_fundamentals(tmp_path, us_instrument, now) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    published = now.replace(hour=10, minute=0)
    available = now.replace(hour=10, minute=30)
    repo.upsert_news((NewsSnapshot(us_instrument, "News", "fixture", published, available, now, None, False, None, None, None),))
    snapshot = FundamentalSnapshot(us_instrument, {"pe": FundamentalValue(20.0, None, None, None, "fixture")}, available, now, "fixture", QualityStatus.OK)
    repo.upsert_fundamental_snapshot(snapshot)
    assert repo.list_news_as_of(us_instrument, now.replace(hour=10, minute=15)) == ()
    assert repo.get_fundamentals_as_of(us_instrument, now.replace(hour=10, minute=15)) is None
    assert len(repo.list_news_as_of(us_instrument, now.replace(hour=10, minute=31))) == 1
    assert repo.get_fundamentals_as_of(us_instrument, now.replace(hour=10, minute=31)) is not None
    repo.close()


def test_repeated_news_refresh_preserves_earliest_ingestion_time(tmp_path, us_instrument, now) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    published = now.replace(hour=8)
    first_seen = now.replace(hour=10)
    fetched_again = now.replace(hour=12)
    repo.upsert_news((NewsSnapshot(us_instrument, "News", "fixture", published, first_seen, first_seen, None, False, None, None, None),))
    repo.upsert_news((NewsSnapshot(us_instrument, "News", "fixture", published, fetched_again, fetched_again, "updated", False, None, None, None),))
    stored = repo.list_news_as_of(us_instrument, now.replace(hour=10, minute=1))[0]
    assert stored.available_at == first_seen and stored.fetched_at == fetched_again and stored.content == "updated"
    repo.close()


def test_repository_serializes_concurrent_reads_and_writes(tmp_path, us_instrument, bar_factory) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")

    def write(day: int) -> int:
        return repo.upsert_daily_bars((bar_factory(us_instrument, date(2026, 6, day), 100 + day),)).inserted

    def read(_index: int) -> int:
        return len(repo.list_daily_bars(us_instrument, date(2026, 6, 1), date(2026, 6, 30)))

    with ThreadPoolExecutor(max_workers=8) as executor:
        inserted = tuple(executor.map(write, range(1, 21)))
        observed = tuple(executor.map(read, range(20)))

    assert sum(inserted) == 20
    assert observed == (20,) * 20
    repo.close()
