"""PO30-PO36：只按结构化字段排序。"""
from decimal import Decimal
from types import SimpleNamespace

from portfolio_helpers import portfolio_batch, portfolio_batch_many
from strategy_helpers import position
from contracts import (
    DecisionDisposition, EvidenceStatus, ExecutionLevel, Market, MarketEligibility,
    PlanAction, PortfolioRole,
)
from portfolio import PortfolioDecisionEngine, rank_entries, rank_holdings
from portfolio.ranking import rank_components


def _candidate(identifier, *, level=ExecutionLevel.B, disposition=DecisionDisposition.CONDITIONALLY_APPROVED,
               evidence=EvidenceStatus.UNAVAILABLE, current=None, confidence=None, expected=None,
               win=None, planned=Decimal("1000"), loss=Decimal("30"), friction=Decimal("5"),
               action=PlanAction.BUY):
    decision = SimpleNamespace(
        decision_id=identifier, level=level, disposition=disposition,
        current_position_pct=current, planned_position_value=planned,
        incremental_planned_loss=loss, friction_reserve=friction,
        market_eligibility=MarketEligibility.ELIGIBLE, max_loss_amount=loss,
    )
    evidence_value = SimpleNamespace(status=evidence, confidence_low=confidence,
                                     expected_net_return=expected, win_rate=win)
    return SimpleNamespace(execution_decision=decision, plan_evidence=evidence_value,
                           trade_plan=SimpleNamespace(action=action))


def test_po30_level_a_entry_ranks_before_level_b():
    assert rank_entries((_candidate("b", level=ExecutionLevel.B),
                         _candidate("a", level=ExecutionLevel.A)))[0].execution_decision.level is ExecutionLevel.A


def test_po31_reliable_positive_ranks_before_weaker_evidence():
    values = (_candidate("u", evidence=EvidenceStatus.UNAVAILABLE),
              _candidate("r", evidence=EvidenceStatus.RELIABLE_POSITIVE),
              _candidate("i", evidence=EvidenceStatus.INSUFFICIENT_SAMPLE))
    assert [item.execution_decision.decision_id for item in rank_entries(values)] == ["r", "i", "u"]


def test_po32_evidence_metrics_are_persisted_as_rank_components():
    components = dict(rank_components(_candidate("x", confidence=0.01, expected=0.03, win=0.60)))
    assert components["confidence_low"] == "0.01"
    assert components["expected_net_return"] == "0.03"
    assert components["win_rate"] == "0.6"


def test_po33_loss_and_friction_ratios_are_recomputable_and_missing_last():
    complete = _candidate("complete", loss=Decimal("20"), friction=Decimal("4"))
    missing = _candidate("missing", planned=None, loss=None, friction=None)
    assert rank_entries((missing, complete))[0] is complete
    components = dict(rank_components(complete))
    assert Decimal(components["loss_ratio"]) == Decimal("0.02")
    assert Decimal(components["friction_ratio"]) == Decimal("0.004")


def test_po34_decision_id_is_stable_final_tie_breaker():
    assert [item.execution_decision.decision_id for item in rank_entries(
        (_candidate("z"), _candidate("a")))] == ["a", "z"]


def test_po35_holding_and_watchlist_roles_do_not_cross(us_instrument):
    watch = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    batch = portfolio_batch_many((us_instrument, watch), positions=(position(us_instrument),))
    assert all(item.role is PortfolioRole.HOLDING for item in batch.candidates
               if item.trade_plan.instrument == us_instrument)
    assert all(item.role is PortfolioRole.WATCHLIST for item in batch.candidates
               if item.trade_plan.instrument == watch)
    assert all(item.trade_plan.action not in {PlanAction.ADD, PlanAction.REDUCE, PlanAction.SELL}
               for item in batch.candidates if item.role is PortfolioRole.WATCHLIST)


def test_po36_buy_never_enters_holding_exit_priority(us_instrument, now):
    result = PortfolioDecisionEngine().decide(
        portfolio_batch(us_instrument, position=position(us_instrument)), now,
    ).conservative
    by_id = {item.allocation_id: item for item in result.allocations}
    assert result.holding_priority_allocation_ids
    assert all(by_id[item].action in {PlanAction.SELL, PlanAction.REDUCE}
               for item in result.holding_priority_allocation_ids)
