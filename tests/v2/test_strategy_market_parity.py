from tradehelper_v2.strategies import StrategyEngine
from strategy_helpers import strategy_input


def test_sp23_equivalent_market_features_have_equivalent_template_ids(a_instrument, us_instrument):
    a_ids = {plan.strategy_id for plan in StrategyEngine().build(strategy_input(a_instrument)).entry_or_add.plans}
    us_ids = {plan.strategy_id for plan in StrategyEngine().build(strategy_input(us_instrument)).entry_or_add.plans}
    assert a_ids == us_ids
