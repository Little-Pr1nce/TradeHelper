"""
策略 R：真实持仓风险管理。

该策略专门服务 Tab3：用户已经有持仓时，按成本、当前价、Alpha 分数和
集中度判断是否止损、减仓、禁止加仓。它不负责开新仓。
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


class PositionRiskManagementStrategy(BaseExecutionStrategy):
    """真实持仓组合风控策略。"""

    suitable_regimes: list[str] = []
    overlay_scope = "position"
    signal_intent = "risk_exit"
    strategy_family = "position_risk"

    def __init__(
        self,
        hard_loss_pct: float = 0.20,
        soft_loss_pct: float = 0.08,
        concentration_pct: float = 0.30,
        negative_score: float = -0.12,
    ):
        self.hard_loss_pct = hard_loss_pct
        self.soft_loss_pct = soft_loss_pct
        self.concentration_pct = concentration_pct
        self.negative_score = negative_score

    @property
    def name(self) -> str:
        return "R 持仓风险管理"

    @property
    def description(self) -> str:
        return "基于真实成本、浮盈亏、仓位集中度和Alpha分数管理已有持仓"

    def diagnose_no_signal(self, df, context) -> list[str]:
        decision = self.generate_decision(df, context)
        return list(decision.missing_conditions or [decision.reason])

    def generate_decision(
        self, df: pd.DataFrame, context: StrategyContext
    ) -> StrategyDecision:
        if df is None or df.empty:
            return StrategyDecision(action="invalid", execution_level="D", source=self.name,
                                    reason="K线数据不可用")
        if context.position.shares <= 0 or context.position.avg_cost <= 0:
            return StrategyDecision(action="watch", execution_level="C", source=self.name,
                                    reason="无持仓，不适用持仓风控")

        row = df.iloc[-1]
        close = float(row.get("close", 0) or 0)
        score = float(row.get("Final_Score", 0) or 0)
        ma20 = float(row.get("ma_20", 0) or 0)
        ma60 = float(row.get("ma_60", 0) or 0)
        if close <= 0:
            return StrategyDecision(action="invalid", execution_level="D", source=self.name,
                                    reason="当前价不可用")

        pnl_pct = (close - context.position.avg_cost) / context.position.avg_cost
        position_pct = (
            context.position.shares * close / context.equity
            if context.equity > 0 else 0.0
        )
        reasons = []
        if pnl_pct <= -self.hard_loss_pct:
            reasons.append(f"浮亏{pnl_pct:.1%}超过硬止损线{self.hard_loss_pct:.0%}")
        elif pnl_pct <= -self.soft_loss_pct:
            reasons.append(f"浮亏{pnl_pct:.1%}跌破软止损线{self.soft_loss_pct:.0%}")
        if score <= self.negative_score:
            reasons.append(f"Final_Score={score:+.3f}偏空")
        if position_pct >= self.concentration_pct and (score < 0 or pnl_pct < 0):
            reasons.append(f"单票仓位{position_pct:.1%}过高且缺少正向确认")

        stop_line = max(
            context.position.avg_cost * (1 - self.soft_loss_pct),
            ma20 * 0.98 if ma20 > 0 else 0,
        )
        if ma60 > 0 and close < ma60 * 0.98 and score < 0:
            reasons.append(f"价格跌破MA60风控线{ma60*0.98:.2f}")

        if reasons:
            hard = pnl_pct <= -self.hard_loss_pct and score <= self.negative_score
            level = "A" if hard else "B"
            shares = context.position.shares if hard else _partial_sell_shares(
                context.position.shares, context.market, 0.5
            )
            action_reason = "；".join(reasons[:3])
            return StrategyDecision(
                action="sell",
                execution_level=level,
                shares=shares,
                trigger_price=close,
                stop_loss=stop_line,
                position_pct=position_pct,
                invalidation="重新站回MA20/MA60且Final_Score转正后重新评估",
                reason=action_reason,
                source=self.name,
            )

        missing = []
        if pnl_pct > -self.soft_loss_pct:
            missing.append(f"未跌破软止损线{-self.soft_loss_pct:.0%}")
        if score > self.negative_score:
            missing.append("Alpha未明显偏空")
        return StrategyDecision(
            action="hold",
            execution_level="B",
            trigger_price=close,
            stop_loss=stop_line,
            position_pct=position_pct,
            invalidation=f"跌破{stop_line:.2f}或Alpha转空则降级",
            missing_conditions=missing,
            reason=f"持仓风险未触发：浮盈亏{pnl_pct:+.1%}，仓位{position_pct:.1%}",
            source=self.name,
        )


def _partial_sell_shares(shares: int, market: str, fraction: float) -> int:
    if shares <= 0:
        return 0
    lot = market_lot_size(market)
    raw = int(shares * fraction)
    if market == "A":
        rounded = int(raw / lot) * lot
        return rounded if rounded >= lot else shares
    return max(min(raw, shares), 1)
