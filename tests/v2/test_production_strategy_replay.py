from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from tests.v2.forecast_helpers import synthetic_samples
from application.background import BackgroundStrategyReplayService
from learning.production_replay import HistoricalStrategyReplayer


def _four_horizon_samples(instrument):
    values = []
    for sample in synthetic_samples(instrument, count=300):
        for horizon in (1, 3, 5, 10):
            values.append(replace(
                sample,
                target_session_date=sample.origin_session_date + timedelta(days=horizon),
                horizon=horizon,
                matured_at=sample.origin_session_date + timedelta(days=horizon),
            ))
    return tuple(values)


def test_strategy_replay_builds_three_sequential_embargoed_folds(us_instrument):
    samples = _four_horizon_samples(us_instrument)
    folds = HistoricalStrategyReplayer._fold_origins(samples)
    assert len(folds) == 3
    assert all(len(testing) == 40 for _, testing in folds)
    origins = HistoricalStrategyReplayer._complete_origins(samples)
    indexes = {day: index for index, day in enumerate(origins)}
    assert all(indexes[testing[0]] - indexes[train_end] == 11 for train_end, testing in folds)
    assert all(left[1][-1] < right[1][0] for left, right in zip(folds, folds[1:]))


def test_joint_replay_metrics_are_stock_and_profile_scoped(us_instrument, now):
    values = tuple(
        SimpleNamespace(
            profile=profile,
            time_weighted_return=result,
            benchmark_return=Decimal("0.03"),
            max_drawdown=drawdown,
            sharpe=Decimal("1.2"),
            generated_at=now,
        )
        for profile, result, drawdown in (
            ("conservative", Decimal("0.02"), Decimal("-0.01")),
            ("conservative", Decimal("-0.01"), Decimal("-0.03")),
            ("aggressive", Decimal("0.04"), Decimal("-0.05")),
        )
    )
    snapshots = HistoricalStrategyReplayer._joint_metric_snapshots(us_instrument, values)
    by_scope = {item.scope_key: item for item in snapshots}
    conservative = by_scope[f"{us_instrument.stable_key}:joint:conservative"]
    assert conservative.sample_count == 2
    assert dict(conservative.metrics)["mean_net_return"] == 0.005
    assert dict(conservative.metrics)["max_drawdown"] == -0.03
    assert dict(conservative.metrics)["win_rate"] == 0.5


def test_background_strategy_replay_is_idempotent_for_same_bar_batch(
    us_instrument, bar_factory, now,
):
    samples = _four_horizon_samples(us_instrument)
    bars = (bar_factory(us_instrument, now.date()),)

    class Replayer:
        calls = 0

        def run(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                outcomes=(), fold_count=3, tested_origins=120, filled_count=0,
                joint_outcomes=(), metric_snapshots=(), validation_statuses=(),
            )

    class Repository:
        def save_strategy_outcome(self, outcome):
            raise AssertionError("empty replay must not save an outcome")

        def save_joint_outcome(self, outcome):
            raise AssertionError("empty replay must not save a joint outcome")

        def save_learning_metric_snapshot(self, snapshot):
            raise AssertionError("empty replay must not save a metric snapshot")

    replayer = Replayer()
    with ThreadPoolExecutor(max_workers=1) as executor:
        service = BackgroundStrategyReplayService(Repository(), replayer, executor)
        first = service.submit(us_instrument, bars, samples)
        second = service.submit(us_instrument, bars, samples)
        assert first == second
        service.result(first, timeout=2)
    assert replayer.calls == 1
