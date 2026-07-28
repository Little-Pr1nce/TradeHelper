from datetime import datetime
from decimal import Decimal

import pytest

from contracts import AccountSnapshot, ContractViolation, Market, PositionSnapshot, value_account


def test_g60_zero_accounts_never_create_fake_capital(now) -> None:
    for market, currency in ((Market.A, "CNY"), (Market.US, "USD")):
        account = AccountSnapshot(market, currency, Decimal("0"), (), now)
        assert value_account(account, {}).equity == Decimal("0")


def test_g61_rejects_negative_account_values(us_instrument, now) -> None:
    with pytest.raises(ContractViolation):
        AccountSnapshot(Market.US, "USD", Decimal("-1"), (), now)
    with pytest.raises(ContractViolation):
        PositionSnapshot(us_instrument, Decimal("-1"), Decimal("1"), now)
    with pytest.raises(ContractViolation):
        PositionSnapshot(us_instrument, Decimal("1"), Decimal("-1"), now)


def test_g62_missing_frozen_quote_does_not_use_cost(us_instrument, now) -> None:
    account = AccountSnapshot(Market.US, "USD", Decimal("1000"), (PositionSnapshot(us_instrument, Decimal("10"), Decimal("100"), now),), now)
    valuation = value_account(account, {})
    assert valuation.equity is None and valuation.missing_prices == (us_instrument,)
