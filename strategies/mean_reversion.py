"""
策略B：波动率自适应均值回归策略 (Volatility-Adaptive Mean Reversion)

也改为百分位模式，解决单边牛市 Final_Score 永远跌不破 -0.5 的问题。

规则：
  - 开仓：Final_Score 处于近 252 日 20% 百分位以下（超卖）
          且 20 日波动率处于历史后 30% 分位（低波）
  - 平仓：Final_Score 回升至 50% 百分位以上，或浮盈达到 3 × ATR
  - 冷却期：5 根 K 线
  - 仓位：反波动率加权
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    BaseExecutionStrategy, Order, Position, StrategyContext,
    compute_atr, compute_percentile_score, round_lot_shares,
)

logger = logging.getLogger(__name__)


def _rolling_volatility(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """计算滚动年化波动率。"""
    returns = df["close"].pct_change()
    return returns.rolling(window=period).std() * np.sqrt(252)


class MeanReversionStrategy(BaseExecutionStrategy):
    """波动率自适应均值回归（百分位版）。

    参数说明：
      entry_pct=0.20    — 需处于近 252 日 20% 百分位以下才视为超卖
      exit_pct=0.50     — 回升至 50% 百分位以上平仓
      vol_percentile=0.30 — 当前波动率需处于历史后 30% 分位
      cooldown_bars=5    — 冷却期
      atr_mult_tp=3.0    — 止盈：浮盈 3×ATR
      lookback=252       — 百分位回溯窗口
    """

    suitable_regimes = ["ranging", "transitional"]

    def tunable_params(self) -> list[dict]:
        return [
            {"name": "entry_pct", "default": self.entry_pct, "values": [0.10, 0.15, 0.20, 0.25]},
            {"name": "vol_percentile", "default": self.vol_percentile, "values": [0.20, 0.30, 0.40]},
        ]


    def __init__(self, entry_pct: float = 0.20, exit_pct: float = 0.50,
                 vol_window: int = 20, vol_percentile: float = 0.30,
                 cooldown_bars: int = 5, atr_period: int = 14,
                 atr_mult_tp: float = 3.0, base_risk_budget: float = 0.02,
                 risk_min: float = 0.01, risk_max: float = 0.04,
                 vol_lookback: int = 252, lookback: int = 252):
        self.entry_pct = entry_pct
        self.exit_pct = exit_pct
        self.vol_window = vol_window
        self.vol_percentile = vol_percentile
        self.cooldown_bars = cooldown_bars
        self.atr_period = atr_period
        self.atr_mult_tp = atr_mult_tp
        self.base_risk_budget = base_risk_budget
        self.risk_min = risk_min
        self.risk_max = risk_max
        self.vol_lookback = vol_lookback
        self.lookback = lookback

    @property
    def name(self) -> str:
        return f"波动率自适应均值回归 (score<{self.entry_pct:.0%}分位, vol<p{int(self.vol_percentile*100)})"

    @property
    def description(self) -> str:
        return (
            f"滚动 {self.lookback} 日百分位模式："
            f"Final_Score 处于 {self.entry_pct:.0%} 分位以下且波动率处于历史后 "
            f"{int(self.vol_percentile*100)}% 分位时开仓。反波动率加权，冷却期 {self.cooldown_bars} 根。"
        )

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        if df.empty or "Final_Score" not in df.columns:
            return []

        idx = len(df) - 1
        current_date = str(df.iloc[-1].get("date", ""))[:10]
        has_position = context.position.shares > 0
        close = float(df["close"].iloc[-1])

        # 计算百分位
        pct_series = compute_percentile_score(df, window=self.lookback)
        current_pct = pct_series.iloc[-1]
        if pd.isna(current_pct):
            return []

        # ── 平仓条件 ──
        if has_position:
            atr = compute_atr(df, self.atr_period).iloc[-1]
            entry_price = context.position.entry_price
            profit_atr = (close - entry_price) / atr if (pd.notna(atr) and atr > 0) else 0

            # 信号反转
            if current_pct > self.exit_pct:
                logger.info(
                    f"[策略B] {current_date} 平仓 | "
                    f"百分位={current_pct:.1%} > {self.exit_pct:.0%}（信号反转）"
                )
                return [Order(
                    date=current_date, action="sell",
                    shares=context.position.shares,
                    reason=f"百分位({current_pct:.1%}) > {self.exit_pct:.0%}",
                )]
            # 止盈
            if profit_atr >= self.atr_mult_tp:
                logger.info(
                    f"[策略B] {current_date} 止盈 | "
                    f"浮盈={profit_atr:.1f}×ATR >= {self.atr_mult_tp}×ATR"
                )
                return [Order(
                    date=current_date, action="sell",
                    shares=context.position.shares,
                    reason=f"浮盈({profit_atr:.1f}×ATR) >= {self.atr_mult_tp}×ATR",
                )]
            return []

        # ── 开仓条件 ──
        if current_pct > self.entry_pct:
            return []

        if idx < context.cooldown_until:
            return []

        # 低波条件
        vol = _rolling_volatility(df, self.vol_window).iloc[-1]
        if pd.isna(vol) or vol <= 0:
            return []

        vol_history = _rolling_volatility(df, self.vol_window).iloc[
            -min(self.vol_lookback, len(df)):
        ].dropna()
        if len(vol_history) < 20:
            return []

        vol_pct = (vol_history < vol).mean()
        if vol_pct > self.vol_percentile:
            logger.debug(
                f"[策略B] {current_date} 波动率分位={vol_pct:.1%} "
                f"> {self.vol_percentile:.0%}，不满足低波条件"
            )
            return []

        # ── 仓位计算 ──
        median_vol = vol_history.median()
        if pd.isna(median_vol) or median_vol <= 0:
            effective_risk = self.base_risk_budget
        else:
            effective_risk = self.base_risk_budget * (median_vol / vol)
        effective_risk = np.clip(effective_risk, self.risk_min, self.risk_max)

        atr = compute_atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return []

        risk_amount = context.equity * effective_risk
        stop_distance = 2 * atr
        shares = round_lot_shares(risk_amount / stop_distance, context.market)
        stop_loss = close - stop_distance

        logger.info(
            f"[策略B] {current_date} 开仓 | "
            f"百分位={current_pct:.1%} < {self.entry_pct:.0%} | "
            f"波动率分位={vol_pct:.1%} | "
            f"风险预算={effective_risk:.1%} | 股数={shares}"
        )
        return [Order(
            date=current_date, action="buy", shares=shares,
            stop_loss=stop_loss,
            reason=f"超卖+低波: 百分位={current_pct:.1%}, vol_pct={vol_pct:.1%}",
        )]
