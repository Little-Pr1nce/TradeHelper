from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tradehelper_v2.contracts import ContractViolation, DecisionMode, FeatureInputs, FundamentalSnapshot, FundamentalValue, NewsSnapshot, ProviderStatus
from tradehelper_v2.features import FeatureBuilder

from feature_helpers import bars, calendar, inputs


def test_f00_contract_hashes_are_stable_and_sensitive(us_instrument, now) -> None:
    values = bars(us_instrument, 40, fetched_at=now)
    builder = FeatureBuilder(calendar(values))
    base = builder.build(inputs(us_instrument, now, values), generated_at=now)
    repeated = builder.build(inputs(us_instrument, now, values), generated_at=now + timedelta(minutes=1))
    assert base.input_hash == repeated.input_hash and base.feature_hash == repeated.feature_hash
    assert tuple(item.name for item in base.values) == tuple(sorted(item.name for item in base.values))
    assert len({item.name for item in base.values}) == len(base.values)
    assert builder.build(inputs(us_instrument, now, values, mode=DecisionMode.PRE), generated_at=now).input_hash != base.input_hash
    assert builder.build(inputs(us_instrument, now + timedelta(minutes=1), values), generated_at=now).input_hash != base.input_hash
    news = NewsSnapshot(us_instrument, "hash", "fixture", now - timedelta(minutes=5), now, now, None, False, None, None, None)
    assert builder.build(inputs(us_instrument, now, values, news=(news,), news_status=ProviderStatus.OK)).input_hash != base.input_hash
    fundamentals = FundamentalSnapshot(us_instrument, {"pe_ttm": FundamentalValue(20.0, "multiple", None, None, "finnhub")}, now, now, "finnhub", "ok")
    assert builder.build(inputs(us_instrument, now, values, fundamentals=fundamentals, fundamentals_status=ProviderStatus.OK)).input_hash != base.input_hash
    assert FeatureBuilder(calendar(values), feature_set_version="2.2.1").build(inputs(us_instrument, now, values)).feature_hash != base.feature_hash
    altered = values[:-1] + (replace(values[-1], close=values[-1].close + 1.0, high=values[-1].high + 1.0),)
    assert FeatureBuilder(calendar(altered)).build(inputs(us_instrument, now, altered)).input_hash != base.input_hash


def test_f00_generated_at_records_build_time_not_fact_cutoff(us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    before = datetime.now(timezone.utc)
    snapshot = FeatureBuilder(calendar(values)).build(inputs(us_instrument, now, values))
    after = datetime.now(timezone.utc)
    assert before <= snapshot.generated_at <= after


def test_f00_duplicate_trading_dates_are_rejected(us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    base = inputs(us_instrument, now, values)
    with pytest.raises(ContractViolation, match="duplicate trading dates"):
        FeatureInputs(
            instrument=base.instrument,
            mode=base.mode,
            cutoff_at=base.cutoff_at,
            bars=values + (values[-1],),
            quote=base.quote,
            news=base.news,
            news_status=base.news_status,
            fundamentals=base.fundamentals,
            fundamentals_status=base.fundamentals_status,
            data_quality=base.data_quality,
            evidence_mode=base.evidence_mode,
        )
