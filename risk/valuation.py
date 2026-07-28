"""同一冻结批次的真实账户估值，绝不以成本价补齐缺失市值。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from contracts import AccountSnapshot, FrozenAccountValuation, PositionValuation, ValuationPrice, ValuationStatus, stable_hash
from contracts.market_data import ensure_utc


def freeze_account_valuation(account: AccountSnapshot, prices: Mapping, valuation_at: datetime, *, generated_at: datetime | None = None) -> FrozenAccountValuation:
    """用已冻结的价格批次估值；任一活跃持仓缺价即为 incomplete。"""
    generated_at = generated_at or datetime.now(timezone.utc)
    valuation_at = ensure_utc(valuation_at, "valuation_at")
    usable: dict = {}
    missing = []
    for position in account.positions:
        quote = prices.get(position.instrument)
        if not isinstance(quote, ValuationPrice) or quote.instrument != position.instrument or quote.observed_at > valuation_at:
            missing.append(position.instrument)
        else:
            usable[position.instrument] = quote
    account_hash = stable_hash(account)
    batch = tuple((instrument.stable_key, quote.price, quote.observed_at, quote.source, quote.price_kind, quote.freshness_status) for instrument, quote in sorted(usable.items(), key=lambda item: item[0].stable_key))
    price_batch_hash = stable_hash(batch)
    identity = {"market": account.market, "currency": account.currency, "account_hash": account_hash, "price_batch_hash": price_batch_hash, "valuation_at": valuation_at}
    valuation_id = stable_hash(identity)
    event_key = f"{account.market.value}|{valuation_at.date().isoformat()}|{valuation_id}"
    if missing:
        partial = tuple(
            PositionValuation(position.instrument, position.shares, usable[position.instrument].price,
                              position.shares * usable[position.instrument].price, None,
                              position.shares * (usable[position.instrument].price - position.cost_price),
                              float(usable[position.instrument].price / position.cost_price - 1) if position.cost_price > 0 else None)
            for position in account.positions if position.instrument in usable
        )
        return FrozenAccountValuation(valuation_id, event_key, account.market, account.currency, account_hash, price_batch_hash, valuation_at, ValuationStatus.INCOMPLETE, None, account.cash, None, None, partial, tuple(missing), generated_at)
    values = []
    invested = Decimal("0")
    for position in account.positions:
        quote = usable[position.instrument]
        market_value = position.shares * quote.price
        invested += market_value
        values.append((position, quote, market_value))
    equity = account.cash + invested
    positions = tuple(PositionValuation(position.instrument, position.shares, quote.price, market_value, float(market_value / equity) if equity else 0.0, market_value - position.shares * position.cost_price, float(quote.price / position.cost_price - 1) if position.cost_price > 0 else None) for position, quote, market_value in values)
    return FrozenAccountValuation(valuation_id, event_key, account.market, account.currency, account_hash, price_batch_hash, valuation_at, ValuationStatus.COMPLETE, equity, account.cash, invested, float(invested / equity) if equity else 0.0, positions, (), generated_at)
