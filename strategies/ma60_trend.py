"""
策略 H：MA60 中长期趋势跟踪 (MA60 Medium-Term Trend Following)

专为中长期投资者设计，以 MA60（季度线）为核心趋势锚，
配合 MA20/MA60 金叉确认中期趋势方向。

与现有策略的区别：
  - 不做 10 天强制时间止损（最大持仓 120 天 ≈ 6 个月）
  - 使用 3×ATR 移动止盈替代时间止损，让利润充分奔跑
  - 开仓门槛更高（MA20>MA60 + 价>MA60 + Score 为正），降低假突破

规则：
  - 开仓：价 > MA60 且 MA20 > MA60 且 Final_Score > 0 且 Score 百分位 > 30%
  - 平仓：价 < MA60 或 移动止盈（最高收盘 - 3×ATR）或 MA20 < MA60
  - 仓位：风险预算 2% 净值 = shares = 2% equity / (2×ATR)
  - 止损：开仓价 - 2×ATR（硬止损）
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    DecisionFirstStrategy, StrategyContext,
    compute_atr, compute_percentile_score, round_lot_shares,
)

logger = logging.getLogger(__name__)


class MA60TrendStrategy(DecisionFirstStrategy):
    """MA60 中长期趋势跟踪策略。

    适用行情：趋势市（trending / trending_steady / trending_volatile）
    不适合：震荡市（ranging）

    参数：
      risk_budget=0.02   — 单笔风险预算占净值比
      atr_stop_mult=2.0  — 硬止损 = 开仓价 - N×ATR
      atr_trail_mult=3.0 — 移动止盈 = 最高收盘 - N×ATR
      max_hold_days=120  — 最大持仓天数（≈6 个自然月）
    """

    # 只适合趋势行情，震荡市不触发
    suitable_regimes = ["trending", "trending_steady", "trending_volatile"]

    def __init__(
        self,
        risk_budget: float = 0.60,
        atr_stop_mult: float = 2.0,
        atr_trail_mult: float = 3.0,
        max_hold_days: int = 250,
    ):
        self.risk_budget = risk_budget
        self.atr_stop_mult = atr_stop_mult
        self.atr_trail_mult = atr_trail_mult
        self.max_hold_days = max_hold_days

    @property
    def name(self) -> str:
        return "MA60中长期趋势跟踪"

    @property
    def description(self) -> str:
        return (
            f"中长期：价>MA60 + MA20>MA60 + Score>0 开仓；"
            f"价<MA60 或 移动止盈({self.atr_trail_mult}×ATR) 平仓；"
            f"最长持仓{self.max_hold_days}天"
        )

    def tunable_params(self) -> list[dict]:
        return [
            {"name": "atr_trail_mult", "default": self.atr_trail_mult, "values": [2.0, 2.5, 3.0, 3.5]},
            {"name": "risk_budget", "default": self.risk_budget, "values": [0.30, 0.45, 0.60]},
        ]

    def diagnose_no_signal(self, df, context) -> list[str]:
        if len(df) < 60 or "Final_Score" not in df.columns:
            return ["至少需要60根K线和 Final_Score"]
        close = df["close"].astype(float)
        price = float(close.iloc[-1])
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = float(close.rolling(60).mean().iloc[-1])
        score = float(df["Final_Score"].iloc[-1])
        pct = compute_percentile_score(df, window=252).iloc[-1]
        atr = compute_atr(df, 14).iloc[-1]
        if context.position.shares > 0:
            trail = context.position.highest_close - self.atr_trail_mult * atr
            return [
                f"价格{price:.2f}尚未有效跌破MA60({ma60:.2f})",
                f"MA20({ma20:.2f})尚未死叉MA60({ma60:.2f})",
                f"价格尚未跌破移动止盈线{trail:.2f}",
            ]
        missing = []
        if price <= ma60:
            missing.append(f"价格{price:.2f}需站上MA60({ma60:.2f})")
        if ma20 <= ma60:
            missing.append(f"MA20({ma20:.2f})需上穿MA60({ma60:.2f})")
        if score <= 0 or pd.isna(pct) or pct < 0.30:
            pct_text = f"{pct:.0%}" if pd.notna(pct) else "样本不足"
            missing.append(f"Final_Score需转正且百分位达到30%（当前{score:+.3f}/{pct_text}）")
        return missing or ["ATR或可下单股数无效"]

    def _evaluate_decision(
        self, df: pd.DataFrame, context: StrategyContext
    ):
        if len(df) < 60:
            return self._no_signal_decision(df, context)

        row = df.iloc[-1]
        score = row.get("Final_Score", 0.0)
        if pd.isna(score):
            return self._no_signal_decision(df, context)

        close = df["close"].astype(float)
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        latest_close = float(close.iloc[-1])
        latest_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else 0
        latest_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else 0

        # 计算 ATR
        atr = compute_atr(df, 14)
        latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else latest_close * 0.02

        # —— 开仓信号（仅空仓时） ——
        if context.position.shares == 0 and latest_ma20 > 0 and latest_ma60 > 0:
            score_pct = compute_percentile_score(df, window=252)
            latest_pct = float(score_pct.iloc[-1]) if pd.notna(score_pct.iloc[-1]) else 0

            # 开仓条件：三重确认
            above_ma60 = latest_close > latest_ma60
            ma_trend_up = latest_ma20 > latest_ma60
            score_positive = score > 0 and latest_pct >= 0.30

            if above_ma60 and ma_trend_up and score_positive:
                # 风险预算仓位：shares = risk_budget × equity / (atr_stop_mult × ATR)
                stop_distance = self.atr_stop_mult * latest_atr
                risk_amount = self.risk_budget * context.equity
                shares = round_lot_shares(risk_amount / stop_distance, context.market)
                stop_loss = latest_close - stop_distance

                decision = self._execution_decision(
                    df, context, action="buy",
                    shares=shares,
                    stop_loss=round(stop_loss, 2),
                    reason=(
                        f"中长期趋势确认 | close({latest_close:.2f})>MA60({latest_ma60:.2f}) "
                        f"MA20({latest_ma20:.2f})>MA60 Score={score:+.3f} pct={latest_pct:.1%} "
                        f"止损={stop_loss:.2f} 股数={shares}"
                    ),
                    time_stop_days=250,   # 中长期持有，不设短时间止损
                    hard_stop_pct=0.25,   # 放宽硬止损至 25%
                )
                logger.info(
                    f"[策略H] {df['date'].iloc[-1]} 开仓 | "
                    f"价={latest_close:.2f}>MA60={latest_ma60:.2f} "
                    f"MA20>MA60 | Score={score:+.3f} | 股数={shares} 止损={stop_loss:.2f}"
                )
                return decision

        # —— 平仓信号（仅持仓时） ——
        elif context.position.shares > 0:
            should_sell = False
            sell_reason = ""

            # 条件 1：价格有效跌破 MA60（2% 缓冲，避免反复穿越）
            if latest_close < latest_ma60 * 0.98 and latest_ma60 > 0:
                should_sell = True
                sell_reason = (
                    f"趋势破坏：close({latest_close:.2f})<MA60({latest_ma60:.2f})×0.98"
                )

            # 条件 2：MA20 死叉 MA60（中期趋势转空）
            if not should_sell and latest_ma20 < latest_ma60 and latest_ma20 > 0:
                prev_ma20 = float(ma20.iloc[-2]) if len(ma20) >= 2 and pd.notna(ma20.iloc[-2]) else 0
                prev_ma60 = float(ma60.iloc[-2]) if len(ma60) >= 2 and pd.notna(ma60.iloc[-2]) else 0
                # 确认是刚死叉（上一天还是金叉状态）
                if prev_ma20 >= prev_ma60:
                    should_sell = True
                    sell_reason = (
                        f"中期转空：MA20({latest_ma20:.2f})死叉MA60({latest_ma60:.2f})"
                    )

            # 条件 3：移动止盈（最高收盘 - atr_trail_mult × ATR）
            if not should_sell and context.position.highest_close > 0:
                trail_stop = context.position.highest_close - self.atr_trail_mult * latest_atr
                if latest_close < trail_stop:
                    should_sell = True
                    sell_reason = (
                        f"移动止盈：close({latest_close:.2f})<最高({context.position.highest_close:.2f})-"
                        f"{self.atr_trail_mult}×ATR({latest_atr:.2f})={trail_stop:.2f}"
                    )

            # 条件 4：超长持仓（max_hold_days 天）
            if not should_sell and context.holding_days >= self.max_hold_days:
                should_sell = True
                sell_reason = f"持仓超期：{context.holding_days}天 ≥ {self.max_hold_days}天"

            if should_sell:
                decision = self._execution_decision(
                    df, context, action="sell",
                    shares=context.position.shares,
                    reason=sell_reason,
                )
                logger.info(f"[策略H] {df['date'].iloc[-1]} 平仓 | {sell_reason}")
                return decision

        return self._no_signal_decision(df, context)
