"""PO37-PO44（除 PO43）：替换研究与预留快照边界。"""
from decimal import Decimal

from portfolio_helpers import correlation_for, portfolio_batch, portfolio_batch_many, rebuild_batch
from strategy_helpers import position
from tradehelper_v2.contracts import (
    AllocationStatus, ExecutionLevel, Market, PlanAction, PortfolioRole, canonical_json,
)
from tradehelper_v2.portfolio import PortfolioDecisionEngine


def _replacement_case(us_instrument, now):
    second = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    third = type(us_instrument).from_code("NVDA", Market.US, "XNAS")
    instruments = (us_instrument, second, third)
    batch = portfolio_batch_many(instruments, positions=(position(us_instrument, shares="10", cost="80"),),
                                 cash=Decimal("500"))
    batch = rebuild_batch(batch, correlation_snapshot=correlation_for(instruments))
    return batch, PortfolioDecisionEngine().decide(batch, now).aggressive


def test_po37_replacement_requires_an_independent_exit(us_instrument, now):
    other = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    batch = portfolio_batch_many((us_instrument, other), cash=Decimal("500"))
    batch = rebuild_batch(batch, correlation_snapshot=correlation_for((us_instrument, other)))
    result = PortfolioDecisionEngine().decide(batch, now)
    assert not result.conservative.replacement_candidates
    assert not result.aggressive.replacement_candidates


def test_po38_replacement_target_is_ab_flat_watchlist_entry(us_instrument, now):
    batch, profile = _replacement_case(us_instrument, now)
    replacement = profile.replacement_candidates[0]
    target = next(item for item in profile.allocations
                  if item.allocation_id == replacement.target_entry_allocation_id)
    candidate = next(item for item in batch.candidates if item.candidate_id == target.candidate_id)
    assert candidate.role is PortfolioRole.WATCHLIST
    assert target.action is PlanAction.BUY
    assert candidate.execution_decision.level in {ExecutionLevel.A, ExecutionLevel.B}
    assert target.status is AllocationStatus.BLOCKED


def test_po39_replacement_never_reuses_exit_cash_automatically(us_instrument, now):
    _, profile = _replacement_case(us_instrument, now)
    replacement = profile.replacement_candidates[0]
    assert replacement.reanalysis_required
    assert replacement.estimated_release_amount > 0
    assert profile.reservation_snapshot.remaining_cash == (
        profile.reservation_snapshot.deployable_cash - profile.reservation_snapshot.reserved_entry_cash
    )
    assert profile.reservation_snapshot.remaining_cash < replacement.target_required_cash


def test_po40_portfolio_output_has_no_profit_guarantee_or_best_asset_fields(us_instrument, now):
    payload = canonical_json(PortfolioDecisionEngine().decide(portfolio_batch(us_instrument), now)).lower()
    for forbidden in ("best_asset", "guaranteed_profit", "llm_score", "reason_text_score", "auto_execute"):
        assert forbidden not in payload


def test_po41_zero_capacity_candidate_remains_visible_and_blocked(us_instrument, now):
    _, profile = _replacement_case(us_instrument, now)
    blocked = [item for item in profile.allocations if item.status is AllocationStatus.BLOCKED]
    assert blocked and all(item.final_requested_shares == 0 for item in blocked)
    assert all(item.allocation_id in profile.blocked_allocation_ids for item in blocked)


def test_po42_conservative_and_aggressive_mutable_budgets_are_isolated(us_instrument, now):
    other = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    batch = portfolio_batch_many((us_instrument, other))
    batch = rebuild_batch(batch, correlation_snapshot=correlation_for((us_instrument, other)))
    result = PortfolioDecisionEngine().decide(batch, now)
    assert result.conservative.reservation_snapshot.frozen_cash == result.aggressive.reservation_snapshot.frozen_cash
    assert result.conservative.reservation_snapshot.reserved_entry_cash != result.aggressive.reservation_snapshot.reserved_entry_cash
    assert {item.allocation_id for item in result.conservative.allocations}.isdisjoint(
        {item.allocation_id for item in result.aggressive.allocations})


def test_po44_reservation_snapshot_is_not_post_exit_valuation(us_instrument, now):
    batch = portfolio_batch(us_instrument, position=position(us_instrument), cash=Decimal("50"))
    profile = PortfolioDecisionEngine().decide(batch, now).conservative
    assert profile.reservation_snapshot.exit_release_estimate == Decimal("1000")
    assert profile.reservation_snapshot.frozen_equity == batch.valuation.equity
    assert profile.reservation_snapshot.frozen_cash == Decimal("50")
    assert profile.reservation_snapshot.remaining_cash <= Decimal("50")
