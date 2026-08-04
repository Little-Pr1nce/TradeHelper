"""V2-11 fixed Golden Cases backed by real frozen V2 contracts."""
from decimal import Decimal
from types import SimpleNamespace

from presentation_helpers import (
    forecast_outcome, portfolio_presentation, research_validation_states, single_document, single_presentation,
)
from strategy_helpers import position
from application.evaluation import HistoricalEvaluationService
from application.tasks import AnalysisTaskCoordinator
from contracts import (
    AvailabilitySource, HistoricalEvaluationQuery, InstrumentId, Market, PlanAction,
    PositionAvailability,
)
from presentation.renderers import render_html, render_markdown
from presentation.report_builder import PortfolioReportBuilder
from presentation.report_builder import _portfolio_plan_rows, _share_count, _signal_profile_text


def _document(instrument, now, calendar, **options):
    return single_document(instrument, now=now, calendar=calendar, **options)


def _section(document, key):
    return next(item for item in document.sections if item.section_id == key)


def _plan_detail(document):
    return next(block.payload for block in _section(document, "operation_report").blocks if block.payload.table_id == "plan_table")


def test_golden_aapl_forecast_states_execution_impact(us_instrument, now, calendar):
    table = _section(_document(us_instrument, now, calendar), "forecast").blocks[0].payload
    assert table.columns[-1] == "能否影响新开仓" and all(row.cells[-1] for row in table.rows)


def test_golden_fcx_plan_has_reduce_and_reentry_branches(us_instrument, now, calendar):
    instrument = InstrumentId.from_code("FCX", Market.US, "XNYS")
    options = {
        "position": position(instrument, cost="90"),
        "directions": {1: "bearish", 3: "bearish", 5: "bullish", 10: "bullish"},
        "reference_price": 99,
        "feature_overrides": {"closed.ma_120": 98, "closed.ma_60": 95, "closed.ma_distance_120": 99/98-1, "closed.ma_distance_60": 99/95-1},
    }
    presentation = single_presentation(instrument, now=now, calendar=calendar, **options)
    assert any(plan.action.value == "add" for plan in presentation.strategy_bundle.entry_or_add.plans)
    assert any(plan.action.value == "reduce" for plan in presentation.strategy_bundle.reduce_or_exit.plans)
    rows = _plan_detail(_document(instrument, now, calendar, **options)).rows
    assert any(row.cells[0] == "买入/加仓" and row.cells[1] == "加仓" for row in rows)
    assert any(row.cells[0] == "卖出/减仓" and row.cells[1] == "减仓" for row in rows)


def test_golden_glw_plan_preserves_invalidation(us_instrument, now, calendar):
    instrument = InstrumentId.from_code("GLW", Market.US, "XNYS")
    document = _document(instrument, now, calendar, position=position(instrument, cost="80"))
    rows = _plan_detail(document).rows
    assert any(row.cells[0] == "卖出/减仓" and row.cells[1] == "减仓" and row.cells[2] == "已触发" for row in rows)
    assert "失效" in {row.cells[0] for row in rows}


def test_golden_wdc_history_does_not_override_current_risk(us_instrument, now, calendar):
    instrument = InstrumentId.from_code("WDC", Market.US, "XNAS")
    document = _document(instrument, now, calendar, position=position(instrument, cost="80"), directions={1:"bearish",3:"bearish",5:"bearish",10:"bearish"})
    table = _section(document, "history").blocks[0].payload
    assert "不会覆盖当前风险动作" in table.interpretation
    rows = _plan_detail(document).rows
    assert "卖出" in rows[0].cells or any(row.cells[1] == "卖出" for row in rows)


def test_golden_a_share_uses_same_report_chain(a_instrument, now, calendar):
    held = position(a_instrument, shares="100", cost="120")
    availability = PositionAvailability(a_instrument, Decimal("100"), Decimal("0"), held.captured_at, AvailabilitySource.USER, ())
    presentation = single_presentation(a_instrument, now=now, calendar=calendar, position=held, reference_price=100, availability=availability)
    assert any("RISK_T1_BLOCKED" in item.reason_codes for item in presentation.risk_bundle.decisions if item.action.value in {"reduce", "sell"})
    document = _document(a_instrument, now, calendar, position=held, reference_price=100, availability=availability)
    risk=next(block.payload for block in _section(document,"operation_report").blocks if block.payload.table_id=="risk_table")
    assert document.market is Market.A and "市场规则暂时限制卖出" in str(risk.rows)


def test_golden_new_listing_sample_short_is_not_quality_failure(us_instrument, now, calendar):
    table = _section(_document(us_instrument, now, calendar, confirmed=False), "forecast").blocks[0].payload
    assert all("不参与新开仓执行分级" in row.cells[-1] for row in table.rows)
    assert "过去表现是否可靠" in table.columns and "数据质量" not in table.columns


def test_golden_missing_news_is_preserved_as_missing(us_instrument, now, calendar):
    table = _section(_document(us_instrument, now, calendar), "facts").blocks[0].payload
    news = next(row for row in table.rows if row.cells[0] == "新闻")
    assert news.cells[1] == "暂无可靠数据"


def test_golden_eleven_stock_waiting_does_not_stop_progress(us_instrument, now):
    progress = AnalysisTaskCoordinator(lambda: now).start("p", total_units=11, instrument=us_instrument)
    assert progress.total_units == 11 and progress.completed_units == 0


def test_golden_research_states_have_fixed_columns(us_instrument, now, calendar):
    hypotheses, validations = research_validation_states(us_instrument, now)
    table = _section(_document(
        us_instrument, now, calendar,
        research_hypotheses=hypotheses,
        research_validations=validations,
    ), "research").blocks[0].payload
    assert table.columns == ("观察", "验证状态", "候选资格", "历史结果", "原因与技术详情")
    assert {row.cells[1] for row in table.rows} == {"已确认", "系统反驳", "待验证", "数据不足"}


def test_golden_verified_forecast_has_issue_target_and_actual(us_instrument, now):
    outcome = forecast_outcome(us_instrument, now=now)
    table = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), outcomes=(outcome,), built_at=now).tables[1]
    row = table.rows[0]
    assert row.cells[0] == "AAPL" and str(outcome.origin_session_date) in row.cells and str(outcome.target_session_date) in row.cells and "正确" in row.cells


def test_golden_no_take_profit_is_unquantifiable(us_instrument, now, calendar):
    rows = _plan_detail(_document(us_instrument, now, calendar)).rows
    assert any(row.cells[6] == "未设置" and row.cells[7] == "不可量化" for row in rows)


def test_golden_zero_history_has_no_fake_curve(now):
    view = HistoricalEvaluationService().build(HistoricalEvaluationQuery(Market.US), built_at=now)
    assert view.charts[1].series == () and view.charts[1].empty_state


def test_golden_shared_trigger_profiles_explain_real_difference(us_instrument, now, calendar):
    table = _plan_detail(_document(us_instrument, now, calendar))
    assert "确认门槛、风险预算、批准股数或仓位" in table.interpretation


def test_golden_dual_market_report_and_export(a_instrument, us_instrument, now, calendar):
    a = _document(a_instrument, now, calendar); us = _document(us_instrument, now, calendar)
    assert "报告" in render_markdown(a) and "报告" in render_html(us)


def test_portfolio_plan_is_one_row_per_stock_and_exposes_all_five_decision_questions(us_instrument, now, calendar):
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 table=next(block.payload for block in _section(document,"operation_report").blocks if block.payload.table_id.startswith("stock_detail_"))
 labels={row.cells[0] for row in table.rows}
 assert {"当前应对","买入或加仓","卖出或减仓","持有与失效","风险方案"}<=labels
 assert all(row.cells[1] for row in table.rows)


def test_triggered_portfolio_exit_is_described_as_next_session_recheck(us_instrument, now, calendar):
 presentation = single_presentation(
  us_instrument, now=now, calendar=calendar,
  position=position(us_instrument, cost="100"), reference_price=90,
 )
 rows = _portfolio_plan_rows(presentation)
 current = rows[0]
 assert "条件已满足，待下一可交易时段复核" in current.cells[2]
 assert "退出不新增计划亏损" in current.cells[6]


def test_full_exit_ranks_before_reduce_when_both_are_triggered(us_instrument, now, calendar):
 presentation = single_presentation(
  us_instrument, now=now, calendar=calendar,
  position=position(us_instrument, cost="55"), reference_price=50,
 )
 current = _portfolio_plan_rows(presentation)[0]
 assert current.cells[2].startswith("卖出；")


def test_portfolio_report_leads_with_auditable_price_and_human_time(us_instrument, now, calendar):
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 assert "北京时间" in document.summary and "T16:00:00" not in document.summary
 facts=_section(document,"facts").blocks[0].payload
 assert facts.columns[:5]==("股票","身份","K线日期","用于分析的价格","价格来源")
 assert facts.rows[0].cells[3].startswith("盘后收盘价 $")
 assert "暂无可靠数据" not in facts.rows[0].cells[3]


def test_portfolio_oof_and_research_status_are_not_misreported_as_market_data_missing(us_instrument, now, calendar):
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 forecasts=_section(document,"forecast").blocks[0].payload
 assert "行情数据完整不等于预测模型有效" in forecasts.interpretation
 assert all(row.cells[6] and "数据缺失" not in row.cells[6] for row in forecasts.rows)
 research=_section(document,"research").blocks[0].payload
 assert research.rows[0].cells[0]=="本次调用"
 assert "后台研究" in research.rows[0].cells[1]


def test_portfolio_forecasts_are_one_readable_row_per_stock(us_instrument, now, calendar):
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 forecasts=_section(document,"forecast").blocks[0].payload
 assert len(forecasts.rows)==1
 assert forecasts.columns==("股票","分析价","未来1日","未来3日","未来5日","未来10日","能否影响新开仓")
 assert all(f"目标 " in forecasts.rows[0].cells[index] for index in range(2,6))


def test_portfolio_history_separates_forecast_strategy_and_joint(us_instrument, now, calendar):
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 history=tuple(block.payload.table_id for block in _section(document,"history").blocks)
 strategy=tuple(block.payload.table_id for block in _section(document,"strategy_performance").blocks)
 assert history==("portfolio_forecast_history",)
 assert strategy==("portfolio_strategies","portfolio_strategy_history","portfolio_joint_history")
 assert "预测是否准确" in _section(document,"history").purpose


def test_portfolio_report_leads_with_focus_before_detailed_evidence(us_instrument, now, calendar):
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 assert tuple(section.section_id for section in document.sections)==(
  "action_summary","facts","forecast","operation_report","strategy_performance","research","history",
 )
 assert document.sections[0].title=="操作总结"
 operation_ids=tuple(block.payload.table_id for block in _section(document,"operation_report").blocks)
 assert operation_ids[:4]==("operation_editorial","portfolio_exit_signals","portfolio_entry_signals","portfolio_hold_signals")


def test_portfolio_signal_cards_use_plain_judgment_steps_and_real_execution_language(us_instrument, now, calendar):
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 tables=tuple(block.payload for block in _section(document,"operation_report").blocks)
 table=next(item for item in tables if item.table_id.startswith("portfolio_") and item.table_id.endswith("_signals") and item.rows)
 row=table.rows[0]
 assert table.columns==("股票","身份","分析价","当前判断","触发步骤","执行方案")
 assert "分析价" not in row.cells[3]
 assert "依据“" not in row.cells[3]
 assert row.cells[3].endswith("。")
 assert "触发：" in row.cells[4] and "执行：" in row.cells[4]
 assert "保守：" in row.cells[5] and "激进：" in row.cells[5]
 assert " 0 股" not in row.cells[5]


def test_portfolio_stock_details_are_focus_explanation_pairs(us_instrument, now, calendar):
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 tables=tuple(block.payload for block in _section(document,"operation_report").blocks)
 detail=next(table for table in tables if table.table_id.startswith("stock_detail_"))
 assert detail.columns==("重点","结论与说明")
 assert {row.cells[0] for row in detail.rows}>={"股票与价格","未来走势","采用策略","当前应对","历史可信度"}
 html=render_html(document)
 assert '<details class="stock-detail">' in html
 assert "展开详细解释" in html


def test_portfolio_html_uses_action_cards_steps_and_probability_bars(us_instrument, now, calendar):
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 html=render_html(document)
 assert 'class="action-grid"' in html
 assert 'class="signal-steps"' in html
 assert '<ol class="cell-list">' in html
 assert 'class="probability-track"' in html
 assert 'class="forecast-cell"' in html
 assert 'class="signal-card' in html
 assert 'class="signal-grid"' in html
 quick=next(block.payload for block in document.sections[0].blocks if block.kind.value=="table")
 assert len(quick.rows)<=5


def test_portfolio_share_display_accepts_decimal_scale_from_real_allocation():
 assert _share_count(Decimal("55.0"))=="55"


def test_portfolio_profile_text_uses_typed_decimal_shares(us_instrument):
 allocation=SimpleNamespace(action=PlanAction.BUY,final_requested_shares=Decimal("55.0"))
 details={(us_instrument.code,name):(allocation,"条件满足后执行") for name in ("保守","激进")}
 text=_signal_profile_text(SimpleNamespace(instrument=us_instrument),details)
 assert "买入 55 股" in text and "55.0" not in text
