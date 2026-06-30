"""
策略 T：统一条件触发计划。

该策略不直接下单，专门把当前 K 线状态转成结构化的买入、卖出和失效
条件，供报告和风控官使用。它保证盘中/盘前/盘后都至少有一套可盯盘
条件，而不是只输出“观望”。
"""

import pandas as pd

from strategies.base import BaseExecutionStrategy, StrategyContext, StrategyDecision


class ConditionalTriggerStrategy(BaseExecutionStrategy):
    """统一生成条件触发计划，不直接产生交易订单。"""

    suitable_regimes: list[str] = []
    overlay_scope = "always"

    @property
    def name(self) -> str:
        return "T 条件触发计划"

    @property
    def description(self) -> str:
        return "统一输出买入/加仓、卖出/减仓、持有和失效条件"

    def diagnose_no_signal(self, df, context) -> list[str]:
        decision = self.generate_decision(df, context)
        return list(decision.missing_conditions or [decision.reason])

    def generate_decision(
        self, df: pd.DataFrame, context: StrategyContext
    ) -> StrategyDecision:
        if df is None or len(df) < 20:
            return StrategyDecision(action="invalid", execution_level="D", source=self.name,
                                    reason="K线样本不足，无法生成条件计划")
        row = df.iloc[-1]
        close = float(row.get("close", 0) or 0)
        score = float(row.get("Final_Score", 0) or 0)
        if close <= 0:
            return StrategyDecision(action="invalid", execution_level="D", source=self.name,
                                    reason="当前价不可用")

        ma20 = _ma(row, df, "ma_20", 20)
        ma60 = _ma(row, df, "ma_60", 60)
        ma120 = _ma(row, df, "ma_120", 120)
        support = max([v for v in (ma20 * 0.98, ma60 * 0.98, ma120 * 0.98) if v > 0], default=close * 0.92)
        resistance_candidates = [v for v in (ma20, ma60, ma120) if v > close * 1.002]
        resistance = min(resistance_candidates) if resistance_candidates else close * 1.03
        trigger = max(resistance, close * 1.01)

        missing = [
            f"买入/加仓：站上{trigger:.2f}且Final_Score保持为正",
            f"卖出/减仓：跌破{support:.2f}或Final_Score转负",
            "持有：价格维持在支撑线上方且未出现数据质量阻断",
        ]
        if context.position.shares > 0:
            action = "hold"
            level = "B" if score >= -0.05 else "C"
            reason = (
                f"持仓条件计划：现价{close:.2f}，支撑{support:.2f}，"
                f"突破/加仓触发{trigger:.2f}，Final_Score={score:+.3f}"
            )
            invalidation = f"收盘跌破{support:.2f}或策略健康度降级为demote"
        else:
            action = "watch"
            level = "C"
            reason = (
                f"空仓/关注条件计划：现价{close:.2f}，需突破{trigger:.2f}并确认Alpha为正"
            )
            invalidation = f"跌破{support:.2f}或5个交易日内未触发则重新评估"

        return StrategyDecision(
            action=action,
            execution_level=level,
            trigger_price=trigger,
            stop_loss=support,
            position_pct=_position_pct(context, close),
            invalidation=invalidation,
            missing_conditions=missing,
            reason=reason,
            source=self.name,
        )


def _ma(row, df: pd.DataFrame, col: str, window: int) -> float:
    value = row.get(col)
    if value is None or pd.isna(value):
        if len(df) >= window:
            value = df["close"].astype(float).rolling(window).mean().iloc[-1]
    try:
        return float(value) if value is not None and pd.notna(value) else 0.0
    except Exception:
        return 0.0


def _position_pct(context: StrategyContext, price: float) -> float:
    if context.equity <= 0 or price <= 0 or context.position.shares <= 0:
        return 0.0
    return context.position.shares * price / context.equity
