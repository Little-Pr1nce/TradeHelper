"""
策略 Q：冲高回落锁利。

用于已有持仓。当价格接近阶段新高后从高点明显回落，且账户仍有浮盈，
策略输出部分止盈/上移止损信号。
"""

import logging

import pandas as pd

from strategies.base import (
    BaseExecutionStrategy,
    StrategyContext,
    StrategyDecision,
    market_lot_size,
)

logger = logging.getLogger(__name__)


class ProfitLockAfterHighStrategy(BaseExecutionStrategy):
    """冲高回落后的利润保护策略。"""

    suitable_regimes: list[str] = []
    overlay_scope = "position"
    signal_intent = "profit_lock"
    strategy_family = "profit_lock"

    def __init__(
        self,
        lookback: int = 120,
        high_tolerance: float = 0.005,
        pullback_pct: float = 0.035,
        min_profit_pct: float = 0.10,
        sell_fraction: float = 0.5,
    ):
        self.lookback = lookback
        self.high_tolerance = high_tolerance
        self.pullback_pct = pullback_pct
        self.min_profit_pct = min_profit_pct
        self.sell_fraction = sell_fraction

    @property
    def name(self) -> str:
        return "Q 冲高回落锁利"

    @property
    def description(self) -> str:
        return "接近阶段新高后回落且仍有浮盈时，部分止盈或上移止损"

    def diagnose_no_signal(self, df, context) -> list[str]:
        decision = self.generate_decision(df, context)
        return list(decision.missing_conditions or [decision.reason])

    def generate_decision(
        self, df: pd.DataFrame, context: StrategyContext
    ) -> StrategyDecision:
        if df is None or len(df) < 20:
            return StrategyDecision(action="invalid", execution_level="D", source=self.name,
                                    reason="K线样本不足")
        if context.position.shares <= 0 or context.position.avg_cost <= 0:
            return StrategyDecision(action="watch", execution_level="C", source=self.name,
                                    reason="无持仓，不适用锁利策略")

        row = df.iloc[-1]
        high = float(row.get("high", 0) or 0)
        close = float(row.get("close", 0) or 0)
        if high <= 0 or close <= 0:
            return StrategyDecision(action="invalid", execution_level="D", source=self.name,
                                    reason="价格数据不可用")

        recent = df.tail(min(len(df), self.lookback))
        period_high = float(recent["high"].astype(float).max())
        profit_pct = (close - context.position.avg_cost) / context.position.avg_cost
        pullback = (close - high) / high if high > 0 else 0.0
        near_high = period_high > 0 and high >= period_high * (1 - self.high_tolerance)
        lock_line = high * (1 - self.pullback_pct)

        if near_high and pullback <= -self.pullback_pct and profit_pct >= self.min_profit_pct:
            sell_shares = _partial_sell_shares(context.position.shares, context.market, self.sell_fraction)
            max_loss = max(close - lock_line, 0.0) * max(context.position.shares - sell_shares, 0)
            return StrategyDecision(
                action="sell",
                # 事实和风险条件成立，但历史正期望需由统一风控层确认。
                execution_level="B",
                shares=sell_shares,
                trigger_price=close,
                stop_loss=lock_line,
                max_loss_amount=max_loss,
                position_pct=_position_pct(context, close),
                invalidation=f"重新突破阶段高点{high:.2f}且回落小于2%，锁利信号取消",
                reason=(
                    f"当日高点{high:.2f}接近{self.lookback}日高点{period_high:.2f}，"
                    f"收盘回落{pullback:.1%}，浮盈{profit_pct:.1%}，卖出约{self.sell_fraction:.0%}锁利"
                ),
                source=self.name,
            )

        missing = []
        if not near_high:
            missing.append(f"需接近{self.lookback}日高点{period_high:.2f}")
        if pullback > -self.pullback_pct:
            missing.append(f"需从日内高点回落≥{self.pullback_pct:.1%}")
        if profit_pct < self.min_profit_pct:
            missing.append(f"浮盈需≥{self.min_profit_pct:.0%}")
        return StrategyDecision(
            action="hold",
            execution_level="B" if profit_pct > 0 else "C",
            trigger_price=lock_line,
            stop_loss=lock_line,
            invalidation="无明显冲高回落，继续按原持仓规则管理",
            missing_conditions=missing,
            reason=f"锁利条件未触发：浮盈{profit_pct:.1%}, 回落{pullback:.1%}",
            source=self.name,
        )


def _partial_sell_shares(shares: int, market: str, fraction: float) -> int:
    if shares <= 0 or fraction <= 0:
        return 0
    lot = market_lot_size(market)
    raw = int(shares * fraction)
    if raw <= 0:
        return shares
    if market == "A":
        rounded = int(raw / lot) * lot
        return rounded if rounded >= lot else shares
    return max(min(raw, shares), 1)


def _position_pct(context: StrategyContext, price: float) -> float:
    if context.equity <= 0 or price <= 0:
        return 0.0
    return context.position.shares * price / context.equity
