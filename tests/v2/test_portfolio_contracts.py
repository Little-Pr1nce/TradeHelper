"""PO00-PO09：组合合同、冻结批次与稳定身份。"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from portfolio_helpers import empty_portfolio_batch, portfolio_batch, rebuild_batch
from strategy_helpers import position
from tradehelper_v2.contracts import (
    ContractViolation, Market, PORTFOLIO_REASON_CODES, PortfolioDecisionBundle,
    PortfolioInputBatch, PortfolioPolicy, stable_hash,
)
from tradehelper_v2.portfolio import PortfolioDecisionEngine


def test_po00_contract_decimal_enum_hash_and_reason_registry(us_instrument, now):
    policy = PortfolioPolicy()
    assert isinstance(policy.conservative_heat_cap, Decimal)
    assert policy.parameter_hash == PortfolioPolicy().parameter_hash
    assert "PORTFOLIO_ALLOCATED" in PORTFOLIO_REASON_CODES
    with pytest.raises(ContractViolation):
        replace(policy, conservative_heat_cap=Decimal("0.05"))


def test_po01_batch_rejects_mixed_market_or_currency(us_instrument, a_instrument):
    batch = portfolio_batch(us_instrument)
    with pytest.raises(ContractViolation):
        PortfolioInputBatch(batch.batch_id, Market.A, "USD", batch.mode, batch.account_snapshot,
                            batch.valuation, batch.risk_policy, batch.portfolio_policy,
                            batch.risk_bundles, batch.candidates, batch.watchlist,
                            batch.holding_risks, batch.correlation_snapshot, batch.as_of,
                            batch.generated_at)
    assert portfolio_batch(a_instrument).currency == "CNY"


def test_po02_account_hash_and_valuation_identity_are_bound(us_instrument, now):
    batch = portfolio_batch(us_instrument)
    result = PortfolioDecisionEngine().decide(batch, now)
    identity = {"batch_id": result.batch_id, "market": result.market, "account_hash": "f" * 64,
                "valuation_id": result.valuation_id,
                "conservative": result.conservative.profile_decision_id,
                "aggressive": result.aggressive.profile_decision_id,
                "policy": result.portfolio_policy_version}
    foreign = PortfolioDecisionBundle(stable_hash(identity), result.batch_id, result.market, "f" * 64,
                                      result.valuation_id, result.conservative, result.aggressive,
                                      result.portfolio_policy_version, now)
    assert foreign.account_hash != batch.valuation.account_hash


def test_po03_incomplete_valuation_blocks_entries_but_keeps_exits(us_instrument, now):
    batch = portfolio_batch(us_instrument, position=position(us_instrument), valuation_price=None)
    result = PortfolioDecisionEngine().decide(batch, now).conservative
    assert any(item.action.value in {"sell", "reduce"} and item.final_requested_shares > 0
               for item in result.allocations)
    assert all(item.final_requested_shares == 0 for item in result.allocations
               if item.action.value in {"buy", "add"})


def test_po04_candidate_upstream_identity_mismatch_is_rejected(us_instrument):
    candidate = portfolio_batch(us_instrument).candidates[0]
    with pytest.raises(ContractViolation):
        replace(candidate, candidate_id="0" * 64)


def test_po05_future_input_evidence_is_rejected(us_instrument):
    batch = portfolio_batch(us_instrument)
    with pytest.raises(ContractViolation):
        rebuild_batch(batch, as_of=batch.as_of - timedelta(seconds=1))


def test_po06_watchlist_must_be_unique_and_disjoint(us_instrument):
    batch = portfolio_batch(us_instrument)
    with pytest.raises(ContractViolation):
        PortfolioInputBatch("0" * 64, batch.market, batch.currency, batch.mode,
                            batch.account_snapshot, batch.valuation, batch.risk_policy,
                            batch.portfolio_policy, batch.risk_bundles, batch.candidates,
                            (us_instrument, us_instrument), batch.holding_risks,
                            batch.correlation_snapshot, batch.as_of, batch.generated_at)


def test_po07_candidates_cover_every_risk_decision(us_instrument):
    batch = portfolio_batch(us_instrument)
    expected = {item.decision_id for bundle in batch.risk_bundles for item in bundle.decisions}
    assert {item.execution_decision.decision_id for item in batch.candidates} == expected
    with pytest.raises(ContractViolation):
        rebuild_batch(batch, candidates=batch.candidates[:-1])


def test_po08_zero_equity_never_uses_default_capital(us_instrument, now):
    batch = portfolio_batch(us_instrument, cash=Decimal("0"))
    result = PortfolioDecisionEngine().decide(batch, now)
    assert result.conservative.current_risk_snapshot.equity == 0
    assert result.conservative.reservation_snapshot.deployable_cash == 0
    assert all(item.final_requested_shares == 0 for item in result.conservative.allocations)
    empty = PortfolioDecisionEngine().decide(empty_portfolio_batch(), now)
    assert empty.conservative.allocations == empty.aggressive.allocations == ()
    assert empty.conservative.current_risk_snapshot.equity == Decimal("1000")


def test_po09_business_ids_ignore_generated_at(us_instrument, now):
    batch = portfolio_batch(us_instrument)
    first = PortfolioDecisionEngine().decide(batch, now)
    second = PortfolioDecisionEngine().decide(batch, now + timedelta(minutes=5))
    assert first.portfolio_bundle_id == second.portfolio_bundle_id
    assert {item.allocation_id for item in first.conservative.allocations} == {
        item.allocation_id for item in second.conservative.allocations
    }
