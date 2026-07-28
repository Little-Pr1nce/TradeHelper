from decimal import Decimal

from contracts import DecisionMode, FreshnessStatus, MarketEligibility, MarketState, PlanAction, TradingSession
from risk.market_rules import default_market_rules, precheck


def test_rk27_us_does_not_have_a_lot_or_limit(now, us_instrument):
    assert default_market_rules(us_instrument.market, us_instrument.exchange, now).lot_size == 1


def test_rk28_us_pre_price_only_uses_quarter_capacity(now, us_instrument):
    state = MarketState(us_instrument, DecisionMode.PRE, TradingSession.PRE, Decimal("100"), Decimal("99"), None, None, None, now, "fixture", FreshnessStatus.FRESH)
    assert precheck(default_market_rules(us_instrument.market, us_instrument.exchange, now), state, PlanAction.BUY).liquidity_multiplier == Decimal("0.25")
