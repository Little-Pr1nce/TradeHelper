from datetime import date
from hashlib import sha256

from tradehelper_v2.contracts import (
    ForecastModelVersion, ForecastScope, ModelFamily, ModelLifecycle, ModelSpec, ValidationStatus, stable_hash,
)
from tradehelper_v2.forecast.models import fit_model
from tradehelper_v2.forecast.registry import ForecastRegistry
from tests.v2.forecast_helpers import synthetic_samples


def test_fc12_fallback_is_empty_without_confirmed_models(us_instrument) -> None:
    registry = ForecastRegistry()
    assert registry.resolve(market=us_instrument.market, stock_key=us_instrument.stable_key, industry_key="US:semiconductors", horizon=1) is None


def _version(instrument, now, scope, scope_key, suffix):
    samples = synthetic_samples(instrument, count=30)
    spec = ModelSpec(f"empirical-{suffix}", ModelFamily.EMPIRICAL, "tech", {})
    model = fit_model(spec, samples); assert model is not None
    artifact = model.artifact_bytes()
    version = ForecastModelVersion(
        f"model-{suffix}", scope, scope_key, instrument.market, 1, spec,
        ModelLifecycle.CHAMPION, ValidationStatus.CONFIRMATION_PASSED,
        samples[0].origin_session_date, samples[-1].origin_session_date,
        samples[0].origin_session_date, samples[9].origin_session_date,
        samples[10].origin_session_date, samples[-1].origin_session_date,
        stable_hash(("training", suffix)), "json+zlib-v1", sha256(artifact).hexdigest(), artifact,
        7, 30, 20, now, now,
    )
    return version, model


def test_fc12_resolve_prefers_stock_then_industry_then_market(us_instrument, now) -> None:
    registry = ForecastRegistry(); industry = "US:semiconductors"
    market_version, market_model = _version(us_instrument, now, ForecastScope.MARKET, us_instrument.market.value, "market")
    industry_version, industry_model = _version(us_instrument, now, ForecastScope.INDUSTRY, industry, "industry")
    stock_version, stock_model = _version(us_instrument, now, ForecastScope.STOCK, us_instrument.stable_key, "stock")
    registry.promote(market_version, market_model)
    assert registry.resolve(market=us_instrument.market, stock_key=us_instrument.stable_key, industry_key=industry, horizon=1).version.scope is ForecastScope.MARKET
    registry.promote(industry_version, industry_model)
    assert registry.resolve(market=us_instrument.market, stock_key=us_instrument.stable_key, industry_key=industry, horizon=1).version.scope is ForecastScope.INDUSTRY
    registry.promote(stock_version, stock_model)
    assert registry.resolve(market=us_instrument.market, stock_key=us_instrument.stable_key, industry_key=industry, horizon=1).version.scope is ForecastScope.STOCK
