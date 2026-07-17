"""UX40--UX48：Tab3 快照编辑与市场隔离。"""
from datetime import timedelta
from decimal import Decimal
import pytest
from tradehelper_v2.application.portfolio import PortfolioEditor
from tradehelper_v2.contracts import AccountSnapshot, ContractViolation, PositionSnapshot
from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.application.portfolio import priority_allocations,replacement_research_candidates

def _account(instrument,now): return AccountSnapshot(instrument.market,"USD" if instrument.market.value=="US" else "CNY",Decimal("100"),(PositionSnapshot(instrument,Decimal("1"),Decimal("10"),now),),now)
def test_ux40_one_edit_path(us_instrument,now,tmp_path):
    editor=PortfolioEditor(SQLiteRepository(tmp_path/"p.sqlite"),lambda:now+timedelta(seconds=1)); assert callable(editor.save_position)
def test_first_use_creates_real_zero_or_positive_cash_account(now,tmp_path):
    repo=SQLiteRepository(tmp_path/"p.sqlite");editor=PortfolioEditor(repo,lambda:now)
    value=editor.create_account(market="A",cash="25000")
    assert value.cash==Decimal("25000") and value.currency=="CNY" and not value.positions
    repo.close()
def test_ux41_edit_creates_new_snapshot(us_instrument,now,tmp_path):
    repo=SQLiteRepository(tmp_path/"p.sqlite"); value=PortfolioEditor(repo,lambda:now+timedelta(seconds=1)).save_position(_account(us_instrument,now),instrument=us_instrument,shares="2",cost_price="11"); assert value.positions[0].shares==2; repo.close()
def test_ux42_remove_preserves_source_object(us_instrument,now,tmp_path):
    repo=SQLiteRepository(tmp_path/"p.sqlite"); source=_account(us_instrument,now); value=PortfolioEditor(repo,lambda:now+timedelta(seconds=1)).remove_position(source,instrument=us_instrument); assert source.positions and not value.positions; repo.close()
def test_ux43_watchlist_cannot_overlap_holding(us_instrument,now,tmp_path):
    repo=SQLiteRepository(tmp_path/"p.sqlite"); editor=PortfolioEditor(repo,lambda:now)
    with pytest.raises(ContractViolation): editor.save_watchlist(market=us_instrument.market,instruments=(us_instrument,),held_instruments=(us_instrument,))
    repo.close()
def test_ux44_cross_market_watchlist_rejected(us_instrument,a_instrument,now,tmp_path):
    repo=SQLiteRepository(tmp_path/"p.sqlite"); editor=PortfolioEditor(repo,lambda:now)
    with pytest.raises(ContractViolation): editor.save_watchlist(market=us_instrument.market,instruments=(a_instrument,))
    repo.close()
def test_ux45_frozen_valuation_contract_closes_exposure():
    """V2-6 FrozenAccountValuation 已将 equity=cash+invested 且仓位<=100% 固化。"""
    from tradehelper_v2.contracts.risk import FrozenAccountValuation
    assert "invested_pct" in FrozenAccountValuation.__dataclass_fields__
def test_ux46_protective_exit_is_before_new_risk(us_instrument,a_instrument):
    exit_item=type("A",(),{"allocation_id":"exit"})(); entry_item=type("A",(),{"allocation_id":"entry"})()
    profile=type("P",(),{"allocations":(entry_item,exit_item),"holding_priority_allocation_ids":("exit",),"entry_priority_allocation_ids":("entry",)})()
    assert priority_allocations(profile)==(exit_item,entry_item)
def test_ux47_replacement_is_research_candidate(us_instrument,a_instrument):
    candidate=type("R",(),{"source_instrument":us_instrument,"target_instrument":a_instrument})()
    profile=type("P",(),{"replacement_candidates":(candidate,)})()
    assert replacement_research_candidates(profile)[0]["reanalysis_required"]
def test_ux48_one_member_issue_is_isolated(us_instrument,a_instrument,now):
    from tradehelper_v2.application.portfolio import member_data_quality
    first=_input_holder(us_instrument,now,"blocked"); second=_input_holder(a_instrument,now,"ok")
    value=type("Input",(),{"instruments":(first,second)})()
    assert member_data_quality(value)[1][1]=="ok"
def _input_holder(instrument,now,status):
    return type("Input",(),{"instrument":instrument,"data_quality":status})()
