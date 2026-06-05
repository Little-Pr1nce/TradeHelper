"""
策略E：Dual Thrust 日内突破策略 (日线适配版)

修正：使用前一日的 open（而不是当日的 open）来构建通道，
遵循严格的 T 日信号 → T+1 开盘撮合的防偷窥铁律。

规则：
  - Range = max(HH - LC, HC - LL), N=20
  - 上轨 = prev_open + K1 × Range
  - 下轨 = prev_open - K2 × Range
  - 开仓：close > 上轨 AND Final_Score 处于 70% 百分位以上
  - 平仓：close < 下轨 或 Final_Score < 40% 百分位
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    BaseExecutionStrategy, Order, Position, StrategyContext,
    compute_atr, compute_percentile_score,
)

logger = logging.getLogger(__name__)


class DualThrustStrategy(BaseExecutionStrategy):
    """Dual Thrust 日线适配版。

    参数说明：
      entry_pct=0.70  — 百分位 >= 70%
      exit_pct=0.40   — 跌破 40% 百分位平仓
      lookback_n=20   — N 日窗口
      k1=0.7, k2=0.7 — 突破系数
      cooldown_bars=3
    """


    suitable_regimes = ["trending_volatile"]

    def tunable_params(self) -> list[dict]:
        return [{"name": "entry_pct", "default": self.entry_pct, "values": [0.6, 0.65, 0.7, 0.75, 0.8]}]

    def __init__(self, entry_pct: float = 0.70, exit_pct: float = 0.40,
                 lookback_n: int = 20, k1: float = 0.2, k2: float = 0.2,
                 cooldown_bars: int = 3, atr_period: int = 14,
                 risk_budget: float = 0.02, score_lookback: int = 252):
        self.entry_pct = entry_pct
        self.exit_pct = exit_pct
        self.lookback_n = lookback_n
        self.k1 = k1
        self.k2 = k2
        self.cooldown_bars = cooldown_bars
        self.atr_period = atr_period
        self.risk_budget = risk_budget
        self.score_lookback = score_lookback

    @property
    def name(self) -> str:
        return f"Dual Thrust突破 (N={self.lookback_n}, K1={self.k1})"

    @property
    def description(self) -> str:
        return (
            f"经典 Dual Thrust 日线适配：近 {self.lookback_n} 日 Range×K 构建突破轨道。"
            f"用 T-1 日 open 防偷窥。"
        )

    def _calc_range(self, df: pd.DataFrame) -> float:
        """用 T-1 日之前的数据计算 Range（不含当日）。"""
        n = min(self.lookback_n, len(df) - 1)
        if n <= 0:
            return 0.0
        window = df.iloc[-(n + 1):-1]  # 不含当前 bar
        hh = float(window["high"].max())
        hc = float(window["close"].max())
        lc = float(window["close"].min())
        ll = float(window["low"].min())
        return max(hh - lc, hc - ll)

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

        # 用上一根 bar 的 open 作为基准（T 日收盘时可拿到 T 日 open，但同时可用 T-1 open）
        if idx >= 1:
            prev_open = float(df["open"].iloc[idx - 1])
        else:
            prev_open = float(df["open"].iloc[idx])

        range_val = self._calc_range(df)
        if range_val <= 0:
            return []
        upper_band = prev_open + self.k1 * range_val
        lower_band = prev_open - self.k2 * range_val

        # ── 平仓 ──
        if has_position:
            if close < lower_band:
                logger.info(f"[策略E] {current_date} 平仓 | close({close:.2f}) < DT下轨({lower_band:.2f})")
                return [Order(date=current_date, action="sell",
                             shares=context.position.shares,
                             reason=f"跌破DT下轨: {close:.2f} < {lower_band:.2f}")]
            if current_pct < self.exit_pct:
                logger.info(f"[策略E] 平仓 | 百分位={current_pct:.1%} < {self.exit_pct:.0%}")
                return [Order(date=current_date, action="sell",
                             shares=context.position.shares,
                             reason=f"百分位({current_pct:.1%}) < {self.exit_pct:.0%}")]
            return []

        # ── 开仓 ──
        if close <= upper_band:
            return []
        if current_pct < self.entry_pct:
            return []
        if idx < context.cooldown_until:
            return []

        atr = compute_atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return []

        stop_distance = 2 * atr
        risk_amount = context.equity * self.risk_budget
        shares = max(int(risk_amount / max(stop_distance, 1e-6) / 100) * 100, 100)
        stop_loss = close - stop_distance

        logger.info(
            f"[策略E] {current_date} DT突破开仓 | close={close:.2f} > 上轨={upper_band:.2f}"
        )
        return [Order(
            date=current_date, action="buy", shares=shares,
            stop_loss=stop_loss,
            reason=f"DT突破: {close:.2f}>{upper_band:.2f}, 百分位={current_pct:.1%}",
        )]
