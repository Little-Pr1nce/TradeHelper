from tradehelper_v2.contracts import ForecastScope
from tradehelper_v2.forecast.registry import ForecastRegistry


def test_fc13_registry_is_horizon_isolated(us_instrument) -> None:
    registry = ForecastRegistry()
    assert registry.champion(market=us_instrument.market, scope=ForecastScope.STOCK, scope_key=us_instrument.stable_key, horizon=1) is None
    assert registry.champion(market=us_instrument.market, scope=ForecastScope.STOCK, scope_key=us_instrument.stable_key, horizon=3) is None
