from contracts import ForecastScope
from forecast.registry import ForecastRegistry

from tests.v2.test_forecast_repository import _model_version


def test_fc13_registry_is_horizon_isolated(us_instrument) -> None:
    registry = ForecastRegistry()
    assert registry.champion(market=us_instrument.market, scope=ForecastScope.STOCK, scope_key=us_instrument.stable_key, horizon=1) is None
    assert registry.champion(market=us_instrument.market, scope=ForecastScope.STOCK, scope_key=us_instrument.stable_key, horizon=3) is None


def test_registry_retires_only_expected_champion(us_instrument, now) -> None:
    registry = ForecastRegistry()
    version = _model_version(us_instrument, now)
    registry.promote(version)
    assert registry.retire(
        market=us_instrument.market, scope=ForecastScope.STOCK,
        scope_key=us_instrument.stable_key, horizon=1,
        expected_version="a-newer-version",
    ) is None
    assert registry.retire(
        market=us_instrument.market, scope=ForecastScope.STOCK,
        scope_key=us_instrument.stable_key, horizon=1,
        expected_version=version.version,
    ) is not None
    assert registry.champion(
        market=us_instrument.market, scope=ForecastScope.STOCK,
        scope_key=us_instrument.stable_key, horizon=1,
    ) is None
