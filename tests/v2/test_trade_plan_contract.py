from datetime import timedelta

import pytest

from contracts import ContractViolation, PlanAction, PlanReadiness
from strategies import StrategyEngine
from strategy_helpers import strategy_input


def test_sp00_plan_identity_is_independent_of_generated_at(us_instrument):
    input = strategy_input(us_instrument)
    first = StrategyEngine().build(input, generated_at=input.as_of)
    second = StrategyEngine().build(input, generated_at=input.as_of + timedelta(seconds=1))
    assert first.entry_or_add.plans[0].plan_id == second.entry_or_add.plans[0].plan_id


def test_sp14_entry_plans_have_stop_or_are_observation_only(us_instrument):
    bundle = StrategyEngine().build(strategy_input(us_instrument))
    for plan in bundle.entry_or_add.plans:
        if plan.action in {PlanAction.BUY, PlanAction.ADD}:
            assert plan.stop is not None or plan.readiness is PlanReadiness.OBSERVATION_ONLY
            assert {item.condition_id for item in plan.evaluations}
