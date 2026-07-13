from datetime import date, timedelta

from tradehelper_v2.contracts import DataCapabilities, DataQualityIssue, Market, QualitySeverity
from tradehelper_v2.contracts.enums import DecisionMode, FreshnessStatus, QualityStatus
from tradehelper_v2.data.quality import _finalize, evaluate_data_quality


def test_g13_price_jump_is_warning_not_ohlc_failure(us_instrument, bar_factory, now, calendar) -> None:
    start = date(2026, 1, 1)
    bars = [bar_factory(us_instrument, start + timedelta(days=index), 10.0) for index in range(120)]
    bars[-2] = bar_factory(us_instrument, start + timedelta(days=118), 10.0)
    bars[-1] = bar_factory(us_instrument, start + timedelta(days=119), 18.0)
    report = evaluate_data_quality(
        bars, market=Market.US, mode=DecisionMode.EOD, as_of=now, listing_date=start,
        news_available=True, fundamentals_available=True,
    )
    assert report.score == 92.0 and report.status is QualityStatus.OK
    assert {issue.code for issue in report.issues} == {"PRICE_JUMP_REVIEW"}


def test_g41_sample_capabilities(us_instrument, bar_factory, now) -> None:
    start = date(2026, 1, 1)
    for count, expected in ((0, (False, False, False, False)), (1, (True, False, False, False)), (20, (True, True, False, False)), (60, (True, True, True, False)), (120, (True, True, True, True))):
        bars = [bar_factory(us_instrument, start + timedelta(days=index)) for index in range(count)]
        report = evaluate_data_quality(bars, market=Market.US, mode=DecisionMode.EOD, as_of=now, listing_date=start, news_available=True, fundamentals_available=True)
        capabilities = report.capabilities
        assert (capabilities.daily_price, capabilities.short_technical_20, capabilities.medium_technical_60, capabilities.ma120) == expected


def test_g42_quality_score_rules(now) -> None:
    capabilities = DataCapabilities()
    warning = DataQualityIssue("W", QualitySeverity.WARNING, "a", "warning", None)
    optional_a = DataQualityIssue("OA", QualitySeverity.OPTIONAL_MISSING, "a", "missing", None)
    optional_b = DataQualityIssue("OB", QualitySeverity.OPTIONAL_MISSING, "b", "missing", None)
    report = _finalize((warning, optional_a, optional_b), capabilities, now)
    assert (report.score, report.status.value, report.max_position_multiplier) == (84.0, "watch", 0.8)
    blocked = _finalize((DataQualityIssue("B", QualitySeverity.BLOCK, None, "block", None),), capabilities, now)
    assert (blocked.score, blocked.status.value, blocked.block_new_entries, blocked.max_position_multiplier) == (65.0, "blocked", True, 0.0)
    degraded = _finalize(tuple(DataQualityIssue(str(index), QualitySeverity.WARNING, None, "warn", None) for index in range(4)), capabilities, now)
    assert (degraded.score, degraded.status.value, degraded.max_position_multiplier) == (68.0, "degraded", 0.5)


def test_g43_portfolio_quality_isolated(us_instrument, quote_factory, bar_factory, now) -> None:
    from tradehelper_v2.contracts import InstrumentId
    aapl = evaluate_data_quality([bar_factory(us_instrument, date(2026, 7, 9))], market=Market.US, mode=DecisionMode.INTRADAY, as_of=now, quote=quote_factory(us_instrument), listing_date=date(2000, 1, 1))
    mu = InstrumentId.from_code("MU", Market.US, "XNAS")
    stale = evaluate_data_quality([bar_factory(mu, date(2026, 7, 9))], market=Market.US, mode=DecisionMode.INTRADAY, as_of=now, quote=quote_factory(mu, observed_at=now - timedelta(minutes=16)), listing_date=date(2000, 1, 1))
    assert aapl.capabilities.realtime_price is True
    assert "REALTIME_STALE" in {issue.code for issue in stale.issues}


def test_g30_quote_freshness_boundaries(us_instrument, quote_factory, now) -> None:
    from tradehelper_v2.data.quality import assess_quote_freshness
    assert assess_quote_freshness(quote_factory(us_instrument, observed_at=now - timedelta(minutes=15)), DecisionMode.INTRADAY, now).freshness_status.value == "fresh"
    assert assess_quote_freshness(quote_factory(us_instrument, observed_at=now - timedelta(minutes=15, seconds=1)), DecisionMode.INTRADAY, now).freshness_status.value == "stale"
    assert assess_quote_freshness(quote_factory(us_instrument, observed_at=now - timedelta(minutes=45)), DecisionMode.PRE, now).freshness_status.value == "fresh"
    assert assess_quote_freshness(quote_factory(us_instrument, observed_at=now + timedelta(minutes=5)), DecisionMode.INTRADAY, now).freshness_status.value == "fresh"
    assert assess_quote_freshness(quote_factory(us_instrument, observed_at=now + timedelta(minutes=5, seconds=1)), DecisionMode.INTRADAY, now).freshness_status.value == "future"


def test_quote_without_provider_timestamp_is_never_promoted_to_fresh(us_instrument, quote_factory, bar_factory, now) -> None:
    quote = quote_factory(us_instrument, freshness_status=FreshnessStatus.MISSING_TIMESTAMP)
    report = evaluate_data_quality(
        [bar_factory(us_instrument, date(2026, 7, 9))], market=Market.US,
        mode=DecisionMode.INTRADAY, as_of=now, quote=quote, listing_date=date(2000, 1, 1),
    )
    assert report.capabilities.realtime_price is False
    assert "REALTIME_TIMESTAMP_MISSING" in {issue.code for issue in report.issues}
