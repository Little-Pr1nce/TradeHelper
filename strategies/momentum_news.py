"""
策略C：动量突破+新闻共振确认策略

三重确认开仓模式，去掉金字塔加仓。
无新闻数据时 FinBERT 条件自动跳过（align_finbert_scores 默认填充 0.0）。

规则：
  - 开仓：Final_Score 处于 80% 分位以上 AND（FinBERT > 0.3 或 FinBERT=0 即无数据）AND 价格突破 5 日高点
  - 平仓：Final_Score 跌破 50% 分位 或 移动止盈
  - 冷却期：2 根 K 线
  - 仓位：1% 风险预算
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    BaseExecutionStrategy, Order, StrategyContext,
    compute_atr, compute_percentile_score,
)

logger = logging.getLogger(__name__)


class MomentumNewsStrategy(BaseExecutionStrategy):
    """动量突破+新闻共振确认。"""

    suitable_regimes = []  # 全行情通用

    def tunable_params(self) -> list[dict]:
        return [
            {"name": "entry_pct", "default": self.entry_pct, "values": [0.70, 0.75, 0.80, 0.85]},
            {"name": "finbert_min", "default": self.finbert_min, "values": [0.20, 0.30, 0.40]},
        ]


    def __init__(self, entry_pct: float = 0.80, exit_pct: float = 0.50,
                 finbert_min: float = 0.3, breakout_window: int = 5,
                 cooldown_bars: int = 2, atr_period: int = 14,
                 trailing_atr_mult: float = 2.0,
                 risk_budget: float = 0.01, lookback: int = 252):
        self.entry_pct = entry_pct
        self.exit_pct = exit_pct
        self.finbert_min = finbert_min
        self.breakout_window = breakout_window
        self.cooldown_bars = cooldown_bars
        self.atr_period = atr_period
        self.trailing_atr_mult = trailing_atr_mult
        self.risk_budget = risk_budget
        self.lookback = lookback

    @property
    def name(self) -> str:
        return (f"动量突破+新闻共振 (score>{self.entry_pct:.0%}分位, "
                f"FinBERT>{self.finbert_min}, breakout{self.breakout_window}d)")

    @property
    def description(self) -> str:
        return (
            f"三重确认开仓：Final_Score 处于 {self.entry_pct:.0%} 分位以上、"
            f"（FinBERT > {self.finbert_min} 或新闻不可用）、价格突破 {self.breakout_window} 日高点。"
            f"简单买卖模式，仓位 {self.risk_budget:.1%}。"
        )

    def _check_entry(self, df: pd.DataFrame, current_pct: float) -> tuple[bool, str]:
        close = float(df["close"].iloc[-1])

        if current_pct < self.entry_pct:
            return False, f"百分位({current_pct:.1%}) < {self.entry_pct:.0%}"

        # 新闻面：0 表示无数据，自动跳过
        if "FinBERT_Score" in df.columns:
            finbert = float(df["FinBERT_Score"].iloc[-1])
            if finbert != 0.0 and pd.notna(finbert) and finbert <= self.finbert_min:
                return False, f"FinBERT({finbert:.3f}) ≤ {self.finbert_min}"

        lookback = min(self.breakout_window, len(df) - 1)
        if lookback > 0:
            highest = df["close"].iloc[-(lookback + 1):-1].max()
            if close <= highest:
                return False, f"close({close:.2f}) ≤ {self.breakout_window}d high({highest:.2f})"

        return True, ""

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        if df.empty or "Final_Score" not in df.columns:
            return []

        idx = len(df) - 1
        current_date = str(df.iloc[-1].get("date", ""))[:10]
        close = float(df["close"].iloc[-1])
        atr = compute_atr(df, self.atr_period).iloc[-1]
        has_position = context.position.shares > 0

        pct_series = compute_percentile_score(df, window=self.lookback)
        current_pct = pct_series.iloc[-1]
        if pd.isna(current_pct):
            return []

        # 平仓
        if has_position:
            trailing_stop = 0.0
            if pd.notna(atr) and atr > 0:
                highest_since = max(context.position.highest_close, close)
                trailing_stop = highest_since - self.trailing_atr_mult * atr

            score_exit = current_pct < self.exit_pct
            trailing_exit = close < trailing_stop if trailing_stop > 0 else False

            if score_exit or trailing_exit:
                reason = (
                    f"百分位({current_pct:.1%}) < {self.exit_pct:.0%}"
                    if score_exit
                    else f"移动止盈: close({close:.2f}) < trail({trailing_stop:.2f})"
                )
                logger.info(f"[策略C] {current_date} 平仓 | {reason}")
                return [Order(date=current_date, action="sell",
                             shares=context.position.shares, reason=reason)]
            return []

        # 开仓
        can_enter, reject_reason = self._check_entry(df, current_pct)
        if not can_enter:
            return []
        if idx < context.cooldown_until:
            return []
        if pd.isna(atr) or atr <= 0:
            return []

        stop_distance = self.trailing_atr_mult * atr
        risk_amount = context.equity * self.risk_budget
        shares = max(int(risk_amount / max(stop_distance, 1e-6) / 100) * 100, 100)
        stop_loss = close - stop_distance

        logger.info(
            f"[策略C] {current_date} 三重确认开仓 | 百分位={current_pct:.1%} | 股数={shares}"
        )
        return [Order(
            date=current_date, action="buy", shares=shares,
            stop_loss=stop_loss,
            reason=f"三重确认: 百分位={current_pct:.1%}",
        )]
