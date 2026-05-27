"""
策略B：波动率自适应均值回归策略 (Volatility-Adaptive Mean Reversion)

核心思想：在低波动环境中捕捉超跌反弹机会。
低波动意味着市场情绪稳定，超跌更可能是短期恐慌而非基本面恶化。

规则：
  - 开仓：Final_Score < -0.5 AND 20 日波动率处于历史后 30% 分位
  - 平仓：Final_Score > 0.2 或持仓浮盈达到 3 × ATR(14)
  - 冷却期：5 根 K 线（均值回归需要更长的冷却期以避免接飞刀）
  - 仓位：反波动率加权 — 波动越低仓位越高
    risk_budget = 2% × (历史中位数波动率 / 当前波动率)，限制在 [1%, 4%]

适用场景：震荡市中捕捉超跌反弹，与策略 A（趋势跟踪）形成互补。
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    BaseExecutionStrategy, Order, Position, StrategyContext, compute_atr,
)

logger = logging.getLogger(__name__)


def _rolling_volatility(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """计算滚动年化波动率（日收益率标准差 × √252）。"""
    returns = df["close"].pct_change()
    return returns.rolling(window=period).std() * np.sqrt(252)


class MeanReversionStrategy(BaseExecutionStrategy):
    """
    波动率自适应均值回归策略。

    参数说明：
      entry_threshold=-0.5    — 超卖阈值：Final_Score 必须低于此值
      vol_percentile=0.30     — 低波分位：当前波动率需处于历史后 30%
      cooldown_bars=5         — 冷却期：均值回归需要更长的冷却
      atr_mult_tp=3.0         — 止盈：浮盈达到 3 倍 ATR 即平仓
      base_risk_budget=0.02   — 基础风险预算 2%
      risk_min=0.01, risk_max=0.04  — 反波动率加权的上下限
    """

    def __init__(self, entry_threshold: float = -0.5, exit_score: float = 0.2,
                 vol_window: int = 20, vol_percentile: float = 0.30,
                 cooldown_bars: int = 5, atr_period: int = 14, atr_mult_tp: float = 3.0,
                 base_risk_budget: float = 0.02,
                 risk_min: float = 0.01, risk_max: float = 0.04,
                 vol_lookback: int = 252):
        self.entry_threshold = entry_threshold
        self.exit_score = exit_score
        self.vol_window = vol_window
        self.vol_percentile = vol_percentile       # 后 30% 分位 = 低波环境
        self.cooldown_bars = cooldown_bars
        self.atr_period = atr_period
        self.atr_mult_tp = atr_mult_tp
        self.base_risk_budget = base_risk_budget
        self.risk_min = risk_min
        self.risk_max = risk_max
        self.vol_lookback = vol_lookback

    @property
    def name(self) -> str:
        return f"波动率自适应均值回归 (entry<{self.entry_threshold}, vol<p{int(self.vol_percentile*100)})"

    @property
    def description(self) -> str:
        return (
            f"Final_Score < {self.entry_threshold} 且当前波动率处于历史后 "
            f"{int(self.vol_percentile*100)}% 分位时开仓。反波动率加权，冷却期 {self.cooldown_bars} 根。"
        )

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        """
        决策逻辑：
          ① 持仓中 → 检查平仓条件（Score 反转 或 浮盈达标）
          ② 空仓中 → 检查开仓条件（超卖 + 低波 + 不在冷却期）
          ③ 反波动率加权计算仓位
        """
        if df.empty or "Final_Score" not in df.columns:
            return []

        idx = len(df) - 1
        current_date = str(df.iloc[-1].get("date", ""))[:10]
        final_score = float(df["Final_Score"].iloc[-1])
        has_position = context.position.shares > 0
        close = float(df["close"].iloc[-1])

        # ── 平仓条件 ──
        if has_position:
            atr = compute_atr(df, self.atr_period).iloc[-1]
            entry_price = context.position.entry_price
            profit_atr = (close - entry_price) / atr if (pd.notna(atr) and atr > 0) else 0

            # 条件 1：Final_Score 反转（> 0.2 表示超卖修复完毕）
            # 条件 2：浮盈达到 3 倍 ATR（趋势可能延续，止盈锁定利润）
            if final_score > self.exit_score:
                logger.info(
                    f"[策略B] {current_date} 平仓 | "
                    f"Final_Score={final_score:.3f} > {self.exit_score}（信号反转）"
                )
                return [Order(
                    date=current_date, action="sell",
                    shares=context.position.shares,
                    reason=f"Final_Score({final_score:.3f}) > {self.exit_score}",
                )]
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
        # 条件 1：超卖信号
        if final_score >= self.entry_threshold:
            return []

        # 条件 2：冷却期检查
        if idx < context.cooldown_until:
            return []

        # 条件 3：低波动环境（当前波动率处于历史后 30% 分位）
        vol = _rolling_volatility(df, self.vol_window).iloc[-1]
        if pd.isna(vol) or vol <= 0:
            return []

        vol_history = _rolling_volatility(df, self.vol_window).iloc[
            -min(self.vol_lookback, len(df)):
        ].dropna()
        if len(vol_history) < 20:
            return []

        current_percentile = (vol_history < vol).mean()
        if current_percentile > self.vol_percentile:
            logger.debug(
                f"[策略B] {current_date} 波动率分位={current_percentile:.1%} "
                f"> {self.vol_percentile:.0%}，不满足低波条件"
            )
            return []

        # ── 仓位计算：反波动率加权 ──
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
        shares = max(int(risk_amount / stop_distance / 100) * 100, 100)
        stop_loss = close - stop_distance

        logger.info(
            f"[策略B] {current_date} 开仓 | "
            f"Final_Score={final_score:.3f} < {self.entry_threshold} | "
            f"波动率分位={current_percentile:.1%} | "
            f"风险预算={effective_risk:.1%} | 股数={shares}"
        )
        return [Order(
            date=current_date, action="buy", shares=shares,
            stop_loss=stop_loss,
            reason=f"超卖+低波: Score={final_score:.3f}, vol_pct={current_percentile:.1%}",
        )]
