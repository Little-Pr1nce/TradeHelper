"""UX20--UX29: evaluation thresholds, filtering, charts, and ledger separation."""
from dataclasses import replace
from datetime import timedelta

import pytest

from presentation_helpers import forecast_outcome
from application.evaluation import HistoricalEvaluationService, maturity_message
from contracts import ContractViolation, HistoricalEvaluationQuery, LedgerKind, LedgerViewKind, LearningMetricSnapshot, Market, stable_hash


def test_ux20_maturity_thresholds():
    assert [maturity_message(x) for x in (0, 1, 10, 30)] == ["暂无已到期记录。", "样本积累中，不评价可靠性。", "可作观察，不允许模型优劣定论。", "样本达到比较门槛，仍需结合分层、区间和回撤。"]


def test_ux21_metric_direction_explained(now):
    view = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), built_at=now)
    assert "校准" in view.charts[0].interpretation


def test_ux22_chart_has_baseline_labels(now):
    chart = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), built_at=now).charts[0]
    assert chart.x_axis and chart.y_axis and "基线" in chart.interpretation


def test_ux23_timeline_links_prediction_to_correct_outcome(us_instrument, now):
    outcome = forecast_outcome(us_instrument, now=now)
    view = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), outcomes=(outcome,), built_at=now)
    assert view.charts[1].series[-1][1][0][1] == .02
    assert view.tables[1].rows[0].cells[0] == "AAPL"


def test_ux24_invalid_target_order_is_rejected(us_instrument, now):
    outcome = forecast_outcome(us_instrument, now=now)
    with pytest.raises(ContractViolation):
        replace(outcome, target_session_date=outcome.origin_session_date - timedelta(days=1))


def test_ux25_single_mature_record_warns_not_reliable(us_instrument, now):
    view = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), outcomes=(forecast_outcome(us_instrument, now=now),), built_at=now)
    assert "不能" in view.warnings[0]


def test_ux26_regime_slice_retains_regime_and_sample_count(us_instrument, now):
    view = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), outcomes=(forecast_outcome(us_instrument, now=now),), built_at=now)
    assert view.tables[3].rows[0].cells[:2] == ("risk_on", "1")


def test_ux27_empty_ledgers_do_not_mix(now):
    view = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.A), outcomes=(), metrics=(), built_at=now)
    assert dict(view.maturity_summary)["matured_count"] == 0


def test_ux28_all_four_ledgers_remain_separate(us_instrument, now):
    view = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), outcomes=(forecast_outcome(us_instrument, now=now),), built_at=now)
    rows = {row.cells[0]: row.cells[1] for row in view.tables[0].rows}
    assert rows == {"forecast": "1", "strategy": "0", "joint": "0", "research": "0"}


def test_ux29_empty_history_has_no_fake_curve(now):
    chart = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), built_at=now).charts[0]
    assert chart.series == () and chart.empty_state


def test_empty_performance_charts_keep_financial_axes(now):
    view = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), built_at=now)
    assert view.charts[2].y_axis == "累计收益" and view.charts[3].y_axis == "回撤"


def test_filters_market_instrument_horizon_and_date(us_instrument, a_instrument, now):
    selected = forecast_outcome(us_instrument, now=now)
    other_market = forecast_outcome(a_instrument, now=now)
    query = HistoricalEvaluationQuery(Market.US, LedgerViewKind.FORECAST, us_instrument, 1, date_from=now-timedelta(minutes=1), date_to=now+timedelta(minutes=1))
    view = HistoricalEvaluationService().build(query, outcomes=(selected, other_market), built_at=now)
    assert dict(view.maturity_summary)["matured_count"] == 1


def test_dimensionless_metric_is_not_mixed_into_horizon_slice(us_instrument, now):
    metrics = (("brier", .2),)
    identity = {"ledger": LedgerKind.FORECAST, "scope": us_instrument.stable_key, "cutoff": now, "sample_count": 30, "metrics": metrics}
    snapshot = LearningMetricSnapshot(stable_hash(identity), LedgerKind.FORECAST, us_instrument.stable_key, now, 30, metrics, now)
    query = HistoricalEvaluationQuery(Market.US, horizon=1)
    view = HistoricalEvaluationService().build(query, metrics=(snapshot,), built_at=now)
    assert not view.tables[4].rows
