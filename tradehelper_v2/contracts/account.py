"""Account contracts kept separate from market observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from .enums import Market
from .market_data import ContractViolation, InstrumentId, ensure_utc


def as_decimal(value: Decimal | int | float | str, field_name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ContractViolation(f"{field_name} must be a Decimal-compatible value") from exc
    if not result.is_finite():
        raise ContractViolation(f"{field_name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    instrument: InstrumentId
    shares: Decimal
    cost_price: Decimal
    captured_at: datetime

    def __post_init__(self) -> None:
        shares = as_decimal(self.shares, "shares")
        cost_price = as_decimal(self.cost_price, "cost_price")
        if shares <= 0:
            raise ContractViolation("active position shares must be positive")
        if cost_price < 0:
            raise ContractViolation("cost_price cannot be negative")
        object.__setattr__(self, "shares", shares)
        object.__setattr__(self, "cost_price", cost_price)
        object.__setattr__(self, "captured_at", ensure_utc(self.captured_at, "captured_at"))


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    market: Market
    currency: str
    cash: Decimal
    positions: tuple[PositionSnapshot, ...]
    captured_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        market = self.market if isinstance(self.market, Market) else Market(str(self.market).upper())
        currency = str(self.currency).upper()
        expected_currency = "CNY" if market is Market.A else "USD"
        if currency != expected_currency:
            raise ContractViolation(f"{market.value} account currency must be {expected_currency}")
        cash = as_decimal(self.cash, "cash")
        if cash < 0:
            raise ContractViolation("cash cannot be negative")
        positions = tuple(sorted(self.positions, key=lambda item: item.instrument.stable_key))
        keys = [position.instrument.stable_key for position in positions]
        if len(keys) != len(set(keys)):
            raise ContractViolation("account positions must be unique by instrument")
        if any(position.instrument.market is not market for position in positions):
            raise ContractViolation("position market must match account market")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "cash", cash)
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "captured_at", ensure_utc(self.captured_at, "captured_at"))


@dataclass(frozen=True, slots=True)
class AccountValuation:
    account: AccountSnapshot
    equity: Decimal | None
    position_values: Mapping[InstrumentId, Decimal]
    missing_prices: tuple[InstrumentId, ...]


def value_account(
    account: AccountSnapshot,
    frozen_prices: Mapping[InstrumentId, Decimal | int | float | str],
) -> AccountValuation:
    """Value an account only when every active position has a frozen price."""
    values: dict[InstrumentId, Decimal] = {}
    missing: list[InstrumentId] = []
    for position in account.positions:
        value = frozen_prices.get(position.instrument)
        if value is None:
            missing.append(position.instrument)
            continue
        price = as_decimal(value, "frozen price")
        if price <= 0:
            missing.append(position.instrument)
            continue
        values[position.instrument] = position.shares * price
    equity = None if missing else account.cash + sum(values.values(), Decimal("0"))
    return AccountValuation(
        account=account,
        equity=equity,
        position_values=MappingProxyType(values),
        missing_prices=tuple(missing),
    )
