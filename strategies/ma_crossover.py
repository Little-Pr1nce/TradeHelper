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
    BaseExecutionStrategy, Order, Position, StrategyContext,
    compute_atr, compute_percentile_score,
)

logger = logging.getLogger(__name__)


class MACrossoverStrategy(BaseExecutionStrategy):
    """均线交叉 + Score 确认策略 — 慢涨/阴跌行情专用。

    参数说明：
      entry_pct=0.50  — Final_Score 需高于 50% 历史分位（中位线以上即可）
      exit_pct=0.30   — Final_Score 跌破 30% 分位退出
    """

    suitable_regimes = ["trending_steady"]

    def tunable_params(self) -> list[dict]:
        return [{"name": "entry_pct", "default": self.entry_pct, "values": [0.4, 0.45, 0.5, 0.55, 0.6]}]


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

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        orders = []
        if len(df) < 60:
            return orders

        row = df.iloc[-1]
        score = row.get("Final_Score", 0.0)
        if pd.isna(score):
            return orders

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

                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="buy",
                    shares=100,
                    stop_loss=round(stop_loss, 2),
                    reason=f"MA5({latest_ma5:.1f})>MA20({latest_ma20:.1f}) "
                           f"Score_pct={latest_pct:.1%} >{self.entry_pct:.0%} "
                           f"close({latest_close:.1f})>MA60({latest_ma60:.1f})",
                ))
                logger.debug(f"MA Crossover BUY: {orders[-1].reason}")

        # —— 平仓信号 ——
        elif context.position.shares > 0:
            score_pct = compute_percentile_score(df, window=252)
            latest_pct = float(score_pct.iloc[-1]) if pd.notna(score_pct.iloc[-1]) else 0.5

            ma_cross_down = latest_ma5 < latest_ma20 and prev_ma5 >= prev_ma20
            score_weak = latest_pct < self.exit_pct

            if ma_cross_down or score_weak:
                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="sell",
                    shares=context.position.shares,
                    reason=f"MA5({latest_ma5:.1f})<MA20({latest_ma20:.1f})" if ma_cross_down
                    else f"Score_pct={latest_pct:.1%} <{self.exit_pct:.0%}",
                ))
                logger.debug(f"MA Crossover SELL: {orders[-1].reason}")

        return orders
