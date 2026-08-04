import json
from types import SimpleNamespace

import pytest

from contracts import ContractViolation
from presentation.editor import EditorialItem, EditorialNarrative, build_editor_prompt, parse_editor_response
from presentation.report_builder import PortfolioReportBuilder, SingleStockReportBuilder
from presentation.renderers import render_html
from presentation_helpers import portfolio_presentation, single_document, single_presentation
from application.report_editor import edit_report


def _action(document):
    table=next(
        block.payload for section in document.sections for block in section.blocks
        if getattr(block.payload,"table_id",None)=="single_quick_action"
    )
    return table.rows[0].cells[2].split("；",1)[0]


def _response(document, *, action=None, headline=None):
    frozen_action=_action(document)
    return json.dumps({
        "overview":"当前先按系统排序处理，未满足条件的计划继续等待。",
        "items":[{
            "instrument":document.instrument.code,
            "action":action or frozen_action,
            "headline":headline or f"当前以{frozen_action}为主要计划，条件未确认前保持耐心。",
            "reasons":["预测先确定可能的行情方向。","策略再选择与该情景匹配的处理方式。","风控最后决定是否允许执行。"],
            "risk_note":"具体条件和风险边界以冻结操作卡为准。",
        }],
    },ensure_ascii=False)


def test_report_editor_prompt_contains_only_frozen_action_contract(us_instrument,now,calendar):
    document=single_document(us_instrument,now=now,calendar=calendar)
    prompt,prompt_hash=build_editor_prompt(document)
    payload=json.loads(prompt)
    assert len(prompt_hash)==64
    assert payload["frozen_actions"][0]["instrument"]==us_instrument.code
    assert payload["frozen_actions"][0]["action"]==_action(document)
    assert "不得改变动作" in payload["task"]


def test_portfolio_editor_covers_every_stock_while_first_screen_stays_compact(now,calendar):
    from contracts import InstrumentId, Market

    instruments=tuple(InstrumentId.from_code(code,Market.US,"XNAS") for code in ("AAPL","AMD","AVGO","GLW","MU","WDC"))
    document=PortfolioReportBuilder().build(portfolio_presentation(instruments,now=now,calendar=calendar))
    prompt,_=build_editor_prompt(document)
    payload=json.loads(prompt)
    quick=next(
        block.payload for block in document.sections[0].blocks
        if getattr(block.payload,"table_id",None)=="portfolio_quick_actions"
    )
    assert len(quick.rows)==5
    assert {item["instrument"] for item in payload["frozen_actions"]}=={item.code for item in instruments}


def test_report_editor_rejects_changed_action_numbers_and_omissions(us_instrument,now,calendar):
    document=single_document(us_instrument,now=now,calendar=calendar)
    changed="卖出" if _action(document)!="卖出" else "买入"
    with pytest.raises(ContractViolation):
        parse_editor_response(_response(document,action=changed),document)
    with pytest.raises(ContractViolation):
        parse_editor_response(_response(document,headline="建议在价格达到一百二十后执行 10 股。"),document)
    raw=json.loads(_response(document)); raw["items"]=[]
    with pytest.raises(ContractViolation):
        parse_editor_response(json.dumps(raw,ensure_ascii=False),document)


def test_editorial_narrative_only_changes_explanation_card(us_instrument,now,calendar):
    value=single_presentation(us_instrument,now=now,calendar=calendar)
    base=SingleStockReportBuilder().build(value)
    narrative=parse_editor_response(_response(base),base)
    revised=SingleStockReportBuilder().build(value,narrative)
    table=next(block.payload for block in next(section for section in revised.sections if section.section_id=="operation_report").blocks if getattr(block.payload,"table_id",None)=="operation_editorial")
    assert table.rows[0].cells[-1]=="LLM 报告编辑员"
    html=render_html(revised)
    assert 'class="editorial-grid"' in html and 'class="editorial-card' in html


def test_editorial_item_rejects_profit_guarantee():
    with pytest.raises(ContractViolation):
        EditorialNarrative("这次保证盈利。",(EditorialItem("AAPL","买入","当前买入。",("条件成立。",),"注意风险。"),))


def test_report_editor_client_uses_thinking_switch_and_strict_parser(us_instrument,now,calendar):
    document=single_document(us_instrument,now=now,calendar=calendar)
    bodies=[]
    def transport(endpoint,body,api_key,timeout):
        bodies.append(body)
        return {"choices":[{"message":{"content":_response(document)},"finish_reason":"stop"}],"finish_reason":"stop"}
    settings=SimpleNamespace(
        llm_base_url="https://api.deepseek.com",llm_api_key="test",llm_model="fixture",llm_enable_thinking=True,
    )
    narrative=edit_report(document,settings,transport=transport)
    assert narrative is not None and narrative.items[0].instrument==us_instrument.code
    assert bodies[0]["thinking"]=={"type":"enabled"} and "temperature" not in bodies[0]
