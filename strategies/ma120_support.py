"""
策略 P：MA120 支撑反弹。

用于识别价格触碰半年线后的反弹候选。它不是简单“碰线就买”，而是
要求触碰 MA120 后重新站回，或输出待确认观察条件。
"""

import logging

import pandas as pd

from strategies.base import BaseExecutionStrategy, StrategyContext, StrategyDecision, compute_atr, shares_for_risk

logger = logging.getLogger(__name__)


class MA120SupportReboundStrategy(BaseExecutionStrategy):
    """MA120 半年线支撑反弹策略。"""

    suitable_regimes = ["ranging", "transitional"]
    overlay_scope = "always"
    strategy_family = "support_rebound"

    def __init__(
        self,
        touch_buffer: float = 0.01,
        reclaim_buffer: float = 0.0,
        stop_buffer: float = 0.02,
        risk_pct: float = 0.006,
        max_position_pct: float = 0.08,
        min_score: float = -0.35,
    ):
        self.touch_buffer = touch_buffer
        self.reclaim_buffer = reclaim_buffer
        self.stop_buffer = stop_buffer
        self.risk_pct = risk_pct
        self.max_position_pct = max_position_pct
        self.min_score = min_score

    @property
    def name(self) -> str:
        return "P MA120支撑反弹"

    @property
    def description(self) -> str:
        return (
            "价格触碰MA120后重新站回，按小仓位验证支撑；"
            "未站回时仅列为观察候选"
        )

    def diagnose_no_signal(self, df, context) -> list[str]:
        decision = self.generate_decision(df, context)
        return list(decision.missing_conditions or [decision.reason])

    def generate_decision(
        self, df: pd.DataFrame, context: StrategyContext
    ) -> StrategyDecision:
        if df is None or len(df) < 120:
            return StrategyDecision(
                action="invalid", execution_level="D", source=self.name,
                reason="MA120样本不足", missing_conditions=["至少需要120根K线"],
            )

        row = df.iloc[-1]
        close = float(row.get("close", 0) or 0)
        low = float(row.get("low", 0) or 0)
        score = float(row.get("Final_Score", 0) or 0)
        ma120 = row.get("ma_120")
        if ma120 is None or pd.isna(ma120):
            ma120 = df["close"].astype(float).rolling(120).mean().iloc[-1]
        ma120 = float(ma120) if pd.notna(ma120) else 0.0
        if close <= 0 or low <= 0 or ma120 <= 0:
            return StrategyDecision(
                action="invalid", execution_level="D", source=self.name,
                reason="价格或MA120数据不可用",
            )

        touched = low <= ma120 * (1 + self.touch_buffer) and low >= ma120 * (1 - 0.04)
        reclaimed = close >= ma120 * (1 + self.reclaim_buffer)
        stop = ma120 * (1 - self.stop_buffer)
        trigger = ma120 * (1 + self.reclaim_buffer)
        atr = compute_atr(df, 14)
        latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else close * 0.02
        stop = min(stop, low - 0.2 * latest_atr) if low > 0 else stop
        stop = max(stop, 0.01)

        if context.position.shares > 0 and close < ma120 * (1 - self.stop_buffer):
            return StrategyDecision(
                action="sell",
                execution_level="A",
                shares=context.position.shares,
                trigger_price=close,
                stop_loss=stop,
                position_pct=_position_pct(context, close),
                invalidation=f"收盘跌破MA120的{1-self.stop_buffer:.0%}",
                reason=f"跌破MA120支撑：close({close:.2f}) < MA120({ma120:.2f})",
                source=self.name,
            )

        if touched and reclaimed and score >= self.min_score and context.position.shares <= 0:
            shares = shares_for_risk(
                context.equity, context.cash, close, stop, context.market,
                risk_pct=self.risk_pct, max_position_pct=self.max_position_pct,
            )
            position_pct = shares * close / context.equity if context.equity > 0 else 0.0
            max_loss = max(close - stop, 0.0) * shares
            return StrategyDecision(
                action="buy",
                execution_level="B",
                shares=shares,
                trigger_price=close,
                stop_loss=stop,
                take_profit_mode="none",
                take_profit_rule="MA120支撑策略当前仅定义止损和20日时间退出，未定义主动止盈",
                max_loss_amount=max_loss,
                position_pct=position_pct,
                invalidation=f"收盘跌破MA120的{1-self.stop_buffer:.0%}（{stop:.2f}）",
                reason=(
                    f"低点{low:.2f}触碰MA120={ma120:.2f}后收回，"
                    f"Final_Score={score:+.3f}"
                ),
                source=self.name,
                time_stop_days=20,
                hard_stop_pct=0.08,
            )

        missing = []
        if not touched:
            missing.append(f"价格低点需触碰MA120附近（{ma120:.2f}±{self.touch_buffer:.0%}）")
        if touched and not reclaimed:
            missing.append(f"需重新站回MA120={trigger:.2f}")
        if score < self.min_score:
            missing.append(f"Final_Score需≥{self.min_score:+.2f}")
        return StrategyDecision(
            action="watch",
            execution_level="C" if touched else "D",
            trigger_price=trigger,
            stop_loss=stop,
            invalidation=f"跌破{stop:.2f}或5个交易日未站回MA120",
            missing_conditions=missing,
            reason=(
                f"MA120支撑观察：low={low:.2f}, close={close:.2f}, "
                f"MA120={ma120:.2f}"
            ),
            source=self.name,
        )

def _position_pct(context: StrategyContext, price: float) -> float:
    if context.equity <= 0 or price <= 0:
        return 0.0
    return context.position.shares * price / context.equity
