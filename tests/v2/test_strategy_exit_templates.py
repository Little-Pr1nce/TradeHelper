from decimal import Decimal

from contracts import PlanAction, PositionSnapshot
from strategies import StrategyEngine
from strategy_helpers import NOW, strategy_input


def test_sp13_held_position_always_has_protective_exit(us_instrument):
    position = PositionSnapshot(us_instrument, Decimal("10"), Decimal("100"), NOW)
    exits = StrategyEngine().build(
        strategy_input(us_instrument, position=position, reference_price=90.0)
    ).reduce_or_exit.plans
    plan = next(plan for plan in exits if plan.strategy_id == "protective_exit_v1")
    assert plan.action is PlanAction.SELL
    assert plan.readiness.value == "triggered"
    assert "PROTECTIVE_EXIT_TRIGGERED" in plan.reason_codes
    invalidation = next(item for item in plan.evaluations if item.condition_id == plan.invalidation_condition.condition_id)
    assert invalidation.result.value == "false"


def test_sp19_held_bundle_has_four_branches(us_instrument):
    position = PositionSnapshot(us_instrument, Decimal("10"), Decimal("100"), NOW)
    bundle = StrategyEngine().build(strategy_input(us_instrument, position=position))
    assert bundle.hold.plans and bundle.invalidation.plans


def test_ma60_break_alone_does_not_turn_a_profitable_position_into_full_exit(us_instrument):
    held = PositionSnapshot(us_instrument, Decimal("10"), Decimal("55"), NOW)
    exits = StrategyEngine().build(
        strategy_input(us_instrument, position=held, reference_price=60.0)
    ).reduce_or_exit.plans
    protective = next(plan for plan in exits if plan.strategy_id == "protective_exit_v1")
    assert protective.readiness.value == "waiting"
    assert "PROTECTIVE_EXIT_PENDING" in protective.reason_codes
    assert any(plan.action is PlanAction.REDUCE for plan in exits)
