"""Decimal 容量和计划止损亏损计算，不处理成交、税务结算或订单。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from tradehelper_v2.contracts import MarketRuleSet


def friction_reserve(shares: Decimal, entry: Decimal, stop: Decimal, rules: MarketRuleSet) -> Decimal:
    if shares <= 0: return Decimal("0")
    buy = max(shares * entry * rules.commission_rate, rules.minimum_commission)
    sell = max(shares * stop * rules.commission_rate, rules.minimum_commission)
    return buy + sell + shares * (entry + stop) * rules.base_slippage_reserve + shares * stop * rules.sell_tax_rate


def cash_required(shares: Decimal, entry: Decimal, rules: MarketRuleSet) -> Decimal:
    if shares <= 0: return Decimal("0")
    return shares * entry + max(shares * entry * rules.commission_rate, rules.minimum_commission) + shares * entry * rules.base_slippage_reserve


def planned_loss(shares: Decimal, entry: Decimal, stop: Decimal, rules: MarketRuleSet) -> Decimal:
    return shares * (entry - stop) + friction_reserve(shares, entry, stop, rules)


def round_lot_down(shares: Decimal, lot: Decimal) -> Decimal:
    return (max(Decimal("0"), shares) / lot).to_integral_value(rounding=ROUND_DOWN) * lot


@dataclass(frozen=True, slots=True)
class EntryCapacityResult:
    shares: Decimal
    risk_cap: Decimal
    cash_cap: Decimal
    single_position_cap: Decimal
    total_stock_cap: Decimal
    binding_reasons: tuple[str, ...]


def entry_capacity_detail(*, equity: Decimal, cash: Decimal, invested: Decimal, current_value: Decimal, existing_shares: Decimal, entry: Decimal, stop: Decimal, risk_budget: Decimal, target_cap: Decimal, rules: MarketRuleSet, is_add: bool) -> EntryCapacityResult:
    """取风险、现金、单票和股票总仓位的最小容量，再处理最低佣金。"""
    zero = Decimal("0")
    if min(equity, entry, risk_budget) <= 0 or stop >= entry:
        return EntryCapacityResult(zero, zero, zero, zero, zero, ("RISK_BUDGET_EXHAUSTED",))
    unit = entry - stop + (entry + stop) * rules.base_slippage_reserve + entry * rules.commission_rate + stop * (rules.commission_rate + rules.sell_tax_rate)
    existing_risk = existing_shares * (entry - stop) if is_add else Decimal("0")
    risk_cap = max(zero, (risk_budget - existing_risk) / unit)
    single_cap = max(zero, (equity * min(target_cap, Decimal("0.25")) - current_value) / entry)
    total_cap = max(zero, (equity * Decimal("0.90") - invested) / entry)
    cash_cap = max(zero, cash / entry)
    raw_min = min(risk_cap, single_cap, total_cap, cash_cap)
    raw = round_lot_down(raw_min, rules.lot_size)
    while raw > 0 and (planned_loss(raw, entry, stop, rules) + existing_risk > risk_budget or cash_required(raw, entry, rules) > cash): raw -= rules.lot_size
    raw = max(zero, raw)
    reasons: list[str] = []
    if risk_cap < rules.lot_size or existing_risk >= risk_budget: reasons.append("RISK_BUDGET_EXHAUSTED" if not is_add else "RISK_ADD_BLOCKED_BY_EXISTING_RISK")
    if cash_cap < rules.lot_size or (raw == 0 and cash_required(rules.lot_size, entry, rules) > cash): reasons.append("RISK_CASH_INSUFFICIENT")
    if single_cap < rules.lot_size: reasons.append("RISK_SINGLE_POSITION_CAP")
    if total_cap < rules.lot_size: reasons.append("RISK_TOTAL_STOCK_CAP")
    if raw == 0: reasons.append("RISK_MIN_LOT_EXCEEDS_CAPACITY")
    return EntryCapacityResult(raw, risk_cap, cash_cap, single_cap, total_cap, tuple(sorted(set(reasons))))


def entry_capacity(**kwargs) -> Decimal:
    """兼容调用方的股数接口；审计信息由 entry_capacity_detail 提供。"""
    return entry_capacity_detail(**kwargs).shares
