from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

import pytest

from contracts import DataCapabilities, DataQualityIssue, DataQualityReport, FundamentalSnapshot, FundamentalValue, ProviderStatus, QualityAction, QualitySeverity, QualityStatus
from data.providers import parse_finnhub_fundamentals
from features import FeatureBuilder

from feature_helpers import bars, calendar, inputs


def test_f06_news_empty_and_failure_have_different_semantics(us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    builder = FeatureBuilder(calendar(values))
    empty = builder.build(inputs(us_instrument, now, values, news_status=ProviderStatus.EMPTY))
    failed = builder.build(inputs(us_instrument, now, values, news_status=ProviderStatus.UNAVAILABLE))
    empty_values, failed_values = ({item.name: item for item in snapshot.values} for snapshot in (empty, failed))
    assert empty_values["news.count_30d"].value == 0
    assert empty_values["news.sentiment_weighted_1d"].value is None
    assert empty_values["news.scored_ratio_30d"].value is None
    assert empty_values["news.scored_ratio_30d"].reason == "NEWS_PROVIDER_EMPTY"
    assert failed_values["news.count_30d"].value is None and failed_values["news.count_30d"].reason == "NEWS_PROVIDER_UNAVAILABLE"


def test_f07_fundamentals_respect_available_and_published_times(us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    future = FundamentalSnapshot(us_instrument, {"pe_ttm": FundamentalValue(20.0, "multiple", None, None, "finnhub")}, now + timedelta(hours=1), now, "finnhub", "ok")
    builder = FeatureBuilder(calendar(values))
    before = builder.build(inputs(us_instrument, now, values, fundamentals=future, fundamentals_status=ProviderStatus.OK))
    after = builder.build(inputs(us_instrument, now + timedelta(hours=2), values, fundamentals=future, fundamentals_status=ProviderStatus.OK))
    assert {item.name: item.value for item in before.values}["fund.pe_ttm"] is None
    assert {item.name: item.value for item in after.values}["fund.pe_ttm"] == 20.0
    late_field = FundamentalSnapshot(us_instrument, {"pe_ttm": FundamentalValue(20.0, "multiple", None, now + timedelta(hours=1), "finnhub")}, now, now, "finnhub", "ok")
    assert {item.name: item.value for item in builder.build(inputs(us_instrument, now, values, fundamentals=late_field, fundamentals_status=ProviderStatus.OK)).values}["fund.pe_ttm"] is None


def test_f08_units_are_explicitly_normalized(us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    snapshot = FundamentalSnapshot(us_instrument, {
        "roe": FundamentalValue(20.0, "percent", None, None, "finnhub"),
        "gross_margin": FundamentalValue(0.2, "ratio", None, None, "baostock"),
        "revenueGrowth": FundamentalValue(0.2, "ratio", None, None, "yfinance"),
        "debt_ratio": FundamentalValue(20.0, "unknown", None, None, "akshare"),
    }, now, now, "fixture", "ok")
    result = FeatureBuilder(calendar(values)).build(inputs(us_instrument, now, values, fundamentals=snapshot, fundamentals_status=ProviderStatus.OK))
    features = {item.name: item for item in result.values}
    assert features["fund.roe"].value == 0.2 and features["fund.gross_margin"].value == 0.2 and features["fund.revenue_growth_yoy"].value == 0.2
    assert features["fund.debt_ratio"].value is None and features["fund.debt_ratio"].reason == "FUND_UNIT_UNSUPPORTED"


def test_f08_real_provider_payloads_reach_canonical_features(a_instrument, us_instrument, now) -> None:
    fixture = Path(__file__).parent / "fixtures" / "providers" / "finnhub_fundamentals.json"
    finnhub = parse_finnhub_fundamentals(json.loads(fixture.read_text(encoding="utf-8")), us_instrument, now)
    finnhub_values = {
        item.name: item
        for item in FeatureBuilder(calendar(bars(us_instrument, 30, fetched_at=now))).build(
            inputs(us_instrument, now, bars(us_instrument, 30, fetched_at=now), fundamentals=finnhub,
                   fundamentals_status=ProviderStatus.OK)
        ).values
    }
    assert finnhub_values["fund.pe_ttm"].value == 31.2

    yfinance = parse_finnhub_fundamentals(
        {"trailingPE": 31.2, "returnOnEquity": 0.2, "grossMargins": 0.4,
         "revenueGrowth": 0.1, "earningsGrowth": 0.08, "debtToEquity": 120.0},
        us_instrument,
        now,
        provider="yfinance",
    )
    yfinance_values = {
        item.name: item
        for item in FeatureBuilder(calendar(bars(us_instrument, 30, fetched_at=now))).build(
            inputs(us_instrument, now, bars(us_instrument, 30, fetched_at=now), fundamentals=yfinance,
                   fundamentals_status=ProviderStatus.OK)
        ).values
    }
    assert yfinance_values["fund.pe_ttm"].value == 31.2
    assert yfinance_values["fund.roe"].value == 0.2
    assert yfinance_values["fund.gross_margin"].value == 0.4
    assert yfinance_values["fund.revenue_growth_yoy"].value == 0.1
    assert yfinance_values["fund.net_profit_growth_yoy"].value == 0.08
    assert yfinance_values["fund.debt_ratio"].value is None

    baostock = parse_finnhub_fundamentals(
        json.loads((fixture.parent / "baostock_fundamentals.json").read_text(encoding="utf-8")),
        a_instrument,
        now,
        provider="baostock",
    )
    a_values = bars(a_instrument, 30, fetched_at=now)
    baostock_values = {
        item.name: item
        for item in FeatureBuilder(calendar(a_values)).build(
            inputs(a_instrument, now, a_values, fundamentals=baostock, fundamentals_status=ProviderStatus.OK)
        ).values
    }
    assert baostock_values["fund.roe"].value == 0.2
    assert baostock_values["fund.debt_ratio"].value == 0.3

    akshare = parse_finnhub_fundamentals(
        {"fields": {
            "净资产收益率(%)": {"value": 20.0, "unit": None, "source": "akshare"},
            "销售毛利率(%)": {"value": 40.0, "unit": None, "source": "akshare"},
            "主营业务收入增长率(%)": {"value": 10.0, "unit": None, "source": "akshare"},
            "净利润增长率(%)": {"value": 8.0, "unit": None, "source": "akshare"},
            "资产负债率(%)": {"value": 30.0, "unit": None, "source": "akshare"},
        }},
        a_instrument,
        now,
        provider="akshare",
    )
    akshare_values = {
        item.name: item
        for item in FeatureBuilder(calendar(a_values)).build(
            inputs(a_instrument, now, a_values, fundamentals=akshare, fundamentals_status=ProviderStatus.OK)
        ).values
    }
    assert akshare_values["fund.roe"].value == 0.2
    assert akshare_values["fund.gross_margin"].value == 0.4
    assert akshare_values["fund.revenue_growth_yoy"].value == 0.1
    assert akshare_values["fund.net_profit_growth_yoy"].value == 0.08
    assert akshare_values["fund.debt_ratio"].value == 0.3


def test_f08_primary_source_and_period_priority_do_not_depend_on_field_order(us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    mixed = FundamentalSnapshot(us_instrument, {
        "trailingPE": FundamentalValue(99.0, None, None, None, "yfinance"),
        "peNormalizedAnnual": FundamentalValue(41.0, None, None, None, "finnhub"),
        "peTTM": FundamentalValue(38.0, None, None, None, "finnhub"),
        "priceToBook": FundamentalValue(99.0, None, None, None, "yfinance"),
        "pbQuarterly": FundamentalValue(34.0, None, None, None, "finnhub"),
        "pb": FundamentalValue(43.0, None, None, None, "finnhub"),
        "returnOnEquity": FundamentalValue(0.9, None, None, None, "yfinance"),
        "roeRfy": FundamentalValue(151.0, None, None, None, "finnhub"),
        "roeTTM": FundamentalValue(146.0, None, None, None, "finnhub"),
        "grossMargins": FundamentalValue(0.9, None, None, None, "yfinance"),
        "grossMarginAnnual": FundamentalValue(46.0, None, None, None, "finnhub"),
        "grossMarginTTM": FundamentalValue(47.0, None, None, None, "finnhub"),
        "revenueGrowth": FundamentalValue(0.9, None, None, None, "yfinance"),
        "revenueGrowthQuarterlyYoy": FundamentalValue(16.0, None, None, None, "finnhub"),
        "revenueGrowthTTMYoy": FundamentalValue(12.0, None, None, None, "finnhub"),
    }, now, now, "finnhub+yfinance", "ok")
    snapshot = FeatureBuilder(calendar(values)).build(
        inputs(us_instrument, now, values, fundamentals=mixed, fundamentals_status=ProviderStatus.OK)
    )
    features = {item.name: item for item in snapshot.values}
    assert features["fund.pe_ttm"].value == 38.0
    assert features["fund.pb_mrq"].value == 43.0
    assert features["fund.roe"].value == pytest.approx(1.46)
    assert features["fund.gross_margin"].value == pytest.approx(0.47)
    assert features["fund.revenue_growth_yoy"].value == pytest.approx(0.12)
    assert all(features[name].sources == ("finnhub",) for name in (
        "fund.pe_ttm", "fund.pb_mrq", "fund.roe", "fund.gross_margin", "fund.revenue_growth_yoy"
    ))


def test_f08_valid_fallback_is_used_when_primary_candidate_is_invalid(us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    mixed = FundamentalSnapshot(us_instrument, {
        "pe_ttm": FundamentalValue(20.0, "unsupported", None, None, "finnhub"),
        "trailingPE": FundamentalValue(30.0, None, None, None, "yfinance"),
    }, now, now, "finnhub+yfinance", "watch")
    snapshot = FeatureBuilder(calendar(values)).build(
        inputs(us_instrument, now, values, fundamentals=mixed, fundamentals_status=ProviderStatus.OK)
    )
    pe = {item.name: item for item in snapshot.values}["fund.pe_ttm"]
    assert pe.value == 30.0 and pe.sources == ("yfinance",)


def test_f08_a_share_uses_field_specific_source_semantics(a_instrument, now) -> None:
    values = bars(a_instrument, 30, fetched_at=now)
    mixed = FundamentalSnapshot(a_instrument, {
        "roe": FundamentalValue(0.34462, "ratio", None, None, "baostock"),
        "weighted_roe_annual": FundamentalValue(32.53, "percent", None, None, "akshare"),
        "gross_margin": FundamentalValue(0.9118, "ratio", None, None, "baostock"),
        "gross_margin_annual": FundamentalValue(91.18, "percent", None, None, "akshare"),
        "revenue_yoy_annual": FundamentalValue(-1.21, "percent", None, None, "akshare"),
    }, now, now, "baostock+akshare", "ok")

    snapshot = FeatureBuilder(calendar(values)).build(
        inputs(a_instrument, now, values, fundamentals=mixed, fundamentals_status=ProviderStatus.OK)
    )
    features = {item.name: item for item in snapshot.values}

    assert features["fund.roe"].value == pytest.approx(0.3253)
    assert features["fund.roe"].sources == ("akshare",)
    assert features["fund.revenue_growth_yoy"].value == pytest.approx(-0.0121)
    assert features["fund.revenue_growth_yoy"].sources == ("akshare",)
    assert features["fund.gross_margin"].value == pytest.approx(0.9118)
    assert features["fund.gross_margin"].sources == ("baostock",)


def test_f10_new_stock_keeps_valid_short_history(us_instrument, now) -> None:
    values = bars(us_instrument, 18, fetched_at=now, volume=0)
    result = FeatureBuilder(calendar(values)).build(inputs(us_instrument, now, values))
    features = {item.name: item for item in result.values}
    assert result.latest_bar_date == values[-1].trading_date
    assert features["closed.ma_20"].status.value == "insufficient_history"


def test_f10_zero_volume_quality_downgrades_only_volume_features(us_instrument, now) -> None:
    values = bars(us_instrument, 21, fetched_at=now)
    issue = DataQualityIssue("ZERO_VOLUME_RATIO_HIGH", QualitySeverity.WARNING, "volume", "zero volume", "fixture")
    quality = DataQualityReport(QualityStatus.WATCH, QualityAction.WATCH, 92, 0.8, False, (issue,), DataCapabilities(daily_price=True), now)
    snapshot = FeatureBuilder(calendar(values)).build(inputs(us_instrument, now, values, data_quality=quality))
    volume = {item.name: item for item in snapshot.values}["closed.volume_ratio_20"]
    assert volume.value is not None and not volume.model_eligible and volume.reason == "ZERO_VOLUME_RATIO_HIGH"
