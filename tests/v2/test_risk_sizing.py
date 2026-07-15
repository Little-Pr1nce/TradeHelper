from decimal import Decimal

from tradehelper_v2.risk.market_rules import default_market_rules
from tradehelper_v2.risk.sizing import friction_reserve, round_lot_down


def test_rk14_lot_rounding_never_forces_capacity(now, a_instrument):
    rules = default_market_rules(a_instrument.market, a_instrument.exchange, now)
    assert round_lot_down(Decimal("99"), rules.lot_size) == 0


def test_rk30_friction_is_decimal(now, us_instrument):
    rules = default_market_rules(us_instrument.market, us_instrument.exchange, now)
    assert isinstance(friction_reserve(Decimal("3"), Decimal("10"), Decimal("9"), rules), Decimal)
