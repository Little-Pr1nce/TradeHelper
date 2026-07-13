from datetime import date, datetime, timedelta, timezone
import sys
from types import SimpleNamespace

from tradehelper_v2.contracts import DailyBarsRequest, FreshnessStatus, FundamentalSnapshot, FundamentalValue, InstrumentId, ProviderResult, ProviderStatus, StockMetadata
from tradehelper_v2.contracts.enums import DecisionMode, Market, TradingSession
from tradehelper_v2.data import DataProviders, DataRefreshService
from tradehelper_v2.data.cache import DataCache
from tradehelper_v2.data.composition import _AkshareTransport, _TickFlowTransport
from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.config.settings import V2Settings


def test_tickflow_daily_buffers_exclusive_start_boundary(tmp_path) -> None:
    captured: dict[str, int] = {}

    class Klines:
        def get(self, _symbol, **kwargs):
            captured["start_time"] = kwargs["start_time"]
            return []

    class Client:
        klines = Klines()

    transport = _TickFlowTransport(V2Settings.from_mapping({"work_dir": tmp_path}))
    transport._clients[Market.A] = Client()
    transport.daily("688981.SH", date(2026, 7, 1), date(2026, 7, 10))

    expected = datetime(2026, 6, 30, tzinfo=timezone.utc)
    assert captured["start_time"] == int(expected.timestamp() * 1000)


def test_akshare_a_fundamentals_select_latest_annual_report_and_explicit_fields(monkeypatch) -> None:
    rows = [
        {
            "REPORT_DATE": "2024-12-31 00:00:00", "REPORT_DATE_NAME": "2024年报",
            "NOTICE_DATE": "2025-04-01 00:00:00", "ROEJQ": 30.0,
            "XSMLL": 90.0, "TOTALOPERATEREVETZ": 10.0,
            "PARENTNETPROFITTZ": 8.0, "ZCFZL": 20.0,
        },
        {
            "REPORT_DATE": "2026-03-31 00:00:00", "REPORT_DATE_NAME": "2026一季报",
            "NOTICE_DATE": "2026-04-30 00:00:00", "ROEJQ": 99.0,
            "XSMLL": 99.0, "TOTALOPERATEREVETZ": 99.0,
            "PARENTNETPROFITTZ": 99.0, "ZCFZL": 99.0,
        },
        {
            "REPORT_DATE": "2025-12-31 00:00:00", "REPORT_DATE_NAME": "2025年报",
            "NOTICE_DATE": "2026-04-17 00:00:00", "ROEJQ": 32.53,
            "XSMLL": 91.18, "TOTALOPERATEREVETZ": -1.20,
            "PARENTNETPROFITTZ": -4.53, "ZCFZL": 16.42,
        },
    ]
    fake = SimpleNamespace(
        stock_financial_analysis_indicator_em=lambda **_: rows,
    )
    monkeypatch.setitem(sys.modules, "akshare", fake)

    result = _AkshareTransport().fundamentals("600519")

    assert set(result["fields"]) == {
        "weighted_roe_annual", "gross_margin_annual", "revenue_yoy_annual",
        "net_profit_yoy_annual", "debt_ratio_annual",
    }
    assert result["fields"]["revenue_yoy_annual"]["value"] == -1.20
    assert result["fields"]["weighted_roe_annual"]["period_end"] == "2025-12-31"
    assert result["fields"]["weighted_roe_annual"]["published_at"] == "2026-04-17 00:00:00"
    assert all(field["unit"] == "percent" for field in result["fields"].values())


def test_g20_us_pre_routes_nasdaq_then_yfinance(us_instrument, quote_factory, now, calendar) -> None:
    calls: list[str] = []
    def nasdaq(_, __):
        calls.append("nasdaq")
        return ProviderResult.failure(ProviderStatus.TIMEOUT, now)
    def yfinance(_, __):
        calls.append("yfinance")
        return ProviderResult.success(quote_factory(us_instrument, session=TradingSession.PRE), "yfinance", now)
    service = DataRefreshService(DataProviders(nasdaq_extended_quote=nasdaq, yfinance_extended_quote=yfinance), calendar, DataCache())
    result = service.refresh_quote(us_instrument, DecisionMode.PRE, now)
    assert calls == ["nasdaq", "yfinance"]
    assert result.status is ProviderStatus.OK and result.selected_source == "yfinance" and result.fallback_reason


def test_us_pre_rejects_nasdaq_quote_without_observation_time(us_instrument, quote_factory, now, calendar) -> None:
    calls: list[str] = []

    def nasdaq(_, __):
        calls.append("nasdaq")
        quote = quote_factory(
            us_instrument, session=TradingSession.PRE,
            freshness_status=FreshnessStatus.MISSING_TIMESTAMP, source="nasdaq",
        )
        return ProviderResult.success(quote, "nasdaq", now)

    def yfinance(_, __):
        calls.append("yfinance")
        return ProviderResult.success(
            quote_factory(us_instrument, session=TradingSession.PRE, source="yfinance"), "yfinance", now
        )

    service = DataRefreshService(
        DataProviders(nasdaq_extended_quote=nasdaq, yfinance_extended_quote=yfinance), calendar, DataCache()
    )
    result = service.refresh_quote(us_instrument, DecisionMode.PRE, now)
    assert calls == ["nasdaq", "yfinance"]
    assert result.status is ProviderStatus.OK and result.selected_source == "yfinance"
    assert result.value is not None and result.value.freshness_status is FreshnessStatus.FRESH


def test_g21_regular_session_never_uses_extended_fallback(us_instrument, now, calendar) -> None:
    calls: list[str] = []
    def tickflow(_, __):
        calls.append("tickflow")
        return ProviderResult.failure(ProviderStatus.RATE_LIMITED, now)
    def forbidden(_, __):
        calls.append("forbidden")
        return ProviderResult.success(None, "forbidden", now)  # pragma: no cover
    service = DataRefreshService(DataProviders(tickflow_quote=tickflow, nasdaq_extended_quote=forbidden, yfinance_extended_quote=forbidden), calendar, DataCache())
    assert service.refresh_quote(us_instrument, DecisionMode.INTRADAY, now).status is ProviderStatus.RATE_LIMITED
    assert calls == ["tickflow"]


def test_g22_us_daily_fallback_accepts_completed_sessions_only(us_instrument, bar_factory, now, calendar) -> None:
    calls: list[str] = []
    def nasdaq(*_):
        calls.append("nasdaq")
        return ProviderResult.failure(ProviderStatus.EMPTY, now)
    def yfinance(_, __, ___):
        calls.append("yfinance")
        return ProviderResult.success(tuple(bar_factory(us_instrument, date(2026, 7, day)) for day in (8, 9, 10)), "yfinance", now)
    service = DataRefreshService(DataProviders(nasdaq_daily=nasdaq, yfinance_daily=yfinance), calendar, DataCache())
    result = service.refresh_daily_bars(us_instrument, date(2026, 7, 8), date(2026, 7, 10), None, now)
    assert calls == ["nasdaq", "yfinance"]
    assert [bar.trading_date for bar in result.value or ()] == [date(2026, 7, 8), date(2026, 7, 9)]


def test_g23_a_daily_has_no_fallback(a_instrument, now, calendar) -> None:
    calls: list[str] = []
    def tickflow(*_):
        calls.append("tickflow")
        return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now)
    def forbidden(*_):
        calls.append("forbidden")
        return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now)
    service = DataRefreshService(DataProviders(tickflow_daily=tickflow, yfinance_daily=forbidden), calendar, DataCache())
    assert service.refresh_daily_bars(a_instrument, date(2026, 7, 1), date(2026, 7, 9), None, now).status is ProviderStatus.UNAVAILABLE
    assert calls == ["tickflow"]


def test_us_daily_uses_tickflow_only_after_nasdaq_and_yfinance_fail(us_instrument, bar_factory, now, calendar) -> None:
    calls: list[str] = []
    def failed(name):
        def loader(*_):
            calls.append(name)
            return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now)
        return loader
    def tickflow(_, __, ___):
        calls.append("tickflow")
        return ProviderResult.success((bar_factory(us_instrument, date(2026, 7, 9)),), "tickflow", now)
    service = DataRefreshService(
        DataProviders(nasdaq_daily=failed("nasdaq"), yfinance_daily=failed("yfinance"), tickflow_daily=tickflow),
        calendar,
        DataCache(),
    )
    result = service.refresh_daily_bars(us_instrument, date(2026, 7, 9), date(2026, 7, 9), None, now)
    assert calls == ["nasdaq", "yfinance", "tickflow"]
    assert result.selected_source == "tickflow"


def test_g24_a_pre_does_not_call_continuous_provider(a_instrument, now, calendar) -> None:
    called = False
    def quote(_, __):
        nonlocal called
        called = True
        return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now)
    service = DataRefreshService(DataProviders(tickflow_quote=quote), calendar, DataCache())
    result = service.refresh_quote(a_instrument, DecisionMode.PRE, now)
    assert result.status is ProviderStatus.UNAVAILABLE and not called
    assert "T-1" in (result.fallback_reason or "")


def test_metadata_uses_authoritative_source_without_spending_tickflow_daily_quota(a_instrument, now, calendar) -> None:
    calls: list[str] = []

    def baostock(_):
        calls.append("baostock")
        return ProviderResult.success(StockMetadata(a_instrument, "贵州茅台", None, None, date(2001, 8, 27), "baostock", now), "baostock", now)

    def forbidden(_):
        calls.append("tickflow")
        return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now)

    service = DataRefreshService(
        DataProviders(baostock_metadata=baostock, tickflow_metadata=forbidden), calendar, DataCache()
    )
    metadata = service.refresh_metadata(a_instrument, now)
    listing = service.refresh_listing_date(a_instrument, now)
    assert metadata.selected_source == "baostock" and listing.value == date(2001, 8, 27)
    assert calls == ["baostock"]


def test_g25_llm_is_not_a_fundamental_provider(us_instrument, now, calendar) -> None:
    calls = 0
    service = DataRefreshService(DataProviders(), calendar, DataCache())
    result = service.refresh_fundamentals(us_instrument, now)
    assert result.status is ProviderStatus.UNAVAILABLE
    assert calls == 0


def test_market_specific_fundamental_routes_cannot_cross_markets(a_instrument, us_instrument, now, calendar) -> None:
    calls: list[str] = []
    def baostock(_):
        calls.append("baostock")
        return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now)
    def akshare(_):
        calls.append("akshare")
        snapshot = FundamentalSnapshot(a_instrument, {"pe": FundamentalValue(10.0, None, None, None, "akshare")}, now, now, "akshare", "ok")
        return ProviderResult.success(snapshot, "akshare", now)
    def finnhub(_):
        calls.append("finnhub")
        snapshot = FundamentalSnapshot(us_instrument, {"pe": FundamentalValue(20.0, None, None, None, "finnhub")}, now, now, "finnhub", "ok")
        return ProviderResult.success(snapshot, "finnhub", now)
    service = DataRefreshService(DataProviders(baostock_fundamentals=baostock, akshare_fundamentals=akshare, finnhub_fundamentals=finnhub), calendar, DataCache())
    assert service.refresh_fundamentals(a_instrument, now).selected_source == "akshare"
    assert calls == ["baostock", "akshare"]
    calls.clear()
    assert service.refresh_fundamentals(us_instrument, now).selected_source == "finnhub"
    assert calls == ["finnhub", "akshare"]


def test_us_fundamentals_fall_back_to_yfinance_when_finnhub_is_unavailable(us_instrument, now, calendar) -> None:
    calls: list[str] = []

    def finnhub(_):
        calls.append("finnhub")
        return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now)

    def yfinance(_):
        calls.append("yfinance")
        snapshot = FundamentalSnapshot(us_instrument, {"trailingPE": FundamentalValue(30.0, None, None, None, "yfinance")}, now, now, "yfinance", "ok")
        return ProviderResult.success(snapshot, "yfinance", now)

    service = DataRefreshService(
        DataProviders(finnhub_fundamentals=finnhub, yfinance_fundamentals=yfinance), calendar, DataCache()
    )
    result = service.refresh_fundamentals(us_instrument, now)
    assert result.selected_source == "yfinance" and calls == ["finnhub", "yfinance"]


def test_incomplete_primary_fundamentals_are_supplemented_per_field(us_instrument, now, calendar) -> None:
    primary = FundamentalSnapshot(
        us_instrument, {"pe_ttm": FundamentalValue(20.0, "multiple", None, None, "finnhub")},
        now, now, "finnhub", "watch",
    )
    fallback = FundamentalSnapshot(
        us_instrument,
        {
            "roe": FundamentalValue(0.2, "ratio", date(2025, 12, 31), None, "yfinance"),
            "revenue_growth": FundamentalValue(0.1, "ratio", date(2025, 12, 31), None, "yfinance"),
            "debt_to_equity": FundamentalValue(0.3, "ratio", date(2025, 12, 31), None, "yfinance"),
        },
        now, now, "yfinance", "ok",
    )
    service = DataRefreshService(
        DataProviders(
            finnhub_fundamentals=lambda _: ProviderResult.success(primary, "finnhub", now),
            yfinance_fundamentals=lambda _: ProviderResult.success(fallback, "yfinance", now),
        ),
        calendar, DataCache(),
    )
    result = service.refresh_fundamentals(us_instrument, now)
    assert result.status is ProviderStatus.OK and result.value is not None
    assert result.selected_source == "finnhub+yfinance" and result.value.quality_status.value == "ok"
    assert result.value.fields["pe_ttm"].source == "finnhub"
    assert result.value.fields["roe"].source == "yfinance"


def test_a_share_semantic_fields_are_supplemented_even_when_primary_quality_is_ok(
    a_instrument, now, calendar
) -> None:
    calls: list[str] = []
    primary = FundamentalSnapshot(
        a_instrument,
        {
            "pe_ttm": FundamentalValue(18.0, "multiple", date(2026, 7, 10), None, "baostock"),
            "roe": FundamentalValue(0.34, "ratio", date(2025, 12, 31), None, "baostock"),
            "gross_margin": FundamentalValue(0.91, "ratio", date(2025, 12, 31), None, "baostock"),
            "net_profit_yoy": FundamentalValue(-0.045, "ratio", date(2025, 12, 31), None, "baostock"),
            "debt_ratio": FundamentalValue(0.16, "ratio", date(2025, 12, 31), None, "baostock"),
        },
        now, now, "baostock", "ok",
    )
    supplement = FundamentalSnapshot(
        a_instrument,
        {
            "weighted_roe_annual": FundamentalValue(32.53, "percent", date(2025, 12, 31), None, "akshare"),
            "revenue_yoy_annual": FundamentalValue(-1.21, "percent", date(2025, 12, 31), None, "akshare"),
        },
        now, now, "akshare", "watch",
    )

    def baostock(_):
        calls.append("baostock")
        return ProviderResult.success(primary, "baostock", now)

    def akshare(_):
        calls.append("akshare")
        return ProviderResult.success(supplement, "akshare", now)

    result = DataRefreshService(
        DataProviders(baostock_fundamentals=baostock, akshare_fundamentals=akshare),
        calendar,
        DataCache(),
    ).refresh_fundamentals(a_instrument, now)

    assert calls == ["baostock", "akshare"]
    assert result.selected_source == "baostock+akshare"
    assert result.value is not None
    assert result.value.fields["roe"].source == "baostock"
    assert result.value.fields["weighted_roe_annual"].source == "akshare"


def test_us_missing_registered_profit_growth_is_supplemented_even_when_raw_quality_is_ok(
    us_instrument, now, calendar
) -> None:
    calls: list[str] = []
    primary = FundamentalSnapshot(
        us_instrument,
        {
            "peTTM": FundamentalValue(38.0, None, None, None, "finnhub"),
            "roeTTM": FundamentalValue(140.0, None, None, None, "finnhub"),
            "revenueGrowthTTMYoy": FundamentalValue(12.0, None, None, None, "finnhub"),
            "totalDebtToEquityQuarterly": FundamentalValue(80.0, None, None, None, "finnhub"),
        },
        now, now, "finnhub", "ok",
    )
    supplement = FundamentalSnapshot(
        us_instrument,
        {"earningsGrowth": FundamentalValue(0.2, None, None, None, "yfinance")},
        now, now, "yfinance", "degraded",
    )

    def finnhub(_):
        calls.append("finnhub")
        return ProviderResult.success(primary, "finnhub", now)

    def yfinance(_):
        calls.append("yfinance")
        return ProviderResult.success(supplement, "yfinance", now)

    result = DataRefreshService(
        DataProviders(finnhub_fundamentals=finnhub, yfinance_fundamentals=yfinance),
        calendar,
        DataCache(),
    ).refresh_fundamentals(us_instrument, now)

    assert calls == ["finnhub", "yfinance"]
    assert result.selected_source == "finnhub+yfinance"
    assert result.value is not None
    assert result.value.fields["earningsGrowth"].source == "yfinance"


def test_wrong_instrument_fundamentals_are_rejected_before_fallback(
    us_instrument, now, calendar
) -> None:
    wrong = InstrumentId.from_code("MSFT", Market.US, "XNAS")
    wrong_snapshot = FundamentalSnapshot(
        wrong,
        {
            "pe_ttm": FundamentalValue(20.0, None, None, None, "finnhub"),
            "roe": FundamentalValue(0.2, None, None, None, "finnhub"),
            "revenue_growth": FundamentalValue(0.1, None, None, None, "finnhub"),
            "debt_to_equity": FundamentalValue(0.3, None, None, None, "finnhub"),
        },
        now, now, "finnhub", "ok",
    )
    fallback_snapshot = FundamentalSnapshot(
        us_instrument,
        {"pe_ttm": FundamentalValue(30.0, None, None, None, "yfinance")},
        now, now, "yfinance", "watch",
    )
    calls: list[str] = []

    def primary(_):
        calls.append("finnhub")
        return ProviderResult.success(wrong_snapshot, "finnhub", now)

    def fallback(_):
        calls.append("yfinance")
        return ProviderResult.success(fallback_snapshot, "yfinance", now)

    service = DataRefreshService(
        DataProviders(finnhub_fundamentals=primary, yfinance_fundamentals=fallback), calendar, DataCache()
    )
    result = service.refresh_fundamentals(us_instrument, now)
    assert calls[:2] == ["finnhub", "yfinance"]
    assert result.status is ProviderStatus.OK and result.value is not None
    assert result.value.instrument == us_instrument and result.value.fields["pe_ttm"].value == 30.0


def test_completed_daily_bars_refresh_only_missing_tail(tmp_path, us_instrument, bar_factory, now, calendar) -> None:
    calls: list[tuple[date, date]] = []
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    repo.upsert_daily_bars((bar_factory(us_instrument, date(2026, 7, 8)),))
    def nasdaq(_, start, end):
        calls.append((start, end))
        return ProviderResult.success((bar_factory(us_instrument, date(2026, 7, 9)),), "nasdaq", now)
    service = DataRefreshService(DataProviders(nasdaq_daily=nasdaq), calendar, DataCache(), repo)
    result = service.refresh_daily_bars(us_instrument, date(2026, 7, 8), date(2026, 7, 9), None, now)
    assert calls == [(date(2026, 7, 9), date(2026, 7, 9))]
    assert [bar.trading_date for bar in result.value or ()] == [date(2026, 7, 8), date(2026, 7, 9)]
    repo.close()


def test_refresh_persists_news_and_fundamentals_point_in_time(tmp_path, us_instrument, now, calendar) -> None:
    from tradehelper_v2.contracts import FundamentalSnapshot, FundamentalValue, NewsSnapshot
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    def news(_):
        item = NewsSnapshot(us_instrument, "AAPL fixture", "finnhub", now, now, now, None, False, None, None, None)
        return ProviderResult.success((item,), "finnhub", now)
    def fundamentals(_):
        snapshot = FundamentalSnapshot(us_instrument, {"pe": FundamentalValue(20.0, None, None, None, "finnhub")}, now, now, "finnhub", "ok")
        return ProviderResult.success(snapshot, "finnhub", now)
    service = DataRefreshService(DataProviders(finnhub_news=news, finnhub_fundamentals=fundamentals), calendar, DataCache(), repo)
    service.refresh_news(us_instrument, DecisionMode.EOD, now)
    service.refresh_fundamentals(us_instrument, now)
    assert len(repo.list_news_as_of(us_instrument, now)) == 1
    assert repo.get_fundamentals_as_of(us_instrument, now) is not None
    repo.close()


def test_daily_batch_keeps_over_quota_a_share_explicitly_pending(a_instrument, bar_factory, now, calendar) -> None:
    calls: list[str] = []
    instruments = tuple(InstrumentId.from_code(f"{600000 + number:06d}", Market.A) for number in range(11))
    def tickflow(instrument, _, __):
        calls.append(instrument.code)
        return ProviderResult.success((bar_factory(instrument, date(2026, 7, 9)),), "tickflow", now)
    service = DataRefreshService(DataProviders(tickflow_daily=tickflow), calendar, DataCache())
    requests = tuple(DailyBarsRequest(item, date(2026, 7, 9), date(2026, 7, 9)) for item in instruments)
    result = service.refresh_daily_bars_batch(requests, now)
    assert len(calls) == 10
    pending_result = result.results[instruments[-1]]
    assert pending_result.status is ProviderStatus.RATE_LIMITED
    assert result.pending_retry_at[instruments[-1]] == now + timedelta(minutes=1)


def test_daily_batch_uses_nasdaq_for_all_us_instruments(us_instrument, bar_factory, now, calendar) -> None:
    nasdaq_calls: list[str] = []
    instruments = tuple(InstrumentId.from_code(f"T{number}", Market.US, "XNAS") for number in range(11))
    def nasdaq(instrument, _, __):
        nasdaq_calls.append(instrument.code)
        return ProviderResult.success((bar_factory(instrument, date(2026, 7, 9)),), "nasdaq", now)
    service = DataRefreshService(DataProviders(nasdaq_daily=nasdaq), calendar, DataCache())
    requests = tuple(DailyBarsRequest(item, date(2026, 7, 9), date(2026, 7, 9)) for item in instruments)
    result = service.refresh_daily_bars_batch(requests, now)
    assert nasdaq_calls == [instrument.code for instrument in instruments]
    assert not result.pending_retry_at
    assert result.results[instruments[-1]].selected_source == "nasdaq"


def test_daily_ingress_quarantines_pre_listing_and_non_session_bars(tmp_path, us_instrument, bar_factory, now) -> None:
    from tradehelper_v2.data.calendar import StaticTradingCalendar
    calendar = StaticTradingCalendar((date(2026, 7, 9),), completed_sessions=(date(2026, 7, 9),))
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    def tickflow(_, __, ___):
        return ProviderResult.success(
            (
                bar_factory(us_instrument, date(2026, 7, 8)),
                bar_factory(us_instrument, date(2026, 7, 9)),
                bar_factory(us_instrument, date(2026, 7, 6)),
            ),
            "tickflow", now,
        )
    service = DataRefreshService(DataProviders(tickflow_daily=tickflow), calendar, DataCache(), repo)
    result = service.refresh_daily_bars(us_instrument, date(2026, 7, 1), date(2026, 7, 9), date(2026, 7, 7), now)
    assert [bar.trading_date for bar in result.value or ()] == [date(2026, 7, 9)]
    stored = repo.list_daily_bars(us_instrument, date(2026, 7, 1), date(2026, 7, 9))
    reasons = [row[0] for row in repo._connection.execute("SELECT reason FROM quarantine_records").fetchall()]
    assert [bar.trading_date for bar in stored] == [date(2026, 7, 9)]
    assert "before_listing_date" in reasons
    assert "trading_date_not_in_exchange_calendar" in reasons
    repo.close()
