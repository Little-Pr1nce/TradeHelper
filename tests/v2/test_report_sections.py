"""UX10--UX19: report sections consume frozen V2 facts without invention."""
from tradehelper_v2.presentation.reasons import explain
from test_presentation_contracts import _document as _build_document, _input


def _document(instrument, now, **_unused):
    return _build_document(_input(instrument, now))


def _section(document, key):
    return next(item for item in document.sections if item.section_id == key)


def _text(section):
    return " ".join(str(block.payload) for block in section.blocks)


def test_ux10_action_desk_uses_risk_decision(us_instrument, now):
    document = _document(us_instrument, now)
    text = _text(_section(document, "action_desk"))
    decision = next(item for item in _input(us_instrument, now).risk_bundle.decisions if item.profile.value == "conservative" and item.action.value in {"buy", "add", "reduce", "sell"})
    assert decision.action.value in text and str(decision.approved_shares) in text


def test_ux11_all_four_plan_branches_are_visible(us_instrument, now):
    table = _section(_document(us_instrument, now), "plans").blocks[0].payload
    assert {row.cells[0] for row in table.rows} == {"买入/加仓", "卖出/减仓", "持有", "失效"}
    assert "当前价" in str(table.rows) and "确认：" in str(table.rows)


def test_ux12_missing_take_profit_says_unquantifiable(us_instrument, now):
    assert "不可量化" in str(_section(_document(us_instrument, now), "plans").blocks[0].payload.rows)


def test_ux13_forecast_table_contains_target_probability_and_quantiles(us_instrument, now):
    columns = _section(_document(us_instrument, now), "forecast").blocks[0].payload.columns
    assert {"预测时间", "目标交易日", "上涨", "P10", "P50", "P90"}.issubset(columns)


def test_ux14_oof_state_explains_execution_impact(us_instrument, now):
    table = _section(_document(us_instrument, now), "forecast").blocks[0].payload
    assert "执行影响" in table.columns and all(row.cells[-1] for row in table.rows)


def test_ux15_profile_difference_is_explained_without_invented_trigger(us_instrument, now):
    interpretation = _section(_document(us_instrument, now), "plans").blocks[0].payload.interpretation
    assert "共享触发价" in interpretation and "风险预算" in interpretation


def test_ux16_history_rank_is_separate_from_current_action(us_instrument, now):
    section = _section(_document(us_instrument, now), "history")
    assert section.section_id != "action_desk" and "不会覆盖当前风险动作" in section.blocks[0].payload.interpretation


def test_ux17_research_status_and_eligibility_columns_are_shown(us_instrument, now):
    table = _section(_document(us_instrument, now), "research").blocks[0].payload
    assert {"验证状态", "候选资格", "历史结果"}.issubset(table.columns)


def test_ux18_missing_news_is_not_zero(us_instrument, now):
    text = _text(_section(_document(us_instrument, now), "facts"))
    assert "暂无可靠数据" in text and "新闻', '0" not in text


def test_ux19_unknown_reason_keeps_technical_code():
    assert "UPSTREAM_X" in explain("UPSTREAM_X")
