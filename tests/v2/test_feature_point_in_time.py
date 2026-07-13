from __future__ import annotations

from datetime import timedelta

import pytest

from tradehelper_v2.contracts import CanonicalBar, ContractViolation, FeatureEvidenceMode, FreshnessStatus, NewsSnapshot, ProviderStatus
from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.features import FeatureBuilder

from feature_helpers import bars, calendar, inputs


def test_f04_quote_only_changes_current_features(tmp_path, us_instrument, now, quote_factory) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    builder = FeatureBuilder(calendar(values))
    first = builder.build(inputs(us_instrument, now, values, quote=quote_factory(us_instrument, price=150.0, open=145.0, low=140.0, high=170.0, freshness_status=FreshnessStatus.FRESH)))
    second = builder.build(inputs(us_instrument, now, values, quote=quote_factory(us_instrument, price=160.0, open=145.0, low=140.0, high=170.0, freshness_status=FreshnessStatus.FRESH)))
    first_closed = {item.name: item.value for item in first.values if item.name.startswith("closed.")}
    second_closed = {item.name: item.value for item in second.values if item.name.startswith("closed.")}
    assert first_closed == second_closed
    assert builder.closed_input_hash(inputs(us_instrument, now, values, quote=quote_factory(us_instrument, price=150.0, open=145.0, low=140.0, high=170.0, freshness_status=FreshnessStatus.FRESH))) == builder.closed_input_hash(inputs(us_instrument, now, values, quote=quote_factory(us_instrument, price=160.0, open=145.0, low=140.0, high=170.0, freshness_status=FreshnessStatus.FRESH)))
    assert first.feature_hash != second.feature_hash
    assert dict((item.name, item.value) for item in first.values)["current.price"] == 150.0
    repository = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    try:
        repository.upsert_daily_bars(values)
        before = repository._connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        builder.build(inputs(us_instrument, now, values, quote=quote_factory(us_instrument, price=150.0, open=145.0, low=140.0, high=170.0, freshness_status=FreshnessStatus.FRESH)))
        assert repository._connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0] == before
    finally:
        repository.close()


def test_f04_quote_freshness_is_rechecked_for_the_current_cutoff(us_instrument, now, quote_factory) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    stale_quote = quote_factory(
        us_instrument,
        observed_at=now - timedelta(hours=24),
        freshness_status=FreshnessStatus.FRESH,
    )
    snapshot = FeatureBuilder(calendar(values)).build(
        inputs(us_instrument, now, values, mode="intraday", quote=stale_quote)
    )
    current_price = {item.name: item for item in snapshot.values}["current.price"]
    assert current_price.value is None
    assert current_price.status.value == "stale"
    assert current_price.reason == "QUOTE_STALE"


def test_f05_news_is_not_visible_before_available_at(us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    item = NewsSnapshot(us_instrument, "late", "fixture", now - timedelta(minutes=31), now + timedelta(minutes=30), now,
                        None, False, "positive", 0.8, None)
    builder = FeatureBuilder(calendar(values))
    before = builder.build(inputs(us_instrument, now + timedelta(minutes=15), values, news=(item,), news_status=ProviderStatus.OK))
    after = builder.build(inputs(us_instrument, now + timedelta(minutes=31), values, news=(item,), news_status=ProviderStatus.OK))
    assert {item.name: item.value for item in before.values}["news.count_1d"] == 0
    assert {item.name: item.value for item in after.values}["news.count_1d"] == 1


def test_f05_later_news_refresh_keeps_earliest_available_time(tmp_path, us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    published, first_available = now - timedelta(minutes=31), now + timedelta(minutes=30)
    first = NewsSnapshot(us_instrument, "stable", "fixture", published, first_available, first_available,
                         None, False, "positive", 0.8, None)
    later = NewsSnapshot(us_instrument, "stable", "fixture", published, now + timedelta(hours=2), now + timedelta(hours=2),
                         "refreshed", False, "positive", 0.8, None)
    repository = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    try:
        repository.upsert_news((first,))
        repository.upsert_news((later,))
        builder = FeatureBuilder(calendar(values))
        early = builder.build(inputs(us_instrument, now + timedelta(minutes=15), values, news=repository.list_news_as_of(us_instrument, now + timedelta(minutes=15)), news_status=ProviderStatus.OK))
        visible = builder.build(inputs(us_instrument, now + timedelta(minutes=31), values, news=repository.list_news_as_of(us_instrument, now + timedelta(minutes=31)), news_status=ProviderStatus.OK))
        assert {item.name: item.value for item in early.values}["news.count_1d"] == 0
        assert {item.name: item.value for item in visible.values}["news.count_1d"] == 1
    finally:
        repository.close()


def test_f05b_evidence_mode_is_explicit_and_hashed(us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    builder = FeatureBuilder(calendar(values))
    reconstructed = builder.build(inputs(us_instrument, now, values, evidence_mode=FeatureEvidenceMode.RECONSTRUCTED_HISTORY))
    observed_input = inputs(us_instrument, now, values, evidence_mode=FeatureEvidenceMode.OBSERVED_SNAPSHOT)
    with pytest.raises(ContractViolation, match="OBSERVED_INPUT_EVIDENCE_UNVERIFIED"):
        builder.build(observed_input)
    observed = FeatureBuilder(calendar(values), observed_input_verifier=lambda _: True).build(observed_input)
    assert reconstructed.evidence_mode is FeatureEvidenceMode.RECONSTRUCTED_HISTORY
    assert reconstructed.input_hash != observed.input_hash
