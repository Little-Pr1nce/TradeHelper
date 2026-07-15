"""执行前的最后市场规则检查；这些约束不可由优化层关闭。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from tradehelper_v2.contracts.enums import Market
from tradehelper_v2.contracts.execution import ExecutionEvent, ExecutionState, FillOutcome, OrderIntent, OrderSide, TradingStatus
from tradehelper_v2.contracts.risk import InstrumentClassification, MarketRuleSet


@dataclass(frozen=True, slots=True)
class MarketCheck:
    permitted_shares: Decimal; outcome: FillOutcome | None; reason_codes: tuple[str, ...]


class ExecutionMarketRules:
    @staticmethod
    def check(intent: OrderIntent, state: ExecutionState, event: ExecutionEvent, rules: MarketRuleSet) -> MarketCheck:
        reasons: list[str] = []
        shares = intent.requested_shares
        if state.market is not intent.instrument.market or rules.market is not state.market:
            return MarketCheck(Decimal("0"), FillOutcome.REJECTED, ("EXEC_POSITION_MISMATCH",))
        if event.trading_status is TradingStatus.SUSPENDED:
            return MarketCheck(Decimal("0"), FillOutcome.REJECTED, ("EXEC_SUSPENDED",))
        if event.trading_status is TradingStatus.UNKNOWN:
            return MarketCheck(Decimal("0"), FillOutcome.UNVERIFIABLE, ("EXEC_TRADING_STATUS_UNKNOWN",))
        if event.volume == 0:
            return MarketCheck(Decimal("0"), FillOutcome.REJECTED, ("EXEC_NO_TRADABLE_VOLUME",))
        if intent.side is OrderSide.SELL:
            shares = min(shares, state.position_shares)
            if rules.market is Market.A and rules.same_day_sell_restricted and state.acquired_session_date == event.session_date:
                return MarketCheck(Decimal("0"), FillOutcome.REJECTED, ("EXEC_T1_BLOCKED",))
            if state.sellable_shares is not None:
                shares = min(shares, state.sellable_shares)
                if shares < intent.requested_shares: reasons.append("EXEC_PARTIAL_SELLABLE")
            if shares <= 0: return MarketCheck(Decimal("0"), FillOutcome.REJECTED, ("EXEC_POSITION_MISMATCH",))
        if rules.market is Market.A:
            # 全部平仓可卖出零股；其余行为必须 100 股向下取整。
            full_exit = intent.action.value == "sell" and shares == state.position_shares
            rounded = shares if full_exit and intent.side is OrderSide.SELL else (shares / rules.lot_size).to_integral_value(rounding=ROUND_DOWN) * rules.lot_size
            if rounded != shares: reasons.append("EXEC_LOT_ROUNDED")
            shares = rounded
            if shares <= 0: return MarketCheck(Decimal("0"), FillOutcome.REJECTED, tuple(sorted(set(reasons + ["EXEC_POSITION_MISMATCH"]))))
            if rules.instrument_classification is InstrumentClassification.UNKNOWN:
                change = None if event.previous_close is None else abs(event.open / event.previous_close - Decimal("1"))
                if change is None or change >= Decimal("0.049"):
                    return MarketCheck(Decimal("0"), FillOutcome.UNVERIFIABLE, ("EXEC_LIMIT_QUEUE_UNVERIFIABLE",))
            elif event.previous_close is not None and rules.price_limit_pct is not None:
                upper = (event.previous_close * (Decimal("1") + Decimal(str(rules.price_limit_pct)))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                lower = (event.previous_close * (Decimal("1") - Decimal(str(rules.price_limit_pct)))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if (intent.side is OrderSide.BUY and event.open >= upper) or (intent.side is OrderSide.SELL and event.open <= lower):
                    return MarketCheck(Decimal("0"), FillOutcome.UNVERIFIABLE, ("EXEC_LIMIT_QUEUE_UNVERIFIABLE",))
        return MarketCheck(shares, None, tuple(sorted(set(reasons))))
