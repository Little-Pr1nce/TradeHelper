"""
策略 S：跌破关键均线后反抽失败退出。

用于已有持仓。价格跌破 MA20/MA60/MA120 后，如果反抽到关键均线附近但
收不回去，说明支撑转压力，策略输出减仓/退出信号。
"""

import pandas as pd

from strategies.base import (
    BaseExecutionStrategy,
    StrategyContext,
    StrategyDecision,
    market_lot_size,
)


class PullbackFailedExitStrategy(BaseExecutionStrategy):
    """关键均线跌破后的反抽失败退出策略。"""

    suitable_regimes: list[str] = []

    def __init__(
        self,
        reclaim_buffer: float = 0.003,
        fail_buffer: float = 0.006,
        negative_score: float = -0.05,
        sell_fraction: float = 0.5,
    ):
        self.reclaim_buffer = reclaim_buffer
        self.fail_buffer = fail_buffer
        self.negative_score = negative_score
        self.sell_fraction = sell_fraction

    @property
    def name(self) -> str:
        return "S 反抽失败退出"

    @property
    def description(self) -> str:
        return "跌破关键均线后反抽无法收回，降低持仓风险"

    def diagnose_no_signal(self, df, context) -> list[str]:
        decision = self.generate_decision(df, context)
        return list(decision.missing_conditions or [decision.reason])

    def generate_decision(
        self, df: pd.DataFrame, context: StrategyContext
    ) -> StrategyDecision:
        if df is None or len(df) < 60:
            return StrategyDecision(action="invalid", execution_level="D", source=self.name,
                                    reason="至少需要60根K线判断反抽失败")
        if context.position.shares <= 0:
            return StrategyDecision(action="watch", execution_level="C", source=self.name,
                                    reason="无持仓，不适用反抽失败退出")

        row = df.iloc[-1]
        close = float(row.get("close", 0) or 0)
        high = float(row.get("high", 0) or 0)
        score = float(row.get("Final_Score", 0) or 0)
        if close <= 0 or high <= 0:
            return StrategyDecision(action="invalid", execution_level="D", source=self.name,
                                    reason="当前价格不可用")

        levels = _key_levels(df)
        failed = []
        for label, level in levels:
            if level <= 0:
                continue
            probed = high >= level * (1 - self.reclaim_buffer)
            rejected = close < level * (1 - self.fail_buffer)
            if probed and rejected:
                failed.append((label, level))

        if failed:
            label, level = failed[0]
            hard_exit = label in ("MA60", "MA120") and score <= self.negative_score
            execution_level = "A" if hard_exit else "B"
            shares = context.position.shares if hard_exit else _partial_sell_shares(
                context.position.shares, context.market, self.sell_fraction
            )
            stop_line = level * (1 + self.reclaim_buffer)
            return StrategyDecision(
                action="sell",
                execution_level=execution_level,
                shares=shares,
                trigger_price=close,
                stop_loss=stop_line,
                position_pct=_position_pct(context, close),
                invalidation=f"重新站回{label}={level:.2f}且Final_Score转正后，退出信号取消",
                reason=(
                    f"反抽{label}={level:.2f}失败：日内高点{high:.2f}接近压力位，"
                    f"收盘{close:.2f}仍低于压力位，Final_Score={score:+.3f}"
                ),
                source=self.name,
            )

        missing = []
        for label, level in levels[:3]:
            if level > 0:
                missing.append(f"若反抽{label}={level:.2f}后收不回，触发退出")
        return StrategyDecision(
            action="hold",
            execution_level="B",
            trigger_price=close,
            invalidation="重新站上MA20/MA60且Alpha转正则维持持仓",
            missing_conditions=missing,
            reason="尚未出现关键均线反抽失败",
            source=self.name,
        )


def _key_levels(df: pd.DataFrame) -> list[tuple[str, float]]:
    row = df.iloc[-1]
    levels = []
    for col, label, window in [
        ("ma_20", "MA20", 20),
        ("ma_60", "MA60", 60),
        ("ma_120", "MA120", 120),
    ]:
        value = row.get(col)
        if value is None or pd.isna(value):
            if len(df) >= window:
                value = df["close"].astype(float).rolling(window).mean().iloc[-1]
        try:
            value = float(value) if value is not None and pd.notna(value) else 0.0
        except Exception:
            value = 0.0
        levels.append((label, value))
    return levels


def _partial_sell_shares(shares: int, market: str, fraction: float) -> int:
    if shares <= 0 or fraction <= 0:
        return 0
    lot = market_lot_size(market)
    raw = int(shares * fraction)
    if market == "A":
        rounded = int(raw / lot) * lot
        return rounded if rounded >= lot else shares
    return max(min(raw, shares), 1)


def _position_pct(context: StrategyContext, price: float) -> float:
    if context.equity <= 0 or price <= 0:
        return 0.0
    return context.position.shares * price / context.equity
