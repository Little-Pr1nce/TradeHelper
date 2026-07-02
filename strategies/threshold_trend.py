"""
策略A：百分位趋势跟踪策略 (Percentile Trend Strategy)

核心创新：用 Final_Score 的滚动百分位替代固定阈值。
滚动 252 日百分位映射后，无论牛熊市，entry=0.80 始终代表"处于近一年最强势的 20%"。

原版因 Final_Score 在单边行情中严重偏离 [-1,+1] 区间（可能 80% 的时间 < 0.3），
导致固定阈值 0.6 永远无法触发。百分位模式从根本上解决了这个问题。

规则：
  - 开仓：Final_Score 处于近 252 日 80% 百分位以上（强多头信号）
  - 平仓：Final_Score 跌破 50% 百分位（趋势转弱）
  - 冷却期：3 根 K 线
  - 仓位：风险预算 2% 净值 / (2 × ATR)，向下取整至 100 股
  - 硬止损：-8%（由 Broker 层统一执行）
  - 时间止损：10 个交易日（由 Broker 层统一执行）
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    DecisionFirstStrategy, StrategyContext,
    compute_atr, compute_percentile_score, round_lot_shares,
)

logger = logging.getLogger(__name__)


class ThresholdTrendStrategy(DecisionFirstStrategy):
    """百分位趋势跟踪（基准策略）。

    参数说明：
      entry_pct=0.80  — 需处于近 252 日 80% 百分位以上才开仓
      exit_pct=0.50   — 跌破 50% 百分位即平仓
      cooldown_bars=3  — 冷却期
      lookback=252     — 百分位回溯窗口（交易日）

    """

    suitable_regimes = ["trending_volatile", "trending_steady"]
    take_profit_mode = "conditional"
    take_profit_rule = "Final_Score滚动百分位跌破退出阈值时平仓"
    strategy_family = "trend_following"

    def tunable_params(self) -> list[dict]:
        return [
            {"name": "entry_pct", "default": self.entry_pct, "values": [0.65, 0.75, 0.80, 0.85]},
            {"name": "exit_pct", "default": self.exit_pct, "values": [0.40, 0.50, 0.60]},
        ]


    def __init__(self, entry_pct: float = 0.80, exit_pct: float = 0.50,
                 cooldown_bars: int = 3, atr_period: int = 14,
                 risk_budget: float = 0.60, lookback: int = 252):
        self.entry_pct = entry_pct
        self.exit_pct = exit_pct
        self.cooldown_bars = cooldown_bars
        self.atr_period = atr_period
        self.risk_budget = risk_budget
        self.lookback = lookback

    @property
    def name(self) -> str:
        return f"百分位趋势跟踪 (entry>{self.entry_pct:.0%}分位, exit<{self.exit_pct:.0%}分位)"

    @property
    def description(self) -> str:
        return (
            f"滚动 {self.lookback} 日百分位模式："
            f"Final_Score 处于 {self.entry_pct:.0%} 分位以上开仓，"
            f"跌破 {self.exit_pct:.0%} 分位平仓。"
            f"冷却期 {self.cooldown_bars} 根 K 线，风险预算 {self.risk_budget:.0%}（高仓位）。"
        )

    def diagnose_no_signal(self, df, context) -> list[str]:
        if df.empty or "Final_Score" not in df.columns:
            return ["缺少 Final_Score，无法检查百分位趋势"]
        pct = compute_percentile_score(df, window=self.lookback).iloc[-1]
        if pd.isna(pct):
            return ["百分位样本不足，至少需要60根有效K线"]
        if context.position.shares > 0:
            return [f"Score百分位{pct:.0%}尚未跌破退出阈值{self.exit_pct:.0%}"]
        missing = []
        if pct < self.entry_pct:
            missing.append(f"Score百分位{pct:.0%}需达到{self.entry_pct:.0%}")
        if len(df) - 1 < context.cooldown_until:
            missing.append("策略仍处于冷却期")
        return missing or ["ATR或可下单股数无效"]

    def _evaluate_decision(self, df: pd.DataFrame, context: StrategyContext):
        if df.empty or "Final_Score" not in df.columns:
            return self._no_signal_decision(df, context)

        idx = len(df) - 1
        current_date = str(df.iloc[-1].get("date", ""))[:10]
        has_position = context.position.shares > 0

        # 计算百分位
        pct_series = compute_percentile_score(df, window=self.lookback)
        current_pct = pct_series.iloc[-1]
        if pd.isna(current_pct):
            return self._no_signal_decision(df, context)

        # ── 平仓条件 ──
        if has_position:
            if current_pct < self.exit_pct:
                logger.info(
                    f"[策略A] {current_date} 平仓 | "
                    f"百分位={current_pct:.1%} < {self.exit_pct:.0%}"
                )
                return self._execution_decision(
                    df, context, action="sell",
                    shares=context.position.shares,
                    reason=f"百分位({current_pct:.1%}) < exit({self.exit_pct:.0%})",
                )
            return self._no_signal_decision(df, context)

        # ── 开仓条件 ──
        if current_pct >= self.entry_pct:
            if idx < context.cooldown_until:
                return self._no_signal_decision(df, context)

            atr = compute_atr(df, self.atr_period).iloc[-1]
            if pd.isna(atr) or atr <= 0:
                return self._no_signal_decision(df, context)

            close = float(df["close"].iloc[-1])
            stop_distance = 2 * atr
            risk_amount = context.equity * self.risk_budget
            shares = round_lot_shares(risk_amount / stop_distance, context.market)
            stop_loss = close - stop_distance

            logger.info(
                f"[策略A] {current_date} 开仓 | "
                f"百分位={current_pct:.1%} >= {self.entry_pct:.0%} | "
                f"股数={shares}, 止损价={stop_loss:.2f}"
            )
            return self._execution_decision(
                df, context, action="buy", shares=shares,
                stop_loss=stop_loss,
                reason=f"百分位({current_pct:.1%}) >= entry({self.entry_pct:.0%})",
                time_stop_days=120,    # 趋势策略不设短时间止损
                hard_stop_pct=0.20,    # 放宽硬止损至 20%
            )

        return self._no_signal_decision(df, context)
