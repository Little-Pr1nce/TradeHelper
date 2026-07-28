from dataclasses import replace
from datetime import date, timedelta

from contracts import DailyBarsRequest, FundamentalSnapshot, FundamentalValue, InstrumentId, Market, ProviderResult, ProviderStatus, QuoteBatch
from data import DataProviders, DataRefreshService, DailyBarDriftMonitor
from data.cache import DataCache
from data.rate_limit import InMemoryFinnhubRateBudget, SQLiteDailyRateBudget
from data.repository import SQLiteRepository


def test_g28_a_daily_budget_is_persistent_and_due_work_resumes(tmp_path, a_instrument, bar_factory, now, calendar) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    calls: list[str] = []

    def tickflow(instrument, _start, _end):
        calls.append(instrument.code)
        return ProviderResult.success((bar_factory(instrument, date(2026, 7, 9)),), "tickflow", now)

    service = DataRefreshService(
        DataProviders(tickflow_daily=tickflow), calendar, DataCache(), repo,
        daily_rate_budget=SQLiteDailyRateBudget(repo),
    )
    requests = tuple(
        DailyBarsRequest(InstrumentId.from_code(f"{600000 + offset:06d}", Market.A), date(2026, 7, 9), date(2026, 7, 9))
        for offset in range(11)
    )
    result = service.refresh_daily_bars_batch(requests, now)
    pending_instrument = requests[-1].instrument
    assert len(calls) == 10
    assert result.results[pending_instrument].status is ProviderStatus.RATE_LIMITED
    assert result.pending_retry_at[pending_instrument] == now + timedelta(minutes=1)
    assert len(repo.due_daily_refreshes(now, limit=10)) == 0

    resumed_service = DataRefreshService(
        DataProviders(tickflow_daily=tickflow), calendar, DataCache(), repo,
        daily_rate_budget=SQLiteDailyRateBudget(repo),
    )
    resumed = resumed_service.refresh_due_daily_bars(now + timedelta(minutes=1), limit=10)
    assert resumed.results[pending_instrument].status is ProviderStatus.OK
    assert len(calls) == 11
    assert repo.due_daily_refreshes(now + timedelta(minutes=2), limit=10) == ()
    repo.close()


def test_daily_source_drift_is_recorded_without_changing_daily_bars(tmp_path, us_instrument, bar_factory, now) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    primary = replace(bar_factory(us_instrument, date(2026, 7, 9), 210.0), source="nasdaq")
    comparison = replace(bar_factory(us_instrument, date(2026, 7, 9), 210.5), source="yfinance")
    records = DailyBarDriftMonitor(repo).compare((primary,), (comparison,), now)
    assert records[0].status == "drift" and records[0].max_abs_price_diff == 0.5
    persisted = repo.list_daily_bar_drift(us_instrument)
    assert persisted[0].primary_source == "nasdaq" and persisted[0].comparator_source == "yfinance"
    assert repo.list_daily_bars(us_instrument, date(2026, 7, 9), date(2026, 7, 9)) == ()
    repo.close()


def test_tickflow_quote_batch_uses_ten_request_budget_and_never_sends_the_51st(calendar, quote_factory, now) -> None:
    instruments = tuple(InstrumentId.from_code(f"T{index}", Market.US, "XNAS") for index in range(51))
    calls: list[tuple[InstrumentId, ...]] = []

    def quotes(items, _session):
        calls.append(items)
        return QuoteBatch({item: quote_factory(item) for item in items}, {})

    service = DataRefreshService(DataProviders(tickflow_quotes=quotes), calendar, DataCache())
    first = service.refresh_intraday_quotes(instruments, now)
    assert len(calls) == 1 and len(calls[0]) == 50
    assert len(first.quotes) == 50 and first.failures[instruments[-1]] is ProviderStatus.RATE_LIMITED

    second = service.refresh_intraday_quotes(instruments[:5], now)
    assert len(second.quotes) == 5 and not second.failures and len(calls) == 1


def test_finnhub_profile_metrics_and_news_share_one_persistent_budget(now) -> None:
    budget = InMemoryFinnhubRateBudget()
    assert all(budget.reserve(now) is None for _ in range(60))
    assert budget.reserve(now) == now + timedelta(minutes=1)


def test_finnhub_limited_facts_are_persisted_and_resumed(tmp_path, us_instrument, now, calendar) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    second = InstrumentId.from_code("MSFT", Market.US, "XNAS")
    calls: list[str] = []

    def finnhub(instrument):
        calls.append(instrument.code)
        snapshot = FundamentalSnapshot(
            instrument, {"pe": FundamentalValue(20.0, None, None, None, "finnhub")}, now, now, "finnhub", "ok"
        )
        return ProviderResult.success(snapshot, "finnhub", now)

    service = DataRefreshService(
        DataProviders(finnhub_fundamentals=finnhub), calendar, DataCache(), repo,
        finnhub_rate_budget=InMemoryFinnhubRateBudget(limit=1),
    )
    assert service.refresh_fundamentals(us_instrument, now).status is ProviderStatus.OK
    limited = service.refresh_fundamentals(second, now)
    assert limited.status is ProviderStatus.RATE_LIMITED and len(repo.due_provider_refreshes(now + timedelta(seconds=30), limit=10)) == 0

    outcomes = service.refresh_due_provider_facts(now + timedelta(minutes=1), limit=10)
    assert list(outcomes.values()) == [ProviderStatus.OK]
    assert calls == ["AAPL", "MSFT"] and repo.due_provider_refreshes(now + timedelta(minutes=2), limit=10) == ()
    repo.close()


def test_completed_queue_identity_can_be_reactivated_and_completed_again(tmp_path, a_instrument, now) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    request = DailyBarsRequest(a_instrument, date(2026, 7, 1), date(2026, 7, 9))
    repo.enqueue_daily_refresh(request, now + timedelta(minutes=1))
    daily_id = repo.due_daily_refreshes(now + timedelta(minutes=2), limit=1)[0].queue_id
    repo.mark_daily_refresh_complete(daily_id)
    repo.enqueue_daily_refresh(request, now + timedelta(minutes=3))
    repeated_daily_id = repo.due_daily_refreshes(now + timedelta(minutes=4), limit=1)[0].queue_id
    repo.mark_daily_refresh_complete(repeated_daily_id)
    assert daily_id == repeated_daily_id

    repo.enqueue_provider_refresh("news", a_instrument, now + timedelta(minutes=1))
    provider_id = repo.due_provider_refreshes(now + timedelta(minutes=2), limit=1)[0].queue_id
    repo.mark_provider_refresh_complete(provider_id)
    repo.enqueue_provider_refresh("news", a_instrument, now + timedelta(minutes=3))
    repeated_provider_id = repo.due_provider_refreshes(now + timedelta(minutes=4), limit=1)[0].queue_id
    repo.mark_provider_refresh_complete(repeated_provider_id)
    assert provider_id == repeated_provider_id
    repo.close()
