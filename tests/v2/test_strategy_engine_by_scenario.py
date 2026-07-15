from tradehelper_v2.contracts import PlanAction
from tradehelper_v2.strategies import StrategyEngine
from strategy_helpers import strategy_input


def test_sp01_bullish_continuation_has_trend_candidate(us_instrument):
    plans = StrategyEngine().build(strategy_input(us_instrument)).entry_or_add.plans
    assert any(plan.strategy_id == "trend_continuation_v1" for plan in plans)


def test_sp03_bearish_continuation_does_not_open_position(us_instrument):
    input = strategy_input(us_instrument, directions={1: "bearish", 3: "bearish", 5: "bearish", 10: "bearish"})
    plans = StrategyEngine().build(input).entry_or_add.plans
    assert all(plan.action is PlanAction.WATCH for plan in plans)
