from dataclasses import replace
from datetime import timedelta

from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.strategies import StrategyEngine
from strategy_helpers import strategy_input


def test_sp25_plan_and_bundle_persist_idempotently(tmp_path, us_instrument):
    input = strategy_input(us_instrument)
    first = StrategyEngine().build(input, generated_at=input.as_of)
    second = StrategyEngine().build(input, generated_at=input.as_of + timedelta(seconds=1))
    assert first.bundle_id == second.bundle_id and first.generated_at != second.generated_at
    repo = SQLiteRepository(tmp_path / "v2.db")
    bundle = first
    plan = bundle.entry_or_add.plans[0]
    assert repo.save_trade_plan(plan).inserted == 1
    later_plan = next(item for item in second.entry_or_add.plans if item.plan_id == plan.plan_id)
    assert repo.save_trade_plan(later_plan).idempotent == 1
    assert repo.get_trade_plan(plan.plan_id) == plan
    assert repo.save_strategy_bundle(bundle).inserted == 1
    assert repo.save_strategy_bundle(second).idempotent == 1
    assert repo.get_strategy_bundle(bundle.bundle_id) == bundle

    conflicting_plan = replace(plan, reason_codes=tuple(plan.reason_codes) + ("PROFILES_MERGED",))
    conflicting_bundle = replace(bundle, reason_codes=("PROFILES_MERGED",))
    assert repo.save_trade_plan(conflicting_plan).conflicts == 1
    assert repo.save_strategy_bundle(conflicting_bundle).conflicts == 1
    assert repo._connection.execute(
        "SELECT COUNT(*) FROM quarantine_records WHERE record_type IN ('trade_plan_conflict','strategy_bundle_conflict')"
    ).fetchone()[0] == 2
    repo.close()

    reopened = SQLiteRepository(tmp_path / "v2.db")
    assert reopened.get_trade_plan(plan.plan_id) == plan
    assert reopened.get_strategy_bundle(bundle.bundle_id) == bundle
    reopened.close()
