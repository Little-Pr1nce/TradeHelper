"""V2-8 合同、双市场和 migration 12 冒烟测试。"""
from __future__ import annotations

from pathlib import Path

from portfolio_helpers import portfolio_batch
from tradehelper_v2.contracts import ExecutionPolicy, RiskProfile
from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.portfolio import PortfolioDecisionEngine, PortfolioOrderAssembler


def test_portfolio_engine_keeps_all_upstream_decisions_for_both_markets(us_instrument, a_instrument, now):
    for instrument in (us_instrument, a_instrument):
        batch = portfolio_batch(instrument)
        result = PortfolioDecisionEngine().decide(batch, now)
        for profile in (result.conservative, result.aggressive):
            upstream = {item.execution_decision.decision_id for item in batch.candidates if item.execution_decision.profile is profile.profile}
            assert {item.decision_id for item in profile.allocations} == upstream
        assert result.market is instrument.market


def test_portfolio_migration_12_and_atomic_idempotent_write(tmp_path, us_instrument, now):
    batch = portfolio_batch(us_instrument)
    result = PortfolioDecisionEngine().decide(batch, now)
    repository = SQLiteRepository(Path(tmp_path) / "portfolio.sqlite")
    try:
        first = repository.save_portfolio_result(batch, result)
        second = repository.save_portfolio_result(batch, result)
        assert first[0].inserted == first[1].inserted == 1
        assert second[0].idempotent == second[1].idempotent == 1
        assert repository.get_portfolio_input_batch(batch.batch_id) == batch
        assert repository.get_portfolio_decision_bundle(result.portfolio_bundle_id) == result
    finally:
        repository.close()


def test_portfolio_order_assembly_explicitly_passes_final_shares(us_instrument, calendar, now):
    batch = portfolio_batch(us_instrument)
    result = PortfolioDecisionEngine().decide(batch, now)
    plans = {item.trade_plan.plan_id: item.trade_plan for item in batch.candidates}
    built = PortfolioOrderAssembler.build(result, RiskProfile.CONSERVATIVE, plans, batch.risk_bundles, calendar, ExecutionPolicy(), now)
    allocations = {item.decision_id: item.final_requested_shares for item in result.conservative.allocations}
    expected = {item.decision_id: allocations.get(item.decision_id, 0) for bundle in batch.risk_bundles for item in bundle.decisions}
    records = {item.decision_id: item for bundle in built for item in bundle.records}
    intents = {item.decision_id: item for bundle in built for item in bundle.intents}
    assert set(records) == set(expected)
    assert all((intents[decision_id].requested_shares if decision_id in intents else 0) == expected[decision_id] for decision_id in records)
