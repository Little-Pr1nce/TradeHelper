from datetime import date, timedelta
from types import SimpleNamespace
from time import perf_counter

from tradehelper_v2.contracts import ForecastScope
from tradehelper_v2.forecast.trainer import ForecastTrainer
from tests.v2.forecast_helpers import synthetic_samples


def test_fc18_cancellation_stops_before_candidate_fit(us_instrument) -> None:
    samples = tuple(SimpleNamespace(horizon=1, scope_membership={ForecastScope.STOCK: us_instrument.stable_key}, instrument=us_instrument, origin_session_date=date(2025, 1, 1) + timedelta(days=index), target_session_date=date(2025, 1, 2) + timedelta(days=index)) for index in range(81))
    outcome = ForecastTrainer().evaluate(samples, scope=ForecastScope.STOCK, scope_key=us_instrument.stable_key, horizon=1, cancelled=lambda: True)
    assert outcome.reason == "cancelled" and outcome.champion is None


def test_fc18_five_hundred_points_four_horizons_complete_under_thirty_seconds(us_instrument) -> None:
    progress = []
    started = perf_counter()
    samples = tuple(sample for horizon in (1, 3, 5, 10) for sample in synthetic_samples(us_instrument, count=500, horizon=horizon, extended=True))
    outcomes = tuple(
        ForecastTrainer().evaluate(
            samples, scope=ForecastScope.STOCK, scope_key=us_instrument.stable_key, horizon=horizon,
            progress=lambda phase, completed, total: progress.append((phase, completed, total)),
        )
        for horizon in (1, 3, 5, 10)
    )
    elapsed = perf_counter() - started
    assert elapsed < 30.0
    assert progress and progress[-1][1] == progress[-1][2]
    assert all(outcome.evaluations for outcome in outcomes)
