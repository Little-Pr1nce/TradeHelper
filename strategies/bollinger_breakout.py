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
    DecisionFirstStrategy, StrategyContext,
    compute_atr, compute_percentile_score, round_lot_shares,
)

logger = logging.getLogger(__name__)


class BollingerBreakoutStrategy(DecisionFirstStrategy):
    """布林带突破策略。

    参数说明：
      entry_pct=0.70 — 百分位需 >= 70%
      exit_pct=0.40  — 跌破 40% 百分位平仓
      atr_mult_stop=1.5 — 止损 ATR 倍数
      cooldown_bars=3
    """


    suitable_regimes = ["ranging"]

    def tunable_params(self) -> list[dict]:
        return [
            {"name": "entry_pct", "default": self.entry_pct, "values": [0.60, 0.65, 0.70, 0.75]},
            {"name": "atr_mult_stop", "default": self.atr_mult_stop, "values": [1.0, 1.5, 2.0]},
        ]

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

    def diagnose_no_signal(
        self, df: pd.DataFrame, context: StrategyContext
    ) -> list[str]:
        if df.empty or "Final_Score" not in df.columns:
            return ["缺少 Final_Score，无法检查布林突破"]
        required = {"close", "bb_upper", "bb_mid"}
        if not required.issubset(df.columns):
            return ["缺少布林带指标，无法检查突破条件"]
        row = df.iloc[-1]
        close = float(row["close"])
        current_pct = compute_percentile_score(df, window=self.lookback).iloc[-1]
        if pd.isna(current_pct):
            return ["百分位样本不足，至少需要60根有效K线"]

        missing = []
        if context.position.shares > 0:
            bb_mid = float(row["bb_mid"])
            if close >= bb_mid:
                missing.append(f"价格{close:.2f}尚未跌破布林中轨{bb_mid:.2f}")
            if current_pct >= self.exit_pct:
                missing.append(
                    f"Score百分位{current_pct:.0%}尚未跌破退出阈值{self.exit_pct:.0%}"
                )
        else:
            bb_upper = float(row["bb_upper"])
            if close <= bb_upper:
                missing.append(f"价格{close:.2f}需突破布林上轨{bb_upper:.2f}")
            if current_pct < self.entry_pct:
                missing.append(
                    f"Score百分位{current_pct:.0%}需达到{self.entry_pct:.0%}"
                )
            if len(df) - 1 < context.cooldown_until:
                missing.append("策略仍处于冷却期")
        return missing or ["布林突破条件尚未形成"]

    def _evaluate_decision(self, df: pd.DataFrame, context: StrategyContext):
        if df.empty or "Final_Score" not in df.columns:
            return self._no_signal_decision(df, context)

        idx = len(df) - 1
        current_date = str(df.iloc[-1].get("date", ""))[:10]
        has_position = context.position.shares > 0
        close = float(df["close"].iloc[-1])

        pct_series = compute_percentile_score(df, window=self.lookback)
        current_pct = pct_series.iloc[-1]
        if pd.isna(current_pct):
            return self._no_signal_decision(df, context)

        if "bb_upper" not in df.columns or "bb_mid" not in df.columns:
            return self._no_signal_decision(df, context)
        bb_upper = float(df["bb_upper"].iloc[-1])
        bb_mid = float(df["bb_mid"].iloc[-1])
        bb_lower = float(df["bb_lower"].iloc[-1]) if "bb_lower" in df.columns else bb_mid * 0.95

        # ── 平仓 ──
        if has_position:
            if close < bb_mid:
                logger.info(f"[策略D] {current_date} 平仓 | close({close:.2f}) < BB中轨({bb_mid:.2f})")
                return self._execution_decision(
                    df, context, action="sell", shares=context.position.shares,
                    reason=f"跌破布林中轨: {close:.2f} < {bb_mid:.2f}",
                )
            if current_pct < self.exit_pct:
                logger.info(f"[策略D] 平仓 | 百分位={current_pct:.1%} < {self.exit_pct:.0%}")
                return self._execution_decision(
                    df, context, action="sell", shares=context.position.shares,
                    reason=f"百分位({current_pct:.1%}) < {self.exit_pct:.0%}",
                )
            return self._no_signal_decision(df, context)

        # ── 开仓 ──
        if close <= bb_upper:
            return self._no_signal_decision(df, context)
        if current_pct < self.entry_pct:
            return self._no_signal_decision(df, context)
        if idx < context.cooldown_until:
            return self._no_signal_decision(df, context)

        atr = compute_atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return self._no_signal_decision(df, context)

        stop_distance = self.atr_mult_stop * atr
        risk_amount = context.equity * self.risk_budget
        shares = round_lot_shares(risk_amount / max(stop_distance, 1e-6), context.market)
        stop_loss = bb_mid - stop_distance

        logger.info(
            f"[策略D] {current_date} 布林突破开仓 | close={close:.2f} > BB上轨={bb_upper:.2f}"
        )
        return self._execution_decision(
            df, context, action="buy", shares=shares,
            stop_loss=stop_loss,
            reason=f"布林突破: {close:.2f}>{bb_upper:.2f}, 百分位={current_pct:.1%}",
        )
