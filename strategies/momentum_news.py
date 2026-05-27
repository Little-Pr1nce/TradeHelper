"""
策略C：动量突破+新闻共振确认策略 (Momentum Breakout + News Resonance)

核心思想：三重确认机制，大幅降低假突破概率。
仅当「技术面 Final_Score 极强 + 新闻面 FinBERT 极度正面 + 价格突破前高」
三者同时满足时才开仓。

规则：
  - 开仓：Final_Score > 0.7 AND FinBERT > 0.8 AND close > 20 日最高点
  - 平仓：Final_Score < 0.4 或移动止盈（从持仓最高点回撤 2 × ATR）
  - 冷却期：2 根 K 线（动量策略可以更积极）
  - 仓位：金字塔加仓模式
    · 首次开仓：1% 风险预算
    · 加仓条件：Final_Score 持续 > 0.8 且浮盈 > 1 × ATR → 追加 0.5%
    · 总敞口限制：≤ 3%

适用场景：高胜率右侧交易，依赖新闻与技术面的双重确认。
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    BaseExecutionStrategy, Order, StrategyContext, compute_atr,
)

logger = logging.getLogger(__name__)


class MomentumNewsStrategy(BaseExecutionStrategy):
    """
    动量突破+新闻共振确认策略（含金字塔加仓）。

    参数说明：
      entry_score=0.7        — 开仓所需的 Final_Score 最低值
      finbert_min=0.8        — 开仓所需的 FinBERT 得分最低值
      breakout_window=20     — 价格突破窗口（日内最高点）
      cooldown_bars=2        — 冷却期（动量策略可较短）
      trailing_atr_mult=2.0  — 移动止盈：最高点回撤 2 倍 ATR
      add_atr_mult=1.0       — 加仓条件：浮盈超过 1 倍 ATR
      first_risk=0.01        — 首次风险预算 1%
      add_risk=0.005         — 加仓风险预算 0.5%
      max_risk=0.03          — 总敞口上限 3%
    """

    def __init__(self, entry_score: float = 0.7, exit_score: float = 0.4,
                 finbert_min: float = 0.8, breakout_window: int = 20,
                 cooldown_bars: int = 2, atr_period: int = 14,
                 trailing_atr_mult: float = 2.0, add_atr_mult: float = 1.0,
                 first_risk: float = 0.01, add_risk: float = 0.005,
                 max_risk: float = 0.03):
        self.entry_score = entry_score
        self.exit_score = exit_score
        self.finbert_min = finbert_min
        self.breakout_window = breakout_window
        self.cooldown_bars = cooldown_bars
        self.atr_period = atr_period
        self.trailing_atr_mult = trailing_atr_mult
        self.add_atr_mult = add_atr_mult
        self.first_risk = first_risk
        self.add_risk = add_risk
        self.max_risk = max_risk

    @property
    def name(self) -> str:
        return (f"动量突破+新闻共振 (score>{self.entry_score}, "
                f"FinBERT>{self.finbert_min}, breakout{self.breakout_window}d)")

    @property
    def description(self) -> str:
        return (
            f"三重确认开仓：Final_Score > {self.entry_score}、"
            f"FinBERT > {self.finbert_min}、价格突破 {self.breakout_window} 日高点。"
            f"金字塔加仓模式，总敞口 ≤ {self.max_risk*100:.0f}%。"
        )

    def _check_entry_conditions(self, df: pd.DataFrame) -> tuple[bool, str]:
        """
        检查三重开仓条件。

        返回 (是否满足, 不满足原因)。
        三重确认缺一不可：
          ① 技术面：Final_Score > entry_score（因子极度看多）
          ② 新闻面：FinBERT > finbert_min（新闻极度正面）
          ③ 价格面：收盘价突破 N 日最高点（趋势确认）
        """
        final_score = float(df["Final_Score"].iloc[-1])
        close = float(df["close"].iloc[-1])

        # 条件 ①
        if final_score <= self.entry_score:
            return False, f"Final_Score({final_score:.3f}) ≤ {self.entry_score}"

        # 条件 ②
        if "FinBERT_Score" in df.columns:
            finbert = float(df["FinBERT_Score"].iloc[-1])
            if finbert <= self.finbert_min:
                return False, f"FinBERT({finbert:.3f}) ≤ {self.finbert_min}"

        # 条件 ③：收盘价 > 过去 N 日最高价
        lookback = min(self.breakout_window, len(df) - 1)
        if lookback > 0:
            highest = df["close"].iloc[-(lookback + 1):-1].max()
            if close <= highest:
                return False, f"close({close:.2f}) ≤ {self.breakout_window}d high({highest:.2f})"

        return True, ""

    def _calc_shares(self, risk_budget: float, atr: float) -> int:
        """根据风险预算和 ATR 计算目标股数（取整到 100 股）。"""
        stop_distance = 2 * atr
        return max(int(risk_budget / stop_distance / 100) * 100, 100)

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        if df.empty or "Final_Score" not in df.columns:
            return []

        idx = len(df) - 1
        current_date = str(df.iloc[-1].get("date", ""))[:10]
        final_score = float(df["Final_Score"].iloc[-1])
        close = float(df["close"].iloc[-1])
        atr = compute_atr(df, self.atr_period).iloc[-1]
        has_position = context.position.shares > 0

        # ── 金字塔加仓（持仓中 + 未加过仓） ──
        if has_position and not context.position.added_position:
            can_enter, _ = self._check_entry_conditions(df)
            if can_enter and final_score > 0.8 and pd.notna(atr) and atr > 0:
                entry_price = context.position.entry_price
                profit_atr = (close - entry_price) / atr
                # 加仓条件：浮盈 > 1 × ATR 且总敞口不超标
                if profit_atr >= self.add_atr_mult:
                    existing_risk_amount = context.position.shares * 2 * atr
                    max_risk_amount = context.equity * self.max_risk
                    add_risk_amount = context.equity * self.add_risk
                    if existing_risk_amount + add_risk_amount <= max_risk_amount:
                        add_shares = self._calc_shares(add_risk_amount, atr)
                        if add_shares > 0:
                            logger.info(
                                f"[策略C] {current_date} 金字塔加仓 | "
                                f"股数=+{add_shares}, 浮盈={profit_atr:.1f}×ATR"
                            )
                            return [Order(
                                date=current_date, action="buy", shares=add_shares,
                                stop_loss=context.position.stop_loss,
                                reason=f"加仓: Score={final_score:.3f}, profit={profit_atr:.1f}×ATR",
                            )]

        # ── 平仓条件 ──
        if has_position:
            entry_price = context.position.entry_price
            highest_since_entry = max(context.position.highest_close, close)
            # 移动止盈线 = 持仓期间最高价 - 2 × ATR
            trailing_stop = highest_since_entry - self.trailing_atr_mult * atr

            # 条件 1：Final_Score 跌破退出阈值
            # 条件 2：价格跌破移动止盈线
            score_exit = final_score < self.exit_score
            trailing_exit = close < trailing_stop if pd.notna(atr) and atr > 0 else False

            if score_exit or trailing_exit:
                reason = (
                    f"Final_Score({final_score:.3f}) < {self.exit_score}"
                    if score_exit
                    else f"移动止盈: close({close:.2f}) < trail({trailing_stop:.2f})"
                )
                logger.info(f"[策略C] {current_date} 平仓 | {reason}")
                return [Order(
                    date=current_date, action="sell",
                    shares=context.position.shares, reason=reason,
                )]
            return []

        # ── 开仓条件：三重确认 ──
        can_enter, reject_reason = self._check_entry_conditions(df)
        if not can_enter:
            return []

        # 冷却期检查
        if idx < context.cooldown_until:
            logger.debug(f"[策略C] {current_date} 冷却期")
            return []

        if pd.isna(atr) or atr <= 0:
            return []

        # 首次开仓：1% 风险预算
        risk_amount = context.equity * self.first_risk
        shares = self._calc_shares(risk_amount, atr)
        stop_loss = close - self.trailing_atr_mult * atr

        logger.info(
            f"[策略C] {current_date} 三重确认开仓 | "
            f"Final_Score={final_score:.3f} | 股数={shares}"
        )
        return [Order(
            date=current_date, action="buy", shares=shares,
            stop_loss=stop_loss,
            reason=f"三重确认: Score={final_score:.3f}",
        )]
