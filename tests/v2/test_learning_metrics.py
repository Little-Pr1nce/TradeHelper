"""LE20-LE27：概率预测事件指标和校准汇总。"""
import pytest
from contracts import DirectionProbabilities, ForecastDirection
from learning.metrics import expected_ece, forecast_event_metrics, strategy_summary

def test_forecast_event_metrics_use_three_class_brier_log_loss_and_interval():
    values=forecast_event_metrics(DirectionProbabilities(.7,.2,.1),ForecastDirection.BULLISH,-.02,.01,.04,.02)
    assert round(values['brier'],2)==.14
    assert values['direction_correct'] and values['interval_hit'] and round(values['absolute_return_error'],2)==.01

def test_ece_ignores_empty_bins_and_uses_max_confidence():
    events=((DirectionProbabilities(.8,.1,.1),ForecastDirection.BULLISH),(DirectionProbabilities(.8,.1,.1),ForecastDirection.BEARISH))
    assert expected_ece(events,10)==pytest.approx(.3)

def test_probability_ties_match_the_forecast_contract_direction_policy():
    values=forecast_event_metrics(
        DirectionProbabilities(.4,.4,.2),
        ForecastDirection.NEUTRAL,
        -.02,
        0,
        .02,
        0,
    )
    assert values["direction_correct"] is True

def test_strategy_metrics_do_not_claim_reliability_before_thirty_fills():
    summary=strategy_summary((.01,-.02,.03))
    assert summary['status']=='unavailable' and summary['sample_count']==3
