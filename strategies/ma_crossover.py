"""
策略G：均线交叉 + Score 确认 (MA Crossover with Score Confirmation)

专门覆盖「低波动慢涨/阴跌」行情（trending_steady），这类行情
趋势型策略的百分位阈值太高（80%），永远不触发。

规则：
  - 开仓：MA5 > MA20 且 Final_Score > 50%分位 且 price > MA60
  - 平仓：MA5 < MA20 或 Final_Score < 30%分位
  - 止损：开仓价 - 1×ATR(14)
  - 仓位：等权重 100 股
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    DecisionFirstStrategy, StrategyContext,
    compute_atr, compute_percentile_score,
)

logger = logging.getLogger(__name__)


class MACrossoverStrategy(DecisionFirstStrategy):
    """均线交叉 + Score 确认策略 — 慢涨/阴跌行情专用。

    参数说明：
      entry_pct=0.50  — Final_Score 需高于 50% 历史分位（中位线以上即可）
      exit_pct=0.30   — Final_Score 跌破 30% 分位退出
    """

    suitable_regimes = ["trending_steady"]
    take_profit_mode = "conditional"
    take_profit_rule = "MA5下穿MA20或Score跌破退出阈值时平仓"
    strategy_family = "trend_following"

    def tunable_params(self) -> list[dict]:
        return [
            {"name": "entry_pct", "default": self.entry_pct, "values": [0.40, 0.45, 0.50, 0.55]},
            {"name": "exit_pct", "default": self.exit_pct, "values": [0.20, 0.30, 0.40]},
        ]


    def __init__(self, entry_pct: float = 0.50, exit_pct: float = 0.30):
        self.entry_pct = entry_pct
        self.exit_pct = exit_pct

    @property
    def name(self) -> str:
        return "均线交叉+Score确认"

    @property
    def description(self) -> str:
        return (
            f"慢涨专用：MA5>MA20 + Score>{self.entry_pct:.0%}分位 + 价>MA60 开仓；"
            f"MA5<MA20 或 Score<{self.exit_pct:.0%}分位 平仓。"
        )

    def diagnose_no_signal(self, df, context) -> list[str]:
        if len(df) < 60 or "Final_Score" not in df.columns:
            return ["至少需要60根K线和 Final_Score"]
        close = df["close"].astype(float)
        ma5 = float(close.rolling(5).mean().iloc[-1])
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = float(close.rolling(60).mean().iloc[-1])
        price = float(close.iloc[-1])
        pct = compute_percentile_score(df, window=252).iloc[-1]
        if pd.isna(pct):
            return ["百分位样本不足，至少需要60根有效K线"]
        if context.position.shares > 0:
            return [
                f"MA5({ma5:.2f})尚未下穿MA20({ma20:.2f})",
                f"Score百分位{pct:.0%}尚未跌破{self.exit_pct:.0%}",
            ]
        missing = []
        if ma5 <= ma20:
            missing.append(f"MA5({ma5:.2f})需上穿MA20({ma20:.2f})")
        if pct < self.entry_pct:
            missing.append(f"Score百分位{pct:.0%}需达到{self.entry_pct:.0%}")
        if price <= ma60:
            missing.append(f"价格{price:.2f}需站上MA60({ma60:.2f})")
        return missing or ["均线交叉条件尚未形成"]

    def _evaluate_decision(self, df: pd.DataFrame, context: StrategyContext):
        if len(df) < 60:
            return self._no_signal_decision(df, context)

        row = df.iloc[-1]
        score = row.get("Final_Score", 0.0)
        if pd.isna(score):
            return self._no_signal_decision(df, context)

        # 计算均线
        close = df["close"].astype(float)
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        latest_ma5 = float(ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else 0
        latest_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else 0
        latest_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else 0
        latest_close = float(close.iloc[-1])
        prev_ma5 = float(ma5.iloc[-2]) if pd.notna(ma5.iloc[-2]) else 0
        prev_ma20 = float(ma20.iloc[-2]) if pd.notna(ma20.iloc[-2]) else 0

        # —— 开仓信号 ——
        if context.position.shares == 0 and latest_ma5 > 0 and latest_ma20 > 0:
            score_pct = compute_percentile_score(df, window=252)
            latest_pct = float(score_pct.iloc[-1]) if pd.notna(score_pct.iloc[-1]) else 0

            ma_cross_up = latest_ma5 > latest_ma20 and prev_ma5 <= prev_ma20  # 刚金叉
            ma_already_up = latest_ma5 > latest_ma20  # 已经多头
            score_ok = latest_pct >= self.entry_pct
            above_ma60 = latest_close > latest_ma60 > 0

            if (ma_cross_up or ma_already_up) and score_ok and above_ma60:
                atr = compute_atr(df, 14)
                latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else latest_close * 0.02
                stop_loss = latest_close - latest_atr

                decision = self._execution_decision(
                    df, context, action="buy",
                    shares=100,
                    stop_loss=round(stop_loss, 2),
                    reason=f"MA5({latest_ma5:.1f})>MA20({latest_ma20:.1f}) "
                           f"Score_pct={latest_pct:.1%} >{self.entry_pct:.0%} "
                           f"close({latest_close:.1f})>MA60({latest_ma60:.1f})",
                )
                logger.debug(f"MA Crossover BUY: {decision.reason}")
                return decision

        # —— 平仓信号 ——
        elif context.position.shares > 0:
            score_pct = compute_percentile_score(df, window=252)
            latest_pct = float(score_pct.iloc[-1]) if pd.notna(score_pct.iloc[-1]) else 0.5

            ma_cross_down = latest_ma5 < latest_ma20 and prev_ma5 >= prev_ma20
            score_weak = latest_pct < self.exit_pct

            if ma_cross_down or score_weak:
                decision = self._execution_decision(
                    df, context, action="sell",
                    shares=context.position.shares,
                    reason=f"MA5({latest_ma5:.1f})<MA20({latest_ma20:.1f})" if ma_cross_down
                    else f"Score_pct={latest_pct:.1%} <{self.exit_pct:.0%}",
                )
                logger.debug(f"MA Crossover SELL: {decision.reason}")
                return decision

        return self._no_signal_decision(df, context)
