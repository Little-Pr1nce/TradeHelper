from tradehelper_v2.contracts import PlanAction, TakeProfitMode
from tradehelper_v2.strategies import StrategyEngine
from strategy_helpers import strategy_input


def test_sp15_trend_two_r_is_consistent(us_instrument):
    plans = StrategyEngine().build(strategy_input(us_instrument)).entry_or_add.plans
    plan = next(plan for plan in plans if plan.strategy_id == "trend_continuation_v1")
    assert plan.take_profit.mode is TakeProfitMode.RISK_MULTIPLE
    assert plan.take_profit.level.value == plan.trigger_level.value + 2 * (plan.trigger_level.value - plan.stop.level.value)


def test_sp05_range_template_is_routed_when_range(us_instrument):
    input = strategy_input(us_instrument, directions={1: "neutral", 3: "neutral", 5: "neutral", 10: "neutral"})
    bundle = StrategyEngine().build(input)
    assert any(plan.strategy_id == "range_mean_reversion_v1" for plan in bundle.entry_or_add.plans)
    assert bundle.entry_or_add.readiness.value == "triggered"
    assert bundle.invalidation.readiness.value == "waiting"
