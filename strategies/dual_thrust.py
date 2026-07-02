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
    DecisionFirstStrategy, StrategyContext,
    compute_atr, compute_percentile_score, round_lot_shares,
)

logger = logging.getLogger(__name__)


class DualThrustStrategy(DecisionFirstStrategy):
    """Dual Thrust 日线适配版。

    参数说明：
      entry_pct=0.70  — 百分位 >= 70%
      exit_pct=0.40   — 跌破 40% 百分位平仓
      lookback_n=20   — N 日窗口
      k1=0.7, k2=0.7 — 突破系数
      cooldown_bars=3
    """


    suitable_regimes = ["trending_volatile"]
    take_profit_mode = "conditional"
    take_profit_rule = "收盘跌破Dual Thrust下轨或Score跌破退出阈值时平仓"
    strategy_family = "channel_breakout"

    def tunable_params(self) -> list[dict]:
        return [
            {"name": "entry_pct", "default": self.entry_pct, "values": [0.60, 0.65, 0.70, 0.75]},
            {"name": "k1", "default": self.k1, "values": [0.15, 0.20, 0.25]},
        ]

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

    def diagnose_no_signal(self, df, context) -> list[str]:
        if df.empty or "Final_Score" not in df.columns:
            return ["缺少 Final_Score，无法检查 Dual Thrust"]
        pct = compute_percentile_score(df, window=self.score_lookback).iloc[-1]
        if pd.isna(pct):
            return ["百分位样本不足，至少需要60根有效K线"]
        close = float(df["close"].iloc[-1])
        previous_open = float(df["open"].iloc[-2]) if len(df) >= 2 else float(df["open"].iloc[-1])
        range_value = self._calc_range(df)
        if range_value <= 0:
            return ["Dual Thrust 历史振幅无效"]
        upper = previous_open + self.k1 * range_value
        lower = previous_open - self.k2 * range_value
        if context.position.shares > 0:
            return [
                f"价格{close:.2f}尚未跌破DT下轨{lower:.2f}",
                f"Score百分位{pct:.0%}尚未跌破{self.exit_pct:.0%}",
            ]
        missing = []
        if close <= upper:
            missing.append(f"价格{close:.2f}需突破DT上轨{upper:.2f}")
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
        close = float(df["close"].iloc[-1])

        pct_series = compute_percentile_score(df, window=self.score_lookback)
        current_pct = pct_series.iloc[-1]
        if pd.isna(current_pct):
            return self._no_signal_decision(df, context)

        # 用上一根 bar 的 open 作为基准（T 日收盘时可拿到 T 日 open，但同时可用 T-1 open）
        if idx >= 1:
            prev_open = float(df["open"].iloc[idx - 1])
        else:
            prev_open = float(df["open"].iloc[idx])

        range_val = self._calc_range(df)
        if range_val <= 0:
            return self._no_signal_decision(df, context)
        upper_band = prev_open + self.k1 * range_val
        lower_band = prev_open - self.k2 * range_val

        # ── 平仓 ──
        if has_position:
            if close < lower_band:
                logger.info(f"[策略E] {current_date} 平仓 | close({close:.2f}) < DT下轨({lower_band:.2f})")
                return self._execution_decision(
                    df, context, action="sell", shares=context.position.shares,
                    reason=f"跌破DT下轨: {close:.2f} < {lower_band:.2f}",
                )
            if current_pct < self.exit_pct:
                logger.info(f"[策略E] 平仓 | 百分位={current_pct:.1%} < {self.exit_pct:.0%}")
                return self._execution_decision(
                    df, context, action="sell", shares=context.position.shares,
                    reason=f"百分位({current_pct:.1%}) < {self.exit_pct:.0%}",
                )
            return self._no_signal_decision(df, context)

        # ── 开仓 ──
        if close <= upper_band:
            return self._no_signal_decision(df, context)
        if current_pct < self.entry_pct:
            return self._no_signal_decision(df, context)
        if idx < context.cooldown_until:
            return self._no_signal_decision(df, context)

        atr = compute_atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return self._no_signal_decision(df, context)

        stop_distance = 2 * atr
        risk_amount = context.equity * self.risk_budget
        shares = round_lot_shares(risk_amount / max(stop_distance, 1e-6), context.market)
        stop_loss = close - stop_distance

        logger.info(
            f"[策略E] {current_date} DT突破开仓 | close={close:.2f} > 上轨={upper_band:.2f}"
        )
        return self._execution_decision(
            df, context, action="buy", shares=shares,
            stop_loss=stop_loss,
            reason=f"DT突破: {close:.2f}>{upper_band:.2f}, 百分位={current_pct:.1%}",
        )
