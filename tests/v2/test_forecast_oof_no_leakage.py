from datetime import date
from types import SimpleNamespace

from contracts import ModelFamily, ModelSpec, ForecastScope, ValidationStatus
from forecast.trainer import ForecastTrainer, selection_split_index
from forecast.labels import matured_samples
from tests.v2.forecast_helpers import synthetic_samples


def test_fc04_only_matured_labels_enter_oof_training() -> None:
    origin = SimpleNamespace(origin_session_date=date(2026, 1, 10))
    matured = SimpleNamespace(origin_session_date=date(2026, 1, 4), target_session_date=date(2026, 1, 10))
    immature = SimpleNamespace(origin_session_date=date(2026, 1, 6), target_session_date=date(2026, 1, 11))
    assert matured_samples((matured, immature), origin) == (matured,)


def test_fc07_selection_confirmation_never_split_same_date() -> None:
    records = []
    for day, count in ((date(2026, 1, 1), 40), (date(2026, 1, 2), 40), (date(2026, 1, 3), 40)):
        records.extend((SimpleNamespace(origin_session_date=day), None, None) for _ in range(count))
    split = selection_split_index(records)
    assert records[split - 1][0].origin_session_date < records[split][0].origin_session_date


def test_fc08_equal_to_baseline_is_not_reported_as_insufficient(us_instrument) -> None:
    spec = ModelSpec("empirical-candidate", ModelFamily.EMPIRICAL, "tech", {})
    outcome = ForecastTrainer(candidate_specs=(spec,)).evaluate(
        synthetic_samples(us_instrument, count=180),
        scope=ForecastScope.STOCK,
        scope_key=us_instrument.stable_key,
        horizon=1,
    )
    assert outcome.status is ValidationStatus.EVALUATED_NOT_BETTER


def test_fc10_predictable_series_promotes_champion(us_instrument) -> None:
    spec = ModelSpec("analog-tech-k40", ModelFamily.ANALOG, "tech", {"k": 40}, complexity_rank=1)
    outcome = ForecastTrainer(candidate_specs=(spec,)).evaluate(
        synthetic_samples(us_instrument), scope=ForecastScope.STOCK,
        scope_key=us_instrument.stable_key, horizon=1,
    )
    assert outcome.status is ValidationStatus.CONFIRMATION_PASSED
    assert outcome.champion is not None and outcome.champion.oof_sample_count >= 60


def test_fc11_random_series_is_deterministic_and_not_promoted(us_instrument) -> None:
    spec = ModelSpec("logistic-tech-c0.1", ModelFamily.MULTINOMIAL_LOGISTIC, "tech", {"C": 0.1}, complexity_rank=1)
    samples = synthetic_samples(us_instrument, predictable=False)
    first = ForecastTrainer(candidate_specs=(spec,)).evaluate(
        samples, scope=ForecastScope.STOCK, scope_key=us_instrument.stable_key, horizon=1,
    )
    second = ForecastTrainer(candidate_specs=(spec,)).evaluate(
        samples, scope=ForecastScope.STOCK, scope_key=us_instrument.stable_key, horizon=1,
    )
    assert first.status is ValidationStatus.EVALUATED_NOT_BETTER and first.champion is None
    assert second.status is first.status
    assert second.evaluations[0].selection == first.evaluations[0].selection


def test_primary_brier_metric_ranks_before_stability_tiebreaker(monkeypatch) -> None:
    lower_brier = (
        SimpleNamespace(spec_id="lower", complexity_rank=2),
        SimpleNamespace(selection=SimpleNamespace(
            multiclass_brier=.50, log_loss=.90, expected_calibration_error=.10,
        )),
    )
    higher_brier = (
        SimpleNamespace(spec_id="higher", complexity_rank=1),
        SimpleNamespace(selection=SimpleNamespace(
            multiclass_brier=.51, log_loss=.80, expected_calibration_error=.05,
        )),
    )
    monkeypatch.setattr(
        ForecastTrainer,
        "_selection_stability_score",
        staticmethod(lambda _baseline, values: 100.0 if values == "unstable" else 0.0),
    )
    records = {"lower": "unstable", "higher": "stable"}
    assert ForecastTrainer._selection_rank(lower_brier, (), records) < ForecastTrainer._selection_rank(higher_brier, (), records)
