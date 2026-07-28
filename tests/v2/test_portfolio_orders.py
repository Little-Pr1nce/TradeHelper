"""PO45-PO47：组合最终股数进入唯一 V2-7 订单路径。"""
from decimal import Decimal

from portfolio_helpers import portfolio_batch
from strategy_helpers import position
from contracts import ExecutionPolicy, PlanAction, RiskProfile
from portfolio import PortfolioDecisionEngine, PortfolioOrderAssembler


def _assemble(batch, result, profile, calendar, now):
    plans = {item.trade_plan.plan_id: item.trade_plan for item in batch.candidates}
    return PortfolioOrderAssembler.build(result, profile, plans, batch.risk_bundles,
                                         calendar, ExecutionPolicy(), now)


def test_po45_final_shares_are_passed_unchanged_to_v2_7(us_instrument, calendar, now):
    batch = portfolio_batch(us_instrument)
    result = PortfolioDecisionEngine().decide(batch, now)
    built = _assemble(batch, result, RiskProfile.CONSERVATIVE, calendar, now)
    allocated = {item.decision_id: item.final_requested_shares for item in result.conservative.allocations}
    intents = {item.decision_id: item for bundle in built for item in bundle.intents}
    assert all(intents[decision_id].requested_shares == shares
               for decision_id, shares in allocated.items() if shares > 0)


def test_po46_unselected_approved_decision_is_explicitly_zero(us_instrument, calendar, now):
    batch = portfolio_batch(us_instrument)
    result = PortfolioDecisionEngine().decide(batch, now)
    built = _assemble(batch, result, RiskProfile.CONSERVATIVE, calendar, now)
    allocated = {item.decision_id: item.final_requested_shares for item in result.conservative.allocations}
    decisions = {item.decision_id: item for bundle in batch.risk_bundles for item in bundle.decisions}
    records = {item.decision_id: item for bundle in built for item in bundle.records}
    suppressed = [decision_id for decision_id, decision in decisions.items()
                  if decision.approved_shares > 0 and allocated.get(decision_id, Decimal("0")) == 0]
    assert suppressed
    assert all("EXEC_PORTFOLIO_NOT_ALLOCATED" in records[item].reasons for item in suppressed)


def test_po47_multiple_exit_plans_share_one_sell_reservation(us_instrument, now):
    batch = portfolio_batch(us_instrument, position=position(us_instrument, shares="10", cost="80"))
    profile = PortfolioDecisionEngine().decide(batch, now).conservative
    assert len(profile.reservation_groups) == 1
    group = profile.reservation_groups[0]
    members = [item for item in profile.allocations if item.reservation_group_id == group.group_id]
    assert len(members) >= 2
    assert group.max_aggregate_shares == Decimal("10")
    assert sum((item.final_requested_shares for item in members), Decimal("0")) > group.max_aggregate_shares


def test_triggered_profit_lock_ranks_before_pending_protective_exit(us_instrument, now):
    batch = portfolio_batch(us_instrument, position=position(us_instrument, shares="10", cost="80"))
    profile = PortfolioDecisionEngine().decide(batch, now).conservative
    allocations = {item.allocation_id: item for item in profile.allocations}
    first = allocations[profile.holding_priority_allocation_ids[0]]
    assert first.action is PlanAction.REDUCE
