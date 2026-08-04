"""UX20--UX29: evaluation thresholds, filtering, charts, and ledger separation."""
from dataclasses import replace
from datetime import timedelta

import pytest

from presentation_helpers import _forecasts, forecast_outcome, single_presentation
from application.evaluation import HistoricalEvaluationService, IssuedForecastRecord, maturity_message
from contracts import ContractViolation, DecisionMode, EvidenceOrigin, HistoricalEvaluationQuery, LedgerKind, LedgerViewKind, LearningMetricSnapshot, Market, OutcomeStatus, ReportKind, stable_hash


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


def test_history_view_exposes_plain_forecast_and_strategy_summaries(us_instrument, now):
    view = HistoricalEvaluationService().build(
        HistoricalEvaluationQuery(Market.US),
        outcomes=(forecast_outcome(us_instrument, now=now),),
        built_at=now,
    )
    tables={table.table_id:table for table in view.tables}
    assert tables["forecast_performance_summary"].rows[0].cells[0]=="AAPL"
    assert tables["strategy_performance_summary"].empty_state
    assert tables["strategy_exit_quality_summary"].empty_state
    assert "预测是否准确" in tables["forecast_performance_summary"].interpretation


def test_strategy_curve_requires_joint_account_replay(now):
    view = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), built_at=now)
    assert not view.charts[2].series
    assert "退出质量不是账户收益" in view.charts[2].interpretation


def test_default_details_use_recent_30_days_without_narrowing_all_history_summary(us_instrument, now):
    old = forecast_outcome(us_instrument, now=now - timedelta(days=40))
    default_view = HistoricalEvaluationService().build(
        HistoricalEvaluationQuery(Market.US, LedgerViewKind.FORECAST),
        outcomes=(old,), built_at=now,
    )
    tables = {table.table_id: table for table in default_view.tables}
    assert len(tables["forecast_performance_summary"].rows) == 1
    assert not tables["forecast_event_details"].rows
    assert "最近30天" in tables["forecast_event_details"].title

    filtered_view = HistoricalEvaluationService().build(
        HistoricalEvaluationQuery(
            Market.US, LedgerViewKind.FORECAST,
            date_from=now - timedelta(days=50), date_to=now - timedelta(days=30),
        ),
        outcomes=(old,), built_at=now,
    )
    filtered = {table.table_id: table for table in filtered_view.tables}
    assert len(filtered["forecast_event_details"].rows) == 1
    assert "筛选区间" in filtered["forecast_event_details"].title


def test_repository_history_evaluation_loads_real_sqlite_read_path(tmp_path, now):
    from application.evaluation import RepositoryHistoricalEvaluationService
    from data.repository import SQLiteRepository
    repository=SQLiteRepository(tmp_path/"history.sqlite")
    try:
        view=RepositoryHistoricalEvaluationService(repository,clock=lambda:now).load(
            HistoricalEvaluationQuery(Market.US)
        )
        assert dict(view.maturity_summary)["matured_count"]==0
        assert {table.table_id for table in view.tables}>={
            "forecast_performance_summary","strategy_performance_summary",
        }
    finally:
        repository.close()


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


def test_scoped_metric_matches_instrument_and_horizon_but_not_unknown_mode(us_instrument, now):
    metrics = (("brier", .2),)
    scope = f"{us_instrument.stable_key}:h1:formal_model"
    identity = {"ledger": LedgerKind.FORECAST, "scope": scope, "cutoff": now, "sample_count": 30, "metrics": metrics}
    snapshot = LearningMetricSnapshot(stable_hash(identity), LedgerKind.FORECAST, scope, now, 30, metrics, now)
    matching = HistoricalEvaluationService().build(
        HistoricalEvaluationQuery(Market.US, instrument=us_instrument, horizon=1),
        metrics=(snapshot,), built_at=now,
    )
    mode_specific = HistoricalEvaluationService().build(
        HistoricalEvaluationQuery(Market.US, instrument=us_instrument, horizon=1, analysis_mode=DecisionMode.PRE),
        metrics=(snapshot,), built_at=now,
    )
    assert len(matching.tables[4].rows) == 1
    assert not mode_specific.tables[4].rows


def test_issued_forecasts_keep_modes_separate_and_only_latest_revision_counts(us_instrument, now):
    result = _forecasts(us_instrument)[0]
    base = forecast_outcome(us_instrument, now=now)
    reasons = base.reason_codes
    identity = {
        "forecast_event_key": result.event_key,
        "origin": EvidenceOrigin.ISSUED_ONLINE,
        "maturity": base.maturity_evidence_id,
        "status": OutcomeStatus.MATURED,
        "actual_return": base.actual_return,
        "revision": base.maturity_evidence_id,
        "reasons": reasons,
    }
    outcome = replace(
        base, forecast_outcome_id=stable_hash(identity), forecast_event_key=result.event_key,
        origin_session_date=result.origin_session_date, target_session_date=result.target_session_date,
        horizon=result.horizon,
    )
    issued = (
        IssuedForecastRecord(result, outcome, DecisionMode.PRE, ReportKind.SINGLE_STOCK, "pre-old", now-timedelta(minutes=10)),
        IssuedForecastRecord(result, outcome, DecisionMode.PRE, ReportKind.PORTFOLIO, "pre-new", now),
        IssuedForecastRecord(result, outcome, DecisionMode.EOD, ReportKind.PORTFOLIO, "eod", now-timedelta(hours=8)),
    )
    view = HistoricalEvaluationService().build(
        HistoricalEvaluationQuery(Market.US, LedgerViewKind.FORECAST, us_instrument, 1, analysis_mode=DecisionMode.PRE),
        outcomes=(outcome,), issued_forecasts=issued, built_at=now,
    )
    tables = {table.table_id: table for table in view.tables}
    rows = tables["issued_forecast_details"].rows
    assert len(rows) == 1
    assert rows[0].cells[1:3] == ("盘前", "我的持仓")
    assert rows[0].cells[0].endswith("美东时间")
    mode_rows = {row.cells[0]: row.cells[1] for row in tables["mode_forecast_summary"].rows}
    assert mode_rows["盘前"] == "1" and mode_rows["盘后"] == "1"

    portfolio_view = HistoricalEvaluationService().build(
        HistoricalEvaluationQuery(
            Market.US, LedgerViewKind.FORECAST, us_instrument, 1,
            analysis_mode=DecisionMode.PRE, report_kind=ReportKind.PORTFOLIO,
        ),
        outcomes=(outcome,), issued_forecasts=issued, built_at=now,
    )
    portfolio_rows = next(
        table.rows for table in portfolio_view.tables
        if table.table_id == "issued_forecast_details"
    )
    assert len(portfolio_rows) == 1
    assert portfolio_rows[0].cells[2] == "我的持仓"
    portfolio_tables = {table.table_id: table for table in portfolio_view.tables}
    assert portfolio_tables["forecast_performance_summary"].rows[0].cells[0] == "AAPL"
    assert dict(portfolio_view.maturity_summary)["matured_count"] == 1
    assert portfolio_tables["ledger_summary"].rows[0].cells[1:3] == ("1", "1")


@pytest.mark.parametrize("instrument_fixture", ("us_instrument", "a_instrument"))
def test_repository_links_frozen_report_to_issued_forecast_and_outcome(
    tmp_path, request, instrument_fixture, now, calendar,
):
    from application.evaluation import RepositoryHistoricalEvaluationService
    from data.repository import SQLiteRepository
    from presentation.report_builder import SingleStockReportBuilder

    instrument = request.getfixturevalue(instrument_fixture)
    presentation = single_presentation(instrument, now=now, calendar=calendar)
    document = SingleStockReportBuilder().build(presentation)
    forecast = presentation.forecasts[0]
    base = forecast_outcome(instrument, now=now)
    identity = {
        "forecast_event_key": forecast.event_key,
        "origin": EvidenceOrigin.ISSUED_ONLINE,
        "maturity": base.maturity_evidence_id,
        "status": OutcomeStatus.MATURED,
        "actual_return": base.actual_return,
        "revision": base.maturity_evidence_id,
        "reasons": base.reason_codes,
    }
    outcome = replace(
        base,
        forecast_outcome_id=stable_hash(identity),
        forecast_event_key=forecast.event_key,
        origin_session_date=forecast.origin_session_date,
        target_session_date=forecast.target_session_date,
        horizon=forecast.horizon,
    )
    repository = SQLiteRepository(tmp_path / "issued-history.sqlite")
    try:
        repository.save_forecast_result(forecast)
        repository.save_forecast_outcome(outcome)
        repository.save_report_document(document)
        view = RepositoryHistoricalEvaluationService(repository, clock=lambda: now).load(
            HistoricalEvaluationQuery(
                instrument.market,
                LedgerViewKind.FORECAST,
                instrument,
                forecast.horizon,
                analysis_mode=document.analysis_mode,
            )
        )
        issued = next(table for table in view.tables if table.table_id == "issued_forecast_details")
        assert len(issued.rows) == 1
        assert issued.rows[0].cells[3] == instrument.code
        assert issued.rows[0].cells[-1] == "正确"
    finally:
        repository.close()
