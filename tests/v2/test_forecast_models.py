from dataclasses import replace
from datetime import date, timedelta

import pytest

from contracts import (
    DecisionMode, FeatureEvidenceMode, FeatureSnapshot, FeatureStatus, FeatureValue,
    ForecastDirection, ForecastScope, ForecastTrainingSample, ModelFamily, ModelSpec, stable_hash,
)
from forecast.feature_sets import TECHNICAL_CORE_V1
from forecast.models import (
    fit_calibrated_model, fit_model, fit_panel_calibrated_model,
    model_from_artifact, predict_model, with_stock_return_history,
)
from forecast.trainer import default_candidate_specs
from tests.v2.forecast_helpers import synthetic_samples


def _sample(instrument, now, offset: int):
    origin = date(2025, 1, 1) + timedelta(days=offset)
    values = tuple(FeatureValue(name, float((offset + index) % 7) / 10, FeatureStatus.AVAILABLE, None, 20, now, ("fixture",), True, None) for index, name in enumerate(TECHNICAL_CORE_V1))
    feature_hash = stable_hash((instrument.stable_key, origin, offset))
    snapshot = FeatureSnapshot(instrument, DecisionMode.EOD, now, origin, None, "2.2.0", FeatureEvidenceMode.RECONSTRUCTED_HISTORY, values, stable_hash(("input", offset)), feature_hash, now)
    direction = (ForecastDirection.BULLISH, ForecastDirection.NEUTRAL, ForecastDirection.BEARISH)[offset % 3]
    future_return = (.03, 0., -.03)[offset % 3]
    return ForecastTrainingSample(instrument, {ForecastScope.STOCK: instrument.stable_key}, origin, origin + timedelta(days=1), 1, 100., 100. * (1 + future_return), future_return, .01, direction, snapshot, feature_hash, FeatureEvidenceMode.RECONSTRUCTED_HISTORY, origin + timedelta(days=1))


def test_fc16_artifact_is_json_zlib_and_rejects_corruption(us_instrument, now) -> None:
    samples = tuple(_sample(us_instrument, now, index) for index in range(18))
    spec = ModelSpec("logistic-tech", ModelFamily.MULTINOMIAL_LOGISTIC, "tech", {"C": .1})
    trained = fit_model(spec, samples)
    assert trained is not None and len(trained.artifact_hash) == 64
    restored = model_from_artifact(spec, trained.artifact_bytes())
    assert predict_model(restored, samples[-1].feature_snapshot)[0].to_dict() == predict_model(trained, samples[-1].feature_snapshot)[0].to_dict()
    with pytest.raises(Exception):
        model_from_artifact(spec, trained.artifact_bytes()[:-2])


def test_fc10_analog_counts_are_normalized_after_weighting(us_instrument) -> None:
    samples = synthetic_samples(us_instrument, count=180)
    spec = ModelSpec("analog-tech-k40", ModelFamily.ANALOG, "tech", {"k": 40})
    trained = fit_model(spec, samples[:150])
    assert trained is not None
    probabilities, _ = predict_model(trained, samples[151].feature_snapshot)
    assert max(probabilities.bullish, probabilities.neutral, probabilities.bearish) > 0.95


def test_fc10_calibration_is_used_and_persisted(us_instrument) -> None:
    samples = synthetic_samples(us_instrument)
    spec = ModelSpec("analog-tech-k40", ModelFamily.ANALOG, "tech", {"k": 40})
    trained = fit_calibrated_model(spec, samples)
    assert trained is not None and trained.temperature != 1.0
    restored = model_from_artifact(spec, trained.artifact_bytes())
    assert restored.temperature == trained.temperature
    assert restored.payload["_prior_shrinkage"] == trained.payload["_prior_shrinkage"]
    assert restored.payload["_calibration_prior"] == trained.payload["_calibration_prior"]
    assert predict_model(restored, samples[-1].feature_snapshot) == predict_model(trained, samples[-1].feature_snapshot)


def test_empirical_model_calibrates_interval_instead_of_skipping_calibration(us_instrument) -> None:
    samples = synthetic_samples(us_instrument, count=180)
    spec = ModelSpec("empirical", ModelFamily.EMPIRICAL, "tech", {})
    raw = fit_model(spec, samples)
    trained = fit_calibrated_model(spec, samples)
    assert raw is not None and trained is not None
    assert "_interval_scale" in trained.payload
    assert trained.temperature == 1.0
    assert "_prior_shrinkage" not in trained.payload
    raw_probability, _ = predict_model(raw, samples[-1].feature_snapshot)
    calibrated_probability, _ = predict_model(trained, samples[-1].feature_snapshot)
    assert calibrated_probability == raw_probability


def test_fc06_candidate_pool_matches_frozen_twenty_specs() -> None:
    specs = default_candidate_specs()
    assert len(specs) == 20
    assert sum(spec.family is ModelFamily.PROBABILITY_TREE for spec in specs) == 3
    assert sum(spec.family is ModelFamily.PROBABILITY_FOREST for spec in specs) == 1
    assert sum(spec.family is ModelFamily.EMPIRICAL for spec in specs) == 2
    assert sum(spec.hyperparameters.get("training_scope") == "market_panel" for spec in specs) == 3
    assert sum(spec.family is ModelFamily.ENSEMBLE for spec in specs) == 2
    assert all(spec.hyperparameters.get("min_samples_leaf") == "max(15,2pct)" for spec in specs if spec.family is ModelFamily.PROBABILITY_TREE)


def test_probability_forest_artifact_is_deterministic_json_and_predicts(us_instrument) -> None:
    samples = synthetic_samples(us_instrument, count=180)
    spec = ModelSpec(
        "forest-tech-d4", ModelFamily.PROBABILITY_FOREST, "tech",
        {"n_estimators": 8, "max_depth": 4, "max_features": 0.7},
    )
    first = fit_model(spec, samples[:150], random_seed=7)
    second = fit_model(spec, samples[:150], random_seed=7)
    assert first is not None and second is not None
    assert first.artifact_bytes() == second.artifact_bytes()
    restored = model_from_artifact(spec, first.artifact_bytes())
    probabilities, interval = predict_model(restored, samples[151].feature_snapshot)
    assert probabilities.bullish + probabilities.neutral + probabilities.bearish == pytest.approx(1.0)
    assert interval.p10 <= interval.p50 <= interval.p90


def test_panel_direction_model_uses_target_stock_return_scale(us_instrument) -> None:
    samples = synthetic_samples(us_instrument, count=180)
    spec = ModelSpec("panel-logistic", ModelFamily.MULTINOMIAL_LOGISTIC, "tech", {"C": .1, "training_scope": "market_panel"})
    trained = fit_calibrated_model(spec, samples)
    assert trained is not None
    narrow = tuple(
        replace(
            sample, future_return=sample.future_return * .1,
            target_price=sample.reference_price * (1 + sample.future_return * .1),
            flat_band=sample.flat_band * .1,
        )
        for sample in samples
    )
    stock_scaled = with_stock_return_history(trained, narrow)
    assert stock_scaled.payload.get("_interval_scale") == trained.payload.get("_interval_scale")
    assert max(abs(value) for value in stock_scaled.training_returns) < max(abs(value) for value in trained.training_returns)


def test_panel_model_is_calibrated_with_target_stock_history(us_instrument) -> None:
    from contracts import InstrumentId, Market

    target = synthetic_samples(us_instrument, count=180)
    panel = []
    for code in ("AAPL", "AMD", "AVGO", "FCX", "NVDA"):
        panel.extend(synthetic_samples(InstrumentId.from_code(code, Market.US), count=180))
    spec = ModelSpec(
        "panel-logistic", ModelFamily.MULTINOMIAL_LOGISTIC, "tech",
        {"C": .1, "training_scope": "market_panel"},
    )
    trained = fit_panel_calibrated_model(spec, tuple(sorted(
        panel, key=lambda sample: (sample.origin_session_date, sample.instrument.stable_key),
    )), target)
    assert trained is not None
    assert trained.training_returns == tuple(sample.future_return for sample in target)
    assert "_interval_scale" in trained.payload
    assert "_prior_shrinkage" in trained.payload


def test_panel_logistic_can_shrink_direction_toward_stock_prior(us_instrument) -> None:
    samples = synthetic_samples(us_instrument, count=120)
    plain_spec = ModelSpec("plain", ModelFamily.MULTINOMIAL_LOGISTIC, "tech", {"C": .1})
    blend_spec = ModelSpec(
        "blend", ModelFamily.MULTINOMIAL_LOGISTIC, "tech",
        {"C": .1, "empirical_blend": .5},
    )
    plain = fit_model(plain_spec, samples[:90])
    blended = fit_model(blend_spec, samples[:90])
    assert plain is not None and blended is not None
    plain_probability, _ = predict_model(plain, samples[91].feature_snapshot)
    blend_probability, _ = predict_model(blended, samples[91].feature_snapshot)
    prior = 1 / 3
    assert abs(blend_probability.bullish - prior) <= abs(plain_probability.bullish - prior)


def test_fc06_regime_analog_filters_to_same_technical_state(us_instrument) -> None:
    adjusted = []
    for index, sample in enumerate(synthetic_samples(us_instrument, count=120)):
        trend, volatility = ((0.1, 0.1) if index < 60 else (-0.1, 0.5))
        values = tuple(
            replace(value, value=trend if value.name == "closed.ma_distance_20" else volatility if value.name == "closed.realized_vol_20" else value.value)
            for value in sample.feature_snapshot.values
        )
        feature_hash = stable_hash(("regime", index))
        snapshot = replace(sample.feature_snapshot, values=values, feature_hash=feature_hash)
        adjusted.append(replace(sample, feature_snapshot=snapshot, feature_hash=feature_hash))
    spec = ModelSpec("regime-tech-k40", ModelFamily.REGIME_ANALOG, "tech", {"k": 40})
    trained = fit_model(spec, tuple(adjusted))
    assert trained is not None
    assert set(trained.payload["regime"]["regimes"]) == {"up:low", "down_or_flat:mid"}
    current_values = tuple(
        replace(value, value=0.1 if value.name == "closed.ma_distance_20" else 0.3 if value.name == "closed.realized_vol_20" else value.value)
        for value in adjusted[0].feature_snapshot.values
    )
    probabilities, interval = predict_model(trained, replace(adjusted[0].feature_snapshot, values=current_values))
    assert probabilities.bullish + probabilities.neutral + probabilities.bearish == pytest.approx(1.0)
    assert interval.p10 <= interval.p50 <= interval.p90
