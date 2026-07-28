from dataclasses import replace
from datetime import date, timedelta
from hashlib import sha256
import sqlite3

from contracts import (
    ForecastAvailability, ForecastModelVersion, ForecastResult, ForecastScope, ModelFamily,
    ModelLifecycle, ModelSpec, ValidationStatus, stable_hash,
)
from data.migrations.schema import apply_schema
from data.repository import SQLiteRepository
from forecast.models import fit_model
from forecast.registry import ForecastRegistry
from tests.v2.forecast_helpers import synthetic_samples


def test_fc15_migration_six_creates_forecast_persistence_tables(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "forecast.db")
    apply_schema(connection)
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"forecast_model_versions", "forecast_model_evaluations", "forecast_snapshots", "forecast_promotion_events"}.issubset(names)
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=7").fetchone()[0] == 1
    connection.close()


def _model_version(instrument, now):
    samples = synthetic_samples(instrument, count=30)
    spec = ModelSpec("empirical-stock", ModelFamily.EMPIRICAL, "tech", {})
    model = fit_model(spec, samples)
    assert model is not None
    artifact = model.artifact_bytes()
    return ForecastModelVersion(
        version="US:AAPL:h1:empirical:v1", scope=ForecastScope.STOCK,
        scope_key=instrument.stable_key, market=instrument.market, horizon=1, spec=spec,
        lifecycle=ModelLifecycle.CHAMPION, validation_status=ValidationStatus.CONFIRMATION_PASSED,
        training_start=samples[0].origin_session_date, training_end=samples[-1].origin_session_date,
        selection_start=samples[0].origin_session_date, selection_end=samples[9].origin_session_date,
        confirmation_start=samples[10].origin_session_date, confirmation_end=samples[-1].origin_session_date,
        training_data_hash=stable_hash("training"), artifact_format="json+zlib-v1",
        artifact_hash=sha256(artifact).hexdigest(), artifact=artifact, random_seed=7,
        sample_count=30, oof_sample_count=20, created_at=now, promoted_at=now,
    )


def _unavailable_result(instrument, now):
    origin = date(2026, 7, 10); input_hash = stable_hash("input"); version = "unavailable-h1"
    key = "|".join((instrument.stable_key, origin.isoformat(), "calendar-unavailable", "1", version, input_hash))
    return ForecastResult(
        instrument, now, origin, None, 1, 100.0, ForecastAvailability.CALENDAR_UNAVAILABLE,
        None, None, None, None, ForecastScope.BASELINE, instrument.stable_key, ModelFamily.EMPIRICAL,
        version, ModelLifecycle.CANDIDATE, ValidationStatus.INSUFFICIENT_SAMPLE, False, "tech",
        "forecast_feature_sets_v1", input_hash, None, 0, 0, (), "fixture", "calendar unavailable", key, now,
    )


def test_fc15_forecast_result_is_idempotent_across_generation_times(tmp_path, us_instrument, now) -> None:
    repository = SQLiteRepository(tmp_path / "forecast.db")
    result = _unavailable_result(us_instrument, now)
    assert repository.save_forecast_result(result).inserted == 1
    assert repository.save_forecast_result(replace(result, generated_at=now + timedelta(seconds=1))).idempotent == 1
    assert repository.get_forecast_result(result.event_key) == result
    assert repository.list_forecast_results(us_instrument, horizon=1) == (result,)
    repository.close()


def test_fc15_champion_and_evaluations_survive_restart(tmp_path, us_instrument, now) -> None:
    path = tmp_path / "forecast.db"; version = _model_version(us_instrument, now)
    repository = SQLiteRepository(path)
    repository.promote_forecast_model(version)
    assert repository.save_forecast_model_evaluation(
        model_version=version.version, phase="selection", data_hash=stable_hash("selection"),
        payload={"brier": 0.4, "samples": 80}, created_at=now,
    ).inserted == 1
    repository.close()

    reopened = SQLiteRepository(path)
    champions = reopened.list_forecast_champions()
    assert len(champions) == 1 and champions[0].oof_sample_count == 20
    registry = ForecastRegistry(); registry.restore(champions)
    assert registry.champion(
        market=us_instrument.market, scope=ForecastScope.STOCK,
        scope_key=us_instrument.stable_key, horizon=1,
    ) is not None
    evaluations = reopened.list_forecast_model_evaluations(version.version)
    assert evaluations[0]["payload"] == {"brier": 0.4, "samples": 80}
    reopened.close()


def test_latest_oof_validation_verdict_survives_restart(tmp_path, us_instrument, now) -> None:
    path = tmp_path / "forecast.db"
    repository = SQLiteRepository(path)
    first_hash = stable_hash("first")
    latest_hash = stable_hash("latest")
    repository.save_forecast_validation_summary(
        market=us_instrument.market, scope_key=us_instrument.stable_key, horizon=5,
        status=ValidationStatus.CALIBRATION_FAILED, reason="selection calibration failed",
        data_hash=first_hash,
        created_at=now,
    )
    repository.save_forecast_validation_summary(
        market=us_instrument.market, scope_key=us_instrument.stable_key, horizon=5,
        status=ValidationStatus.EVALUATED_NOT_BETTER, reason="candidate did not beat baseline",
        data_hash=latest_hash,
        created_at=now + timedelta(seconds=1),
    )
    repository.close()

    reopened = SQLiteRepository(path)
    try:
        assert reopened.list_latest_forecast_validations() == ({
            "market": "US", "scope_key": us_instrument.stable_key, "horizon": 5,
            "status": "evaluated_not_better", "reason": "candidate did not beat baseline",
            "data_hash": latest_hash, "created_at": now + timedelta(seconds=1),
        },)
    finally:
        reopened.close()


def test_candidate_oof_metrics_do_not_require_a_fake_model_version(tmp_path, us_instrument, now) -> None:
    repository = SQLiteRepository(tmp_path / "forecast.db")
    data_hash = stable_hash("candidate-training")
    try:
        result = repository.save_forecast_candidate_evaluation(
            market=us_instrument.market, scope=ForecastScope.STOCK,
            scope_key=us_instrument.stable_key, horizon=1,
            spec_id="logistic-tech-c0.1", phase="selection", data_hash=data_hash,
            payload={"candidate": {"brier": 0.42}, "baseline": {"brier": 0.44}},
            created_at=now,
        )
        assert result.inserted == 1
        assert repository.get_forecast_model_version("logistic-tech-c0.1") is None
        assert repository.list_forecast_candidate_evaluations(
            market=us_instrument.market, scope_key=us_instrument.stable_key, horizon=1,
        )[0]["payload"]["candidate"]["brier"] == 0.42
    finally:
        repository.close()
