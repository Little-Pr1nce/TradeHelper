"""
策略A：阈值滞后带趋势跟踪策略 (Threshold Trend Strategy)

核心思想：利用滞后带（Hysteresis Band）过滤噪音，避免频繁交易。
开仓阈值 (0.6) 高于平仓阈值 (0.3)，形成缓冲区间，
防止 Final_Score 在临界值附近反复穿越导致频繁开平。

规则：
  - 开仓：Final_Score > 0.6（强买入信号）
  - 平仓：Final_Score < 0.3（信号减弱但尚未反转）
  - 冷却期：平仓后 3 根 K 线内不再开仓
  - 仓位：风险预算 2% 净值 / (2 × ATR(14))，向下取整至 100 股
  - 硬止损：-8%（由 Broker 层统一执行）
  - 时间止损：10 个交易日（由 Broker 层统一执行）

适用场景：作为基准策略，验证 Final_Score 因子本身的有效性。
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    BaseExecutionStrategy, Order, Position, StrategyContext, compute_atr,
)

logger = logging.getLogger(__name__)


class ThresholdTrendStrategy(BaseExecutionStrategy):
    """
    阈值滞后带趋势跟踪（基准策略）。

    参数说明：
      entry_threshold=0.6   — 开仓阈值：Final_Score 必须大于此值才买入
      exit_threshold=0.3    — 平仓阈值：Final_Score 低于此值即卖出
      cooldown_bars=3       — 冷却期：平仓后需要等 3 根 K 线
      atr_mult_stop=2.0     — 止损距离 = 2 倍 ATR
      risk_budget=0.02      — 单笔风险预算 = 账户净值的 2%
    """

    def __init__(self, entry_threshold: float = 0.6, exit_threshold: float = 0.3,
                 cooldown_bars: int = 3, atr_period: int = 14, atr_mult_stop: float = 2.0,
                 risk_budget: float = 0.02):
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.cooldown_bars = cooldown_bars
        self.atr_period = atr_period
        self.atr_mult_stop = atr_mult_stop
        self.risk_budget = risk_budget

    @property
    def name(self) -> str:
        return f"阈值滞后带趋势跟踪 (entry>{self.entry_threshold}, exit<{self.exit_threshold})"

    @property
    def description(self) -> str:
        return (
            f"当 Final_Score > {self.entry_threshold} 时开仓，"
            f"Final_Score < {self.exit_threshold} 时平仓。"
            f"冷却期 {self.cooldown_bars} 根 K 线，止损距离 = {self.atr_mult_stop}×ATR。"
        )

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        """
        根据当前 Final_Score 和持仓状态生成买卖指令。

        决策逻辑：
          ① 如果持仓 → 检查平仓条件（Final_Score < exit_threshold）
          ② 如果空仓 → 检查开仓条件（Final_Score > entry_threshold + 不在冷却期）
          ③ 开仓时计算仓位：股数 = (净值 × 2%) / (2 × ATR)，向下取整 100 股
        """
        if df.empty or "Final_Score" not in df.columns:
            return []

        idx = len(df) - 1                      # 当前 T 日的 bar 索引
        current_date = str(df.iloc[-1].get("date", ""))[:10]
        final_score = float(df["Final_Score"].iloc[-1])
        has_position = context.position.shares > 0

        # ── 平仓条件：持仓中 + Final_Score 跌破平仓阈值 ──
        if has_position:
            if final_score < self.exit_threshold:
                logger.info(
                    f"[策略A] {current_date} 平仓 | "
                    f"Final_Score={final_score:.3f} < {self.exit_threshold}"
                )
                return [Order(
                    date=current_date, action="sell",
                    shares=context.position.shares,
                    reason=f"Final_Score({final_score:.3f}) < exit({self.exit_threshold})",
                )]
            return []  # 持仓但未触发平仓，继续持有

        # ── 开仓条件：空仓 + Final_Score 突破开仓阈值 ──
        if final_score > self.entry_threshold:
            # 冷却期检查
            if idx < context.cooldown_until:
                logger.debug(
                    f"[策略A] {current_date} 冷却期 (直到 K 线 {context.cooldown_until})"
                )
                return []

            # 计算 ATR 和仓位
            atr = compute_atr(df, self.atr_period).iloc[-1]
            if pd.isna(atr) or atr <= 0:
                logger.debug(f"[策略A] {current_date} ATR 无效 ({atr})")
                return []

            close = float(df["close"].iloc[-1])
            stop_distance = self.atr_mult_stop * atr               # 止损距离
            risk_amount = context.equity * self.risk_budget        # 风险预算
            shares = max(int(risk_amount / stop_distance / 100) * 100, 100)  # 股数取整
            stop_loss = close - stop_distance                      # 硬止损价格

            logger.info(
                f"[策略A] {current_date} 开仓 | "
                f"Final_Score={final_score:.3f} > {self.entry_threshold} | "
                f"股数={shares}, 止损价={stop_loss:.2f}"
            )
            return [Order(
                date=current_date, action="buy", shares=shares,
                stop_loss=stop_loss,
                reason=f"Final_Score({final_score:.3f}) > entry({self.entry_threshold})",
            )]

        return []  # 空仓且未触发开仓
