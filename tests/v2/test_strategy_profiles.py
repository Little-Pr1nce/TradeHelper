from contracts import PlanProfile
from strategies import StrategyEngine
from strategy_helpers import strategy_input


def test_sp18_identical_profiles_merge(us_instrument):
    plans = StrategyEngine().build(strategy_input(us_instrument)).entry_or_add.plans
    plan = next(plan for plan in plans if plan.strategy_id == "trend_continuation_v1")
    assert plan.profiles == (PlanProfile.AGGRESSIVE, PlanProfile.CONSERVATIVE)
