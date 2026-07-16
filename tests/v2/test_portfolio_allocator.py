"""PO10-PO23：退出优先、双 profile 与五类容量约束。"""
from decimal import Decimal

from portfolio_helpers import portfolio_batch, portfolio_batch_many, rebuild_batch
from strategy_helpers import position
from tradehelper_v2.contracts import (
    AllocationStatus, AvailabilitySource, HoldingRiskSnapshot, HoldingRiskStatus,
    Market, PlanAction, PositionAvailability, RiskProfile, stable_hash,
)
from tradehelper_v2.portfolio import PortfolioDecisionEngine
from tradehelper_v2.risk.sizing import cash_required, planned_loss


def _profile(batch, now, profile=RiskProfile.CONSERVATIVE):
    result = PortfolioDecisionEngine().decide(batch, now)
    return result.conservative if profile is RiskProfile.CONSERVATIVE else result.aggressive


def test_po10_protective_exit_precedes_new_risk(us_instrument, now):
    result = _profile(portfolio_batch(us_instrument, position=position(us_instrument)), now)
    first = next(item for item in result.allocations if item.allocation_id == result.holding_priority_allocation_ids[0])
    assert first.action is PlanAction.SELL
    assert "PORTFOLIO_PROTECTIVE_EXIT_PRIORITY" in first.reason_codes


def test_po11_bullish_forecast_and_missing_samples_do_not_delete_exit(us_instrument, now):
    result = _profile(portfolio_batch(us_instrument, position=position(us_instrument)), now)
    assert any(item.action is PlanAction.SELL and item.final_requested_shares > 0 for item in result.allocations)


def test_po12_cd_and_no_order_decisions_remain_zero_audit_rows(us_instrument, now):
    batch = portfolio_batch(us_instrument)
    result = _profile(batch, now)
    assert len(result.allocations) == sum(item.execution_decision.profile is RiskProfile.CONSERVATIVE
                                         for item in batch.candidates)
    assert all(item.final_requested_shares == 0 for item in result.allocations
               if item.status is AllocationStatus.NO_ORDER)


def test_po13_only_one_primary_entry_per_instrument_profile(us_instrument, now):
    result = _profile(portfolio_batch(us_instrument), now)
    buys = [item for item in result.allocations if item.action is PlanAction.BUY]
    assert sum(item.status in {AllocationStatus.ALLOCATED_NOW, AllocationStatus.RESERVED_CONDITIONAL}
               for item in buys) <= 1
    assert sum(item.status is AllocationStatus.MONITOR_ONLY for item in buys) >= 1


def test_po14_duplicate_entries_do_not_consume_cash_or_heat(us_instrument, now):
    result = _profile(portfolio_batch(us_instrument), now)
    duplicates = [item for item in result.allocations
                  if item.status is AllocationStatus.MONITOR_ONLY]
    assert duplicates and all(item.reserved_cash == item.reserved_incremental_loss == 0 for item in duplicates)
    assert result.reservation_snapshot.reserved_entry_cash == sum(
        (item.reserved_cash for item in result.allocations), Decimal("0"))


def test_po15_final_shares_never_exceed_risk_approval(us_instrument, now):
    result = PortfolioDecisionEngine().decide(portfolio_batch(us_instrument), now)
    for profile in (result.conservative, result.aggressive):
        assert all(Decimal("0") <= item.final_requested_shares <= item.approved_shares
                   for item in profile.allocations)


def test_po16_market_lots_round_down_without_forcing_one_lot(a_instrument, now):
    result = PortfolioDecisionEngine().decide(portfolio_batch(a_instrument), now)
    for profile in (result.conservative, result.aggressive):
        for item in profile.allocations:
            if item.action in {PlanAction.BUY, PlanAction.ADD}:
                assert item.final_requested_shares == 0 or item.final_requested_shares % 100 == 0
    held_position = position(a_instrument, shares="150")
    available = PositionAvailability(a_instrument, Decimal("150"), Decimal("150"), held_position.captured_at,
                                     AvailabilitySource.USER, ())
    held = portfolio_batch(a_instrument, position=held_position,
                           availability=available)
    exits = PortfolioDecisionEngine().decide(held, now).conservative.allocations
    assert any(item.action is PlanAction.SELL and item.final_requested_shares == 150 for item in exits)


def test_po17_exit_estimate_is_not_reused_as_deployable_cash(us_instrument, now):
    batch = portfolio_batch(us_instrument, position=position(us_instrument), cash=Decimal("50"))
    result = _profile(batch, now)
    assert result.reservation_snapshot.exit_release_estimate == Decimal("1000")
    assert result.reservation_snapshot.deployable_cash <= batch.valuation.cash


def test_po18_cash_reservations_use_complete_formula_and_stay_within_cash(us_instrument, now):
    batch = portfolio_batch(us_instrument)
    result = _profile(batch, now)
    candidate_by_id = {item.candidate_id: item for item in batch.candidates}
    for item in result.allocations:
        if item.reserved_cash > 0:
            candidate = candidate_by_id[item.candidate_id]
            assert item.reserved_cash == cash_required(item.final_requested_shares,
                                                       item.reference_entry_price,
                                                       candidate.market_rules)
    assert result.reservation_snapshot.reserved_entry_cash <= result.reservation_snapshot.deployable_cash


def test_po19_projected_total_stock_exposure_never_exceeds_ninety_percent(us_instrument, now):
    result = PortfolioDecisionEngine().decide(portfolio_batch(us_instrument), now)
    assert result.conservative.reservation_snapshot.projected_invested_pct_at_reference_price <= Decimal("0.90")
    assert result.aggressive.reservation_snapshot.projected_invested_pct_at_reference_price <= Decimal("0.90")


def test_po20_each_allocated_position_respects_twenty_five_percent_cap(us_instrument, now):
    result = PortfolioDecisionEngine().decide(portfolio_batch(us_instrument), now)
    assert all(item.estimated_position_pct is None or item.estimated_position_pct <= Decimal("0.25")
               for profile in (result.conservative, result.aggressive) for item in profile.allocations)


def test_po21_recomputed_heat_respects_profile_caps(us_instrument, now):
    batch = portfolio_batch(us_instrument)
    result = PortfolioDecisionEngine().decide(batch, now)
    for profile, cap in ((result.conservative, Decimal("0.04")),
                         (result.aggressive, Decimal("0.06"))):
        assert profile.reservation_snapshot.projected_heat_pct is None or profile.reservation_snapshot.projected_heat_pct <= cap
        for item in profile.allocations:
            if item.reserved_incremental_loss > 0:
                candidate = next(value for value in batch.candidates if value.candidate_id == item.candidate_id)
                assert item.reserved_incremental_loss == planned_loss(
                    item.final_requested_shares, item.reference_entry_price,
                    candidate.execution_decision.stop_price, candidate.market_rules,
                )


def test_po22_unknown_holding_risk_blocks_every_entry_but_not_exit(us_instrument, now):
    watch = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    batch = portfolio_batch_many((us_instrument, watch), positions=(position(us_instrument),),
                                 valuation_prices={})
    result = _profile(batch, now)
    assert result.current_risk_snapshot.planned_loss_amount is None
    assert all(item.final_requested_shares == 0 for item in result.allocations
               if item.action in {PlanAction.BUY, PlanAction.ADD})
    assert any(item.action is PlanAction.SELL and item.final_requested_shares > 0 for item in result.allocations)


def test_po23_breached_stop_is_not_zero_loss_and_blocks_entries(us_instrument, now):
    watch = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    batch = portfolio_batch_many((us_instrument, watch), positions=(position(us_instrument),))
    old = batch.holding_risks[0]
    identity = {"instrument": old.instrument, "shares": old.shares,
                "reference_price": old.reference_price, "market_value": old.market_value,
                "stop_price": old.reference_price, "exit_friction_reserve": old.exit_friction_reserve,
                "status": HoldingRiskStatus.BREACHED, "source_plan_id": old.source_plan_id,
                "source_decision_id": old.source_decision_id, "captured_at": old.captured_at}
    breached = HoldingRiskSnapshot(stable_hash(identity), old.instrument, old.shares, old.reference_price,
                                   old.market_value, old.reference_price, old.exit_friction_reserve, None,
                                   HoldingRiskStatus.BREACHED, old.source_plan_id, old.source_decision_id,
                                   old.captured_at, old.generated_at)
    result = _profile(rebuild_batch(batch, holding_risks=(breached,)), now)
    assert result.current_risk_snapshot.planned_loss_amount is None
    assert all(item.final_requested_shares == 0 for item in result.allocations
               if item.action in {PlanAction.BUY, PlanAction.ADD})
