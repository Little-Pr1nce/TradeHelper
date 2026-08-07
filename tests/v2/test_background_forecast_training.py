from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from application.background import BackgroundForecastTrainingService
from contracts import ForecastScope, ValidationStatus
from data.repository import SQLiteRepository
from forecast.registry import ForecastRegistry
from forecast.trainer import TrainingOutcome
from tests.v2.forecast_helpers import synthetic_samples
from tests.v2.test_forecast_repository import _model_version


class _FailedTrainer:
    def evaluate(self, samples, *, scope, scope_key, horizon, panel_samples=()):
        return TrainingOutcome(
            scope, scope_key, horizon, ValidationStatus.EVALUATED_NOT_BETTER,
            (), None, None, "fresh OOF did not beat baseline",
        )


def test_fresh_oof_failure_retires_stale_persisted_and_runtime_champion(
    tmp_path, us_instrument, now,
) -> None:
    repository = SQLiteRepository(tmp_path / "background-retire.db")
    version = _model_version(us_instrument, now)
    registry = ForecastRegistry()
    repository.promote_forecast_model(version)
    registry.promote(version)
    samples = tuple(
        replace(sample, horizon=(1, 3, 5, 10)[index % 4])
        for index, sample in enumerate(synthetic_samples(us_instrument, count=180))
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        service = BackgroundForecastTrainingService(
            repository, _FailedTrainer(), registry, executor,
        )
        statuses = service.result(service.submit(us_instrument, samples), timeout=10)
    try:
        assert statuses[1] == ValidationStatus.EVALUATED_NOT_BETTER.value
        assert repository.list_forecast_champions() == ()
        assert registry.champion(
            market=us_instrument.market, scope=ForecastScope.STOCK,
            scope_key=us_instrument.stable_key, horizon=1,
        ) is None
    finally:
        repository.close()
