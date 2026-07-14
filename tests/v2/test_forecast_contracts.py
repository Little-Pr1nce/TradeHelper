from dataclasses import replace
from datetime import date

import pytest

from tradehelper_v2.contracts import (
    AdjustmentMode, CanonicalBar, ContractViolation, DecisionMode, DirectionProbabilities,
    FeatureEvidenceMode, FeatureSnapshot, FeatureStatus, FeatureValue, ForecastRequest,
    ForecastAvailability, ForecastResult, ForecastScope, ModelFamily, ModelLifecycle,
    ReturnDistribution, ValidationStatus, stable_hash,
)
from tests.v2.forecast_helpers import synthetic_samples


def _snapshot(instrument, now, day=date(2026, 7, 10)):
    values = (FeatureValue("closed.realized_vol_20", .2, FeatureStatus.AVAILABLE, None, 20, now, ("fixture",), True, None),)
    return FeatureSnapshot(instrument, DecisionMode.EOD, now, day, None, "2.2.0", FeatureEvidenceMode.RECONSTRUCTED_HISTORY, values, stable_hash("input"), stable_hash("features"), now)


def test_fc00_contract_invariants(us_instrument, now, bar_factory) -> None:
    with pytest.raises(ContractViolation):
        DirectionProbabilities(.4, .4, .4)
    with pytest.raises(ContractViolation):
        ReturnDistribution(.1, -.1, .2, "empirical")
    snapshot = _snapshot(us_instrument, now)
    bar = bar_factory(us_instrument, snapshot.latest_bar_date)
    with pytest.raises(ContractViolation):
        ForecastRequest(snapshot, bar, now, (2,))
    raw = CanonicalBar(us_instrument, date(2026, 7, 9), 99, 101, 98, 100, 1, AdjustmentMode.FRONT_ADJUSTED, "fixture", now)
    with pytest.raises(ContractViolation):
        ForecastRequest(snapshot, raw, now)


def test_fc02_request_is_eod_and_horizon_normalized(us_instrument, now, bar_factory) -> None:
    snapshot = _snapshot(us_instrument, now); request = ForecastRequest(snapshot, bar_factory(us_instrument, snapshot.latest_bar_date), now, (10, 1, 1))
    assert request.horizons == (1, 10)


def test_fc00_training_sample_rejects_non_eod_and_inconsistent_labels(us_instrument) -> None:
    sample = synthetic_samples(us_instrument, count=1)[0]
    with pytest.raises(ContractViolation):
        replace(sample, feature_snapshot=replace(sample.feature_snapshot, mode=DecisionMode.INTRADAY))
    with pytest.raises(ContractViolation):
        replace(sample, future_return=sample.future_return + 0.01)
    with pytest.raises(ContractViolation):
        replace(sample, scope_membership={ForecastScope.STOCK: us_instrument.stable_key, ForecastScope.INDUSTRY: "US:tech"})
    accepted = replace(
        sample,
        scope_membership={ForecastScope.STOCK: us_instrument.stable_key, ForecastScope.INDUSTRY: "US:tech"},
        scope_membership_available_at={ForecastScope.INDUSTRY: sample.feature_snapshot.cutoff_at},
    )
    assert accepted.scope_membership[ForecastScope.INDUSTRY] == "US:tech"


def test_fc00_calendar_unavailable_result_has_no_fake_target(us_instrument, now) -> None:
    origin = date(2026, 7, 10)
    input_hash = stable_hash("input")
    version = "unavailable-h1"
    key = "|".join((us_instrument.stable_key, origin.isoformat(), "calendar-unavailable", "1", version, input_hash))
    result = ForecastResult(
        us_instrument, now, origin, None, 1, 100.0, ForecastAvailability.CALENDAR_UNAVAILABLE,
        None, None, None, None, ForecastScope.BASELINE, us_instrument.stable_key, ModelFamily.EMPIRICAL,
        version, ModelLifecycle.CANDIDATE, ValidationStatus.INSUFFICIENT_SAMPLE, False, "tech",
        "forecast_feature_sets_v1", input_hash, None, 0, 0, (), "fixture", "calendar unavailable", key, now,
    )
    assert result.target_session_date is None
    with pytest.raises(ContractViolation):
        replace(result, target_session_date=date(2026, 7, 11))
