"""
策略F：海龟ATR通道突破 (Turtle ATR Channel Breakout)

经典海龟交易法则变体。用 Donchian 通道 + ATR 止损。

修复：去掉复杂的金字塔加仓逻辑（与回测引擎的 Position 模型不兼容），
简化为单次开仓 + ATR 止损 + Donchian 退出。

规则：
  - 开仓：close > 20日最高价 AND Final_Score 处于 75% 分位以上
  - 平仓：close < 10日最低价 或 Final_Score 跌破 40% 分位
  - 止损：开仓价 - 2×ATR(20)
  - 仓位：风险预算 2% / (2×ATR)
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    BaseExecutionStrategy, Order, Position, StrategyContext,
    compute_atr, compute_percentile_score,
)

logger = logging.getLogger(__name__)


class TurtleATRStrategy(BaseExecutionStrategy):
    """海龟ATR通道突破（简化版）。

    参数说明：
      entry_pct=0.75    — 百分位 >= 75%
      exit_pct=0.40     — 跌破 40% 百分位平仓
      channel_n=20      — 入场通道
      exit_channel_n=10 — 退出通道
      atr_period=20
      atr_mult_stop=2.0
      cooldown_bars=3
    """


    suitable_regimes = ["trending_volatile"]

    def tunable_params(self) -> list[dict]:
        return [
            {"name": "entry_pct", "default": self.entry_pct, "values": [0.65, 0.70, 0.75, 0.80]},
            {"name": "atr_mult_stop", "default": self.atr_mult_stop, "values": [1.5, 2.0, 2.5]},
        ]

    def __init__(self, entry_pct: float = 0.75, exit_pct: float = 0.40,
                 channel_n: int = 20, exit_channel_n: int = 10,
                 atr_period: int = 20, atr_mult_stop: float = 2.0,
                 cooldown_bars: int = 3, risk_budget: float = 0.02,
                 score_lookback: int = 252):
        self.entry_pct = entry_pct
        self.exit_pct = exit_pct
        self.channel_n = channel_n
        self.exit_channel_n = exit_channel_n
        self.atr_period = atr_period
        self.atr_mult_stop = atr_mult_stop
        self.cooldown_bars = cooldown_bars
        self.risk_budget = risk_budget
        self.score_lookback = score_lookback

    @property
    def name(self) -> str:
        return f"海龟ATR通道 (N={self.channel_n}, 止损{self.atr_mult_stop}×ATR)"

    @property
    def description(self) -> str:
        return (
            f"海龟变体：close > {self.channel_n}日高点 + "
            f"百分位 >= {self.entry_pct:.0%} 开仓。"
            f"跌破 {self.exit_channel_n}日低点 或 {self.exit_pct:.0%} 百分位平仓。"
        )

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        if df.empty or "Final_Score" not in df.columns:
            return []

        idx = len(df) - 1
        current_date = str(df.iloc[-1].get("date", ""))[:10]
        has_position = context.position.shares > 0
        close = float(df["close"].iloc[-1])

        pct_series = compute_percentile_score(df, window=self.score_lookback)
        current_pct = pct_series.iloc[-1]
        if pd.isna(current_pct):
            return []

        # ── Donchian ──
        n = min(self.channel_n, len(df) - 1)
        if n <= 0:
            return []
        entry_upper = float(df["high"].iloc[-(n + 1):-1].max())

        exit_n = min(self.exit_channel_n, len(df) - 1)
        exit_lower = float(df["low"].iloc[-(exit_n + 1):-1].min()) if exit_n > 0 else 0

        atr = compute_atr(df, self.atr_period).iloc[-1]

        # ── 平仓 ──
        if has_position:
            if exit_lower > 0 and close < exit_lower:
                logger.info(f"[策略F] {current_date} 平仓 | close({close:.2f}) < {self.exit_channel_n}日低({exit_lower:.2f})")
                return [Order(date=current_date, action="sell",
                             shares=context.position.shares,
                             reason=f"跌破{self.exit_channel_n}日低: {close:.2f} < {exit_lower:.2f}")]
            if current_pct < self.exit_pct:
                logger.info(f"[策略F] 平仓 | 百分位={current_pct:.1%} < {self.exit_pct:.0%}")
                return [Order(date=current_date, action="sell",
                             shares=context.position.shares,
                             reason=f"百分位({current_pct:.1%}) < {self.exit_pct:.0%}")]
            return []

        # ── 开仓 ──
        if close <= entry_upper:
            return []
        if current_pct < self.entry_pct:
            return []
        if idx < context.cooldown_until:
            return []
        if pd.isna(atr) or atr <= 0:
            return []

        stop_distance = self.atr_mult_stop * atr
        risk_amount = context.equity * self.risk_budget
        shares = max(int(risk_amount / max(stop_distance, 1e-6) / 100) * 100, 100)
        stop_loss = close - stop_distance

        logger.info(
            f"[策略F] {current_date} 海龟开仓 | close={close:.2f} > {self.channel_n}日高={entry_upper:.2f} | 股数={shares}"
        )
        return [Order(
            date=current_date, action="buy", shares=shares,
            stop_loss=stop_loss,
            reason=f"海龟: close>{entry_upper:.2f}, 百分位={current_pct:.1%}",
        )]
