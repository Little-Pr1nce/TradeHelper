from decimal import Decimal

from contracts import AccountSnapshot, Market, PositionSnapshot, ValuationPrice, ValuationPriceKind, FreshnessStatus
from risk import freeze_account_valuation


def test_rk03_missing_position_price_stays_incomplete(us_instrument, now):
    account = AccountSnapshot(Market.US, "USD", Decimal("10"), (PositionSnapshot(us_instrument, Decimal("2"), Decimal("100"), now),), now)
    assert freeze_account_valuation(account, {}, now, generated_at=now).equity is None


def test_rk30_frozen_valuation_uses_decimal(us_instrument, now):
    account = AccountSnapshot(Market.US, "USD", Decimal("10"), (PositionSnapshot(us_instrument, Decimal("2"), Decimal("100"), now),), now)
    price = ValuationPrice(us_instrument, Decimal("110.10"), now, "fixture", ValuationPriceKind.REFERENCE_CLOSE, FreshnessStatus.NOT_REQUIRED)
    assert freeze_account_valuation(account, {us_instrument: price}, now, generated_at=now).equity == Decimal("230.20")
