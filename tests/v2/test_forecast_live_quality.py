from datetime import date, datetime, timezone
from types import SimpleNamespace

from application.analysis import RuntimeAnalysisPipeline
from forecast.live_quality import live_forecast_verdict
from test_scenario_planner import _forecast


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


def _outcome(index, *, correct, actual, brier):
    return SimpleNamespace(
        forecast_outcome_id=f"outcome-{index}",
        target_session_date=date(2026, 7, 1),
        generated_at=NOW,
        status=SimpleNamespace(value="matured"),
        direction_correct=correct,
        actual_direction=SimpleNamespace(value=actual),
        event_brier=brier,
    )


def test_live_gate_defers_to_oof_before_twenty_matured_events():
    outcomes = tuple(
        _outcome(index, correct=False, actual="bullish", brier=.9)
        for index in range(19)
    )
    verdict = live_forecast_verdict(outcomes)
    assert verdict.execution_allowed
    assert verdict.reason == "LIVE_TRACK_RECORD_SAMPLE_INSUFFICIENT"


def test_live_gate_suspends_directionally_weak_and_poorly_calibrated_horizon():
    outcomes = tuple(
        _outcome(
            index,
            correct=index < 6,
            actual="bullish" if index < 16 else "bearish",
            brier=.72,
        )
        for index in range(20)
    )
    verdict = live_forecast_verdict(outcomes)
    assert not verdict.execution_allowed
    assert verdict.reason == "LIVE_TRACK_RECORD_BELOW_BASELINE"
    assert verdict.direction_accuracy == .3
    assert verdict.majority_baseline_accuracy == .8


def test_live_gate_keeps_probability_model_that_is_useful_despite_majority_class():
    outcomes = tuple(
        _outcome(
            index,
            correct=index < 13,
            actual="bullish" if index < 14 else "bearish",
            brier=.50,
        )
        for index in range(20)
    )
    verdict = live_forecast_verdict(outcomes)
    assert verdict.execution_allowed
    assert verdict.reason == "LIVE_TRACK_RECORD_ACCEPTABLE"


def test_runtime_pipeline_removes_execution_eligibility_but_keeps_forecast_visible(us_instrument):
    outcomes = tuple(
        _outcome(
            index,
            correct=index < 6,
            actual="bullish" if index < 16 else "bearish",
            brier=.72,
        )
        for index in range(20)
    )
    repository = SimpleNamespace(list_market_forecast_outcomes=lambda *args, **kwargs: outcomes)
    pipeline = RuntimeAnalysisPipeline(SimpleNamespace(repository=repository))
    forecast = _forecast(us_instrument, 1, "bullish", confirmed=True)
    assert forecast.execution_eligible
    gated = pipeline._apply_live_forecast_gate(us_instrument, (forecast,))[0]
    assert gated.availability == forecast.availability
    assert gated.probabilities == forecast.probabilities
    assert not gated.execution_eligible
    assert gated.reason == "LIVE_TRACK_RECORD_BELOW_BASELINE"
    assert gated.model_version == f"{forecast.model_version}:live-observation-v1"
    assert gated.event_key != forecast.event_key
    assert gated.event_key.endswith(
        f"{gated.model_version}|{forecast.model_input_hash}"
    )


def test_live_gate_deduplicates_repeated_runs_of_the_same_forecast():
    repeated = tuple(
        SimpleNamespace(
            **vars(_outcome(index, correct=False, actual="bullish", brier=.9)),
            instrument=SimpleNamespace(stable_key="US:NASDAQ:AAPL"),
            origin_session_date=date(2026, 6, 30),
            horizon=1,
        )
        for index in range(25)
    )
    verdict = live_forecast_verdict(repeated)
    assert verdict.execution_allowed
    assert verdict.sample_count == 1
    assert verdict.reason == "LIVE_TRACK_RECORD_SAMPLE_INSUFFICIENT"
