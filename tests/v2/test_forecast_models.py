from dataclasses import replace
from datetime import date, timedelta

import pytest

from tradehelper_v2.contracts import (
    DecisionMode, FeatureEvidenceMode, FeatureSnapshot, FeatureStatus, FeatureValue,
    ForecastDirection, ForecastScope, ForecastTrainingSample, ModelFamily, ModelSpec, stable_hash,
)
from tradehelper_v2.forecast.feature_sets import TECHNICAL_CORE_V1
from tradehelper_v2.forecast.models import InsufficientRegimeSamples, fit_calibrated_model, fit_model, model_from_artifact, predict_model
from tradehelper_v2.forecast.trainer import default_candidate_specs
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
    assert predict_model(restored, samples[-1].feature_snapshot) == predict_model(trained, samples[-1].feature_snapshot)


def test_fc06_candidate_pool_matches_frozen_twenty_specs() -> None:
    specs = default_candidate_specs()
    assert len(specs) == 20
    assert sum(spec.family is ModelFamily.PROBABILITY_TREE for spec in specs) == 4
    assert sum(spec.family is ModelFamily.ENSEMBLE for spec in specs) == 2
    assert all(spec.hyperparameters.get("min_samples_leaf") == "max(15,2pct)" for spec in specs if spec.family is ModelFamily.PROBABILITY_TREE)


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
    with pytest.raises(InsufficientRegimeSamples):
        predict_model(trained, replace(adjusted[0].feature_snapshot, values=current_values))
