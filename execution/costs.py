"""成交仿真的 Decimal 滑点、容量和费用计算。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

from contracts.enums import Market
from contracts.execution import ExecutionEvidenceGrade, ExecutionPolicy, LiquidityEvidence, OrderSide
from contracts.risk import MarketRuleSet
from contracts.market_data import ContractViolation, ensure_utc


@dataclass(frozen=True, slots=True)
class CostEstimate:
    fillable_shares: Decimal; unfilled_shares: Decimal; slippage_rate: Decimal; fill_price: Decimal
    gross_value: Decimal; commission: Decimal; sell_tax: Decimal; total_fee: Decimal; cash_delta: Decimal
    evidence_grade: ExecutionEvidenceGrade; reason_codes: tuple[str, ...]


def _up(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_CEILING)


class CostModel:
    @staticmethod
    def estimate(*, side: OrderSide, raw_price: Decimal, requested_shares: Decimal, market_rules: MarketRuleSet, policy: ExecutionPolicy, liquidity: LiquidityEvidence, event_at, evidence_grade: ExecutionEvidenceGrade) -> CostEstimate:
        reasons = ["EXEC_BASE_SLIPPAGE_APPLIED"]
        event_at = ensure_utc(event_at, "cost event_at")
        requested = Decimal(requested_shares); raw_price = Decimal(raw_price)
        if requested <= 0 or raw_price <= 0: raise ContractViolation("cost estimate needs positive price and shares")
        if liquidity.cutoff_at > event_at: raise ContractViolation("liquidity evidence is from the future")
        if not (market_rules.effective_from <= event_at and (market_rules.effective_to is None or event_at < market_rules.effective_to)):
            raise ContractViolation("market rules are not effective at execution time")
        available = liquidity.median_daily_volume_20
        if available is None:
            fillable, unfilled, liquidity_extra = requested, Decimal("0"), policy.missing_liquidity_reserve
            grade = ExecutionEvidenceGrade.LOW if evidence_grade is not ExecutionEvidenceGrade.INSUFFICIENT else evidence_grade
            reasons.extend(("EXEC_LIQUIDITY_EVIDENCE_MISSING", "EXEC_LIQUIDITY_SLIPPAGE_APPLIED", "EXEC_EVIDENCE_LOW"))
        else:
            maximum = (available * policy.max_participation / market_rules.lot_size).to_integral_value(rounding=ROUND_DOWN) * market_rules.lot_size
            fillable, unfilled = min(requested, maximum), max(requested - maximum, Decimal("0"))
            participation = Decimal("0") if available == 0 else fillable / available
            ratio = max(participation - policy.free_participation, Decimal("0")) / (policy.max_participation - policy.free_participation)
            liquidity_extra = min(ratio * policy.max_liquidity_extra, policy.max_liquidity_extra)
            grade = evidence_grade
            if liquidity_extra: reasons.append("EXEC_LIQUIDITY_SLIPPAGE_APPLIED")
            if unfilled: reasons.extend(("EXEC_LIQUIDITY_CAPPED", "EXEC_UNFILLED_REMAINDER"))
        vol = liquidity.annualized_volatility_20
        volatility_extra = Decimal("0") if vol is None else min(max(vol - policy.volatility_threshold, Decimal("0")) * policy.volatility_factor, policy.max_volatility_extra)
        if volatility_extra: reasons.append("EXEC_VOLATILITY_SLIPPAGE_APPLIED")
        slippage = policy.base_slippage + volatility_extra + liquidity_extra
        quantum = policy.a_fill_quantum if market_rules.market is Market.A else policy.us_fill_quantum
        price = raw_price * (Decimal("1") + slippage if side is OrderSide.BUY else Decimal("1") - slippage)
        fill_price = _up(price, quantum) if side is OrderSide.BUY else price.quantize(quantum, rounding=ROUND_DOWN)
        gross = fill_price * fillable
        commission = _up(max(gross * market_rules.commission_rate, market_rules.minimum_commission), policy.currency_quantum) if fillable else Decimal("0")
        sell_tax = _up(gross * market_rules.sell_tax_rate, policy.currency_quantum) if side is OrderSide.SELL and fillable else Decimal("0")
        fee = commission + sell_tax
        cash = -(gross + fee) if side is OrderSide.BUY else gross - fee
        if fillable: reasons.append("EXEC_COMMISSION_APPLIED")
        if sell_tax: reasons.append("EXEC_SELL_TAX_APPLIED")
        return CostEstimate(fillable, unfilled, slippage, fill_price, gross, commission, sell_tax, fee, cash, grade, tuple(sorted(set(reasons))))
