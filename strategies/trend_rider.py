"""
策略 O：趋势满仓持有 (Trend Rider)

核心思路：牛市中最好的策略就是"坐着不动"。
此策略专为对标买入持有基准而设计，在趋势确认后几乎全仓进场，
仅在趋势彻底逆转时才退出，最大程度捕获趋势收益。

与现有策略的关键区别：
  - 仓位：80% 可用资金 — 接近满仓，而非 1-2% 保守仓位
  - 退出：仅 MA60 死叉 或 25% 最大回撤 — 无时间止损，无小止盈
  - Broker 覆盖：时间止损 250 天、硬止损 25% — 极大放宽

规则：
  - 开仓：MA20 > MA60 + 价 > MA60 + Final_Score > -0.2
  - 平仓：(1) 价 < MA60 × 0.95 或 (2) MA20 死叉 MA60 或 (3) 从最高点回撤 25%
  - 仓位：80% 可用资金，按 100 股取整
  - 止损：开仓价 × 0.75（25% 硬止损，仅防黑天鹅）
  - 时间止损：250 天（约 1 年，几乎不触发）
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    DecisionFirstStrategy, StrategyContext,
    compute_atr, shares_from_cash,
)

logger = logging.getLogger(__name__)


class TrendRiderStrategy(DecisionFirstStrategy):
    """趋势满仓持有 — 专为对标买入持有基准而设计。

    设计哲学：承认在强趋势中"不动"才是最好的策略，
    只在趋势彻底逆转时才撤退，其余时间满仓持有。

    适用行情：所有趋势市（trending / trending_steady / trending_volatile）
    不适合：震荡市（ranging）— 满仓在震荡中会被反复打脸
    """

    suitable_regimes = ["trending", "trending_steady", "trending_volatile"]

    def __init__(
        self,
        invest_pct: float = 0.95,      # 仓位比例：95% 可用资金，几乎满仓
        hard_stop_pct: float = 0.25,   # 最大回撤容忍：25%
        ma60_buffer: float = 0.95,     # MA60 跌破缓冲（5% 避免假突破）
    ):
        self.invest_pct = invest_pct
        self.hard_stop_pct = hard_stop_pct
        self.ma60_buffer = ma60_buffer

    @property
    def name(self) -> str:
        return "O 趋势满仓持有（对标基准）"

    @property
    def description(self) -> str:
        return (
            f"满仓：{self.invest_pct:.0%}资金入场，仅 MA60 死叉 或 "
            f"{self.hard_stop_pct:.0%} 回撤退出，250 天不时间止损"
        )

    def tunable_params(self) -> list[dict]:
        return [
            {"name": "invest_pct", "default": self.invest_pct, "values": [0.6, 0.7, 0.8, 0.9]},
            {"name": "hard_stop_pct", "default": self.hard_stop_pct, "values": [0.15, 0.20, 0.25]},
        ]

    def diagnose_no_signal(self, df, context) -> list[str]:
        if len(df) < 80:
            return ["趋势基准策略至少需要80根K线"]
        close = df["close"].astype(float)
        price = float(close.iloc[-1])
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = float(close.rolling(60).mean().iloc[-1])
        score = float(df.iloc[-1].get("Final_Score", 0.0) or 0.0)
        if context.position.shares > 0:
            drawdown = (
                (context.position.highest_close - price) / context.position.highest_close
                if context.position.highest_close > 0 else 0.0
            )
            return [
                f"价格{price:.2f}尚未跌破MA60缓冲线{ma60 * self.ma60_buffer:.2f}",
                f"MA20({ma20:.2f})尚未死叉MA60({ma60:.2f})",
                f"高点回撤{drawdown:.1%}尚未达到{self.hard_stop_pct:.0%}",
            ]
        missing = []
        if ma20 <= ma60:
            missing.append(f"MA20({ma20:.2f})需上穿MA60({ma60:.2f})")
        if price <= ma60:
            missing.append(f"价格{price:.2f}需站上MA60({ma60:.2f})")
        if score <= -0.2:
            missing.append(f"Final_Score={score:+.3f}需高于-0.200")
        return missing or ["可用现金不足以形成最小交易单位"]

    def _evaluate_decision(
        self, df: pd.DataFrame, context: StrategyContext
    ):
        if len(df) < 80:
            return self._no_signal_decision(df, context)

        row = df.iloc[-1]
        score = row.get("Final_Score", 0.0)
        if pd.isna(score):
            score = 0.0

        close = df["close"].astype(float)
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        latest_close = float(close.iloc[-1])
        latest_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else 0
        latest_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else 0

        # —— 开仓信号（仅空仓时） ——
        if context.position.shares == 0 and latest_ma20 > 0 and latest_ma60 > 0:
            # 开仓条件：趋势向上 + 股价在 MA60 之上
            trend_up = latest_ma20 > latest_ma60
            above_ma60 = latest_close > latest_ma60
            score_ok = score > -0.2  # 极宽松，只要不是极端看空

            if trend_up and above_ma60 and score_ok:
                # 用 invest_pct 比例资金买入
                shares = shares_from_cash(context.cash, latest_close, context.market, self.invest_pct)
                stop_loss = round(latest_close * (1 - self.hard_stop_pct), 2)

                decision = self._execution_decision(
                    df, context, action="buy",
                    shares=shares,
                    stop_loss=stop_loss,
                    reason=(
                        f"趋势满仓 | MA20({latest_ma20:.2f})>MA60({latest_ma60:.2f}) "
                        f"close({latest_close:.2f})>MA60 Score={score:+.3f} "
                        f"投入{self.invest_pct:.0%}→{shares}股 止损={stop_loss}"
                    ),
                    time_stop_days=250,        # 一年不时间止损
                    hard_stop_pct=self.hard_stop_pct,
                )
                logger.info(
                    f"[策略O] {df['date'].iloc[-1]} 满仓入场 | "
                    f"价={latest_close:.2f} MA20>MA60 | "
                    f"股数={shares} ({self.invest_pct:.0%}仓位) 止损={stop_loss}"
                )
                return decision

        # —— 平仓信号（仅持仓时） ——
        elif context.position.shares > 0:
            should_sell = False
            sell_reason = ""

            # 条件 1：趋势破坏 — 价格有效跌破 MA60（5% 缓冲）
            if latest_close < latest_ma60 * self.ma60_buffer and latest_ma60 > 0:
                should_sell = True
                sell_reason = (
                    f"趋势破坏：close({latest_close:.2f})<MA60({latest_ma60:.2f})×{self.ma60_buffer}"
                )

            # 条件 2：中期趋势逆转 — MA20 死叉 MA60
            if not should_sell and latest_ma20 < latest_ma60 and latest_ma20 > 0:
                prev_ma20 = float(ma20.iloc[-2]) if len(ma20) >= 2 and pd.notna(ma20.iloc[-2]) else 0
                prev_ma60_val = float(ma60.iloc[-2]) if len(ma60) >= 2 and pd.notna(ma60.iloc[-2]) else 0
                if prev_ma20 >= prev_ma60_val:
                    should_sell = True
                    sell_reason = (
                        f"中期逆转：MA20({latest_ma20:.2f})死叉MA60({latest_ma60:.2f})"
                    )

            # 条件 3：从最高点大幅回撤（25%）
            if not should_sell and context.position.highest_close > 0:
                drawdown = (context.position.highest_close - latest_close) / context.position.highest_close
                if drawdown >= self.hard_stop_pct:
                    should_sell = True
                    sell_reason = (
                        f"大幅回撤：从最高点({context.position.highest_close:.2f})"
                        f"回撤{drawdown:.1%} ≥ {self.hard_stop_pct:.0%}"
                    )

            if should_sell:
                decision = self._execution_decision(
                    df, context, action="sell",
                    shares=context.position.shares,
                    reason=sell_reason,
                )
                logger.info(f"[策略O] {df['date'].iloc[-1]} 退出 | {sell_reason}")
                return decision

        return self._no_signal_decision(df, context)
