"""
策略D：布林带突破策略 (Bollinger Band Breakout)

收盘价突破布林上轨 + Final_Score 高百分位 = 开仓。
去掉"收窄"前置条件（过于严格），改用布林带宽度占比代替。

规则：
  - 开仓：close > bb_upper AND Final_Score 处于 70% 百分位以上
  - 平仓：close < bb_mid 或 Final_Score 跌破 40% 百分位
  - 止损：bb_mid - 1×ATR
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    BaseExecutionStrategy, Order, Position, StrategyContext,
    compute_atr, compute_percentile_score,
)

logger = logging.getLogger(__name__)


class BollingerBreakoutStrategy(BaseExecutionStrategy):
    """布林带突破策略。

    参数说明：
      entry_pct=0.70 — 百分位需 >= 70%
      exit_pct=0.40  — 跌破 40% 百分位平仓
      atr_mult_stop=1.5 — 止损 ATR 倍数
      cooldown_bars=3
    """


    suitable_regimes = ["ranging"]
    def __init__(self, entry_pct: float = 0.70, exit_pct: float = 0.40,
                 cooldown_bars: int = 3, atr_period: int = 14,
                 atr_mult_stop: float = 1.5, risk_budget: float = 0.02,
                 lookback: int = 252):
        self.entry_pct = entry_pct
        self.exit_pct = exit_pct
        self.cooldown_bars = cooldown_bars
        self.atr_period = atr_period
        self.atr_mult_stop = atr_mult_stop
        self.risk_budget = risk_budget
        self.lookback = lookback

    @property
    def name(self) -> str:
        return f"布林带突破 (score>{self.entry_pct:.0%}分位)"

    @property
    def description(self) -> str:
        return (
            f"收盘价突破布林上轨且 Final_Score 处于 {self.entry_pct:.0%} 分位以上时开仓。"
            f"跌破布林中轨或 {self.exit_pct:.0%} 分位平仓。"
        )

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        if df.empty or "Final_Score" not in df.columns:
            return []

        idx = len(df) - 1
        current_date = str(df.iloc[-1].get("date", ""))[:10]
        has_position = context.position.shares > 0
        close = float(df["close"].iloc[-1])

        pct_series = compute_percentile_score(df, window=self.lookback)
        current_pct = pct_series.iloc[-1]
        if pd.isna(current_pct):
            return []

        if "bb_upper" not in df.columns or "bb_mid" not in df.columns:
            return []
        bb_upper = float(df["bb_upper"].iloc[-1])
        bb_mid = float(df["bb_mid"].iloc[-1])
        bb_lower = float(df["bb_lower"].iloc[-1]) if "bb_lower" in df.columns else bb_mid * 0.95

        # ── 平仓 ──
        if has_position:
            if close < bb_mid:
                logger.info(f"[策略D] {current_date} 平仓 | close({close:.2f}) < BB中轨({bb_mid:.2f})")
                return [Order(date=current_date, action="sell",
                             shares=context.position.shares,
                             reason=f"跌破布林中轨: {close:.2f} < {bb_mid:.2f}")]
            if current_pct < self.exit_pct:
                logger.info(f"[策略D] 平仓 | 百分位={current_pct:.1%} < {self.exit_pct:.0%}")
                return [Order(date=current_date, action="sell",
                             shares=context.position.shares,
                             reason=f"百分位({current_pct:.1%}) < {self.exit_pct:.0%}")]
            return []

        # ── 开仓 ──
        if close <= bb_upper:
            return []
        if current_pct < self.entry_pct:
            return []
        if idx < context.cooldown_until:
            return []

        atr = compute_atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return []

        stop_distance = self.atr_mult_stop * atr
        risk_amount = context.equity * self.risk_budget
        shares = max(int(risk_amount / max(stop_distance, 1e-6) / 100) * 100, 100)
        stop_loss = bb_mid - stop_distance

        logger.info(
            f"[策略D] {current_date} 布林突破开仓 | close={close:.2f} > BB上轨={bb_upper:.2f}"
        )
        return [Order(
            date=current_date, action="buy", shares=shares,
            stop_loss=stop_loss,
            reason=f"布林突破: {close:.2f}>{bb_upper:.2f}, 百分位={current_pct:.1%}",
        )]
