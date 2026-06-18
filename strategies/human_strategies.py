"""
人类交易策略库（新手 + 老手）。

不做行情筛选，始终活跃。与量化策略（A-H）的核心区别：
  - 逻辑基于单一、直观的技术条件（而非多因子复合）
  - 模拟真实交易者的决策模式（追涨、抄底、扛单、回调进场等）
  - 始终运行，不受 market_regime 过滤影响
"""

import logging

import numpy as np
import pandas as pd

from strategies.base import (
    BaseExecutionStrategy, Order, Position, StrategyContext,
    compute_atr, compute_percentile_score,
)

logger = logging.getLogger(__name__)


# ╔══════════════════════════════════════════════════════════════╗
# ║                    新手策略 (I / J / K)                      ║
# ╚══════════════════════════════════════════════════════════════╝


class ChaseMomentumStrategy(BaseExecutionStrategy):
    """I: 追涨杀跌 — 新手最常见的操作：金叉就追、死叉就跑。"""

    suitable_regimes: list[str] = []   # 始终活跃

    def __init__(self):
        pass

    @property
    def name(self) -> str:
        return "I 追涨杀跌（新手）"

    @property
    def description(self) -> str:
        return "新手：MA5金叉MA20买入80%仓位，死叉卖出+移动止盈，60天时间止损"

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        orders = []
        if len(df) < 30:
            return orders

        close = df["close"].astype(float)
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        latest_close = float(close.iloc[-1])

        # —— 开仓 ——
        if context.position.shares == 0:
            gold_cross = (
                float(ma5.iloc[-1]) > float(ma20.iloc[-1])
                and float(ma5.iloc[-2]) <= float(ma20.iloc[-2])
            )
            if gold_cross:
                # 用 80% 可用资金买入
                invest_amount = context.cash * 0.80
                shares = max(100, int(invest_amount / latest_close / 100) * 100)
                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="buy",
                    shares=shares,
                    reason=f"金叉买入 MA5({ma5.iloc[-1]:.2f})>MA20({ma20.iloc[-1]:.2f}) {shares}股",
                    time_stop_days=60,
                    hard_stop_pct=0.15,
                ))
                logger.debug(f"[策略I] 金叉买入 close={latest_close:.2f} shares={shares}")

        # —— 平仓 ——
        elif context.position.shares > 0:
            should_sell = False
            sell_reason = ""

            # 条件 1：死叉
            death_cross = (
                float(ma5.iloc[-1]) < float(ma20.iloc[-1])
                and float(ma5.iloc[-2]) >= float(ma20.iloc[-2])
            )
            if death_cross:
                should_sell = True
                sell_reason = f"死叉卖出 MA5({ma5.iloc[-1]:.2f})<MA20({ma20.iloc[-1]:.2f})"

            # 条件 2：移动止盈（最高收盘 - 3×ATR）
            if not should_sell and context.position.highest_close > 0:
                atr = compute_atr(df, 14)
                latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else latest_close * 0.02
                trail_stop = context.position.highest_close - 3.0 * latest_atr
                if latest_close < trail_stop:
                    should_sell = True
                    sell_reason = f"移动止盈: close({latest_close:.2f})<最高({context.position.highest_close:.2f})-3×ATR({latest_atr:.2f})"

            if should_sell:
                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="sell",
                    shares=context.position.shares,
                    reason=sell_reason,
                ))
                logger.debug(f"[策略I] {sell_reason}")

        return orders


class PickBottomStrategy(BaseExecutionStrategy):
    """J: 抄底摸顶 — 新手觉得超卖就该反弹，小赚就跑。"""

    suitable_regimes: list[str] = []

    def __init__(self, rsi_oversold: float = 30.0, rsi_overbought: float = 70.0,
                 take_profit_pct: float = 0.10):
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.take_profit_pct = take_profit_pct

    @property
    def name(self) -> str:
        return "J 抄底摸顶（新手）"

    @property
    def description(self) -> str:
        return f"新手：RSI<{self.rsi_oversold:.0f}抄底60%仓位，RSI>{self.rsi_overbought:.0f}或移动止盈清仓"

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        orders = []
        if len(df) < 20:
            return orders

        close = df["close"].astype(float)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        latest_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50
        latest_close = float(close.iloc[-1])

        # —— 开仓 ——
        if context.position.shares == 0 and latest_rsi < self.rsi_oversold:
            invest_amount = context.cash * 0.60
            shares = max(100, int(invest_amount / latest_close / 100) * 100)
            orders.append(Order(
                date=str(df["date"].iloc[-1])[:10],
                action="buy",
                shares=shares,
                reason=f"RSI({latest_rsi:.1f})<{self.rsi_oversold:.0f} 超卖抄底 {shares}股",
                time_stop_days=60,
                hard_stop_pct=0.12,
            ))
            logger.debug(f"[策略J] 抄底 close={latest_close:.2f} RSI={latest_rsi:.1f} shares={shares}")

        # —— 平仓 ——
        elif context.position.shares > 0:
            should_sell = False
            reason = ""

            # 条件 1：RSI 超买
            if latest_rsi > self.rsi_overbought:
                should_sell = True
                reason = f"RSI({latest_rsi:.1f})>{self.rsi_overbought:.0f} 超买"

            # 条件 2：移动止盈（最高收盘 - 2×ATR），替代原来的 +10% 固定止盈
            if not should_sell and context.position.highest_close > 0:
                atr = compute_atr(df, 14)
                latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else latest_close * 0.02
                trail_stop = context.position.highest_close - 2.0 * latest_atr
                if latest_close < trail_stop:
                    should_sell = True
                    reason = f"移动止盈: close({latest_close:.2f})<最高({context.position.highest_close:.2f})-2×ATR({latest_atr:.2f})"

            # 条件 3：跌破开仓价 8%（硬止损保底）
            if not should_sell and context.position.entry_price > 0:
                loss_pct = (latest_close - context.position.entry_price) / context.position.entry_price
                if loss_pct < -0.08:
                    should_sell = True
                    reason = f"跌破成本{loss_pct:.1%} 止损"

            if should_sell:
                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="sell",
                    shares=context.position.shares,
                    reason=reason,
                ))

        return orders


class HoldUntilBreakevenStrategy(BaseExecutionStrategy):
    """K: 死扛回本 — 大跌抄底，回本即卖，跌太多扛不住割肉。"""

    suitable_regimes: list[str] = []

    def __init__(self, dip_pct: float = 0.03, cut_loss_pct: float = 0.15):
        self.dip_pct = dip_pct
        self.cut_loss_pct = cut_loss_pct

    @property
    def name(self) -> str:
        return "K 死扛回本（新手）"

    @property
    def description(self) -> str:
        return f"新手：单日跌>{self.dip_pct:.0%}且收阳抄底70%仓位，移动止盈，-{self.cut_loss_pct:.0%}割肉"

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        orders = []
        if len(df) < 3:
            return orders

        close = df["close"].astype(float)
        row = df.iloc[-1]
        latest_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else latest_close
        change_pct = (latest_close - prev_close) / prev_close if prev_close > 0 else 0

        # —— 开仓 ——
        if context.position.shares == 0:
            prev_day_dip = (float(close.iloc[-2]) - float(close.iloc[-3])) / float(close.iloc[-3]) if len(close) >= 3 and float(close.iloc[-3]) > 0 else 0
            today_up = latest_close > prev_close
            if prev_day_dip < -self.dip_pct and today_up:
                invest_amount = context.cash * 0.70
                shares = max(100, int(invest_amount / latest_close / 100) * 100)
                cut_price = round(latest_close * (1 - self.cut_loss_pct), 2)
                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="buy",
                    shares=shares,
                    stop_loss=cut_price,
                    reason=f"昨日跌{prev_day_dip:.1%}今日反弹 抄底 {shares}股",
                    time_stop_days=90,         # 给足够时间让趋势发展
                    hard_stop_pct=self.cut_loss_pct,   # 由 cut_loss_pct 控制硬止损
                ))
                logger.debug(f"[策略K] 抄底 close={latest_close:.2f} shares={shares} 止损={cut_price}")

        # —— 平仓 ——
        elif context.position.shares > 0 and context.position.entry_price > 0:
            should_sell = False
            reason = ""

            gain_pct = (latest_close - context.position.entry_price) / context.position.entry_price

            # 核心改造：不再"回本就卖"！用移动止盈替代
            # 条件 1：移动止盈 — 从最高点回撤 2×ATR
            if context.position.highest_close > 0:
                atr = compute_atr(df, 14)
                latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else latest_close * 0.02
                trail_stop = context.position.highest_close - 2.0 * latest_atr
                if latest_close < trail_stop and gain_pct > 0.03:  # 至少盈利 3% 后才启用移动止盈
                    should_sell = True
                    reason = f"移动止盈: close({latest_close:.2f})<最高({context.position.highest_close:.2f})-2×ATR({latest_atr:.2f}) 盈利{gain_pct:.1%}"

            # 条件 2：-15% 割肉止损由 Broker 层 stop_loss 处理（已在开仓时设置）

            if should_sell:
                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="sell",
                    shares=context.position.shares,
                    reason=reason,
                ))

        return orders


# ╔══════════════════════════════════════════════════════════════╗
# ║                    老手策略 (L / M / N)                      ║
# ╚══════════════════════════════════════════════════════════════╝


class TrendPullbackStrategy(BaseExecutionStrategy):
    """L: 趋势回调买入 — 老手等 MA60 上行中回踩 MA20 缩量企稳时进场。"""

    suitable_regimes: list[str] = []

    def __init__(self, risk_budget: float = 0.50, take_profit_pct: float = 1.00):
        self.risk_budget = risk_budget
        self.take_profit_pct = take_profit_pct

    @property
    def name(self) -> str:
        return "L 趋势回调（老手）"

    @property
    def description(self) -> str:
        return f"老手：MA60上行+价>MA60+回踩MA20放量企稳进场，+{self.take_profit_pct:.0%}止盈（大波段）"

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        orders = []
        if len(df) < 80:
            return orders

        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        latest_close = float(close.iloc[-1])
        latest_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else 0
        latest_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else 0
        prev_ma60 = float(ma60.iloc[-21]) if len(ma60) >= 21 and pd.notna(ma60.iloc[-21]) else 0
        avg_vol = float(volume.rolling(20).mean().iloc[-1]) if pd.notna(volume.rolling(20).mean().iloc[-1]) else 0
        latest_vol = float(volume.iloc[-1])

        # —— 开仓 ——
        if context.position.shares == 0 and latest_ma60 > 0:
            ma60_rising = latest_ma60 > prev_ma60 if prev_ma60 > 0 else True
            above_ma60 = latest_close > latest_ma60
            near_ma20 = (
                abs(latest_close - latest_ma20) / latest_ma20 < 0.03
                if latest_ma20 > 0 else False
            )
            vol_ok = latest_vol > avg_vol * 0.8 if avg_vol > 0 else True
            # 收阳确认
            today_up = (
                len(close) >= 2 and latest_close > float(close.iloc[-2])
            )

            if ma60_rising and above_ma60 and near_ma20 and vol_ok and today_up:
                atr = compute_atr(df, 14)
                latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else latest_close * 0.02
                stop_distance = 2.0 * latest_atr
                risk_amount = self.risk_budget * context.equity
                shares = max(100, int(risk_amount / stop_distance / 100) * 100)

                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="buy",
                    shares=shares,
                    stop_loss=round(latest_close - stop_distance, 2),
                    reason=(
                        f"趋势回调: MA60↑ close({latest_close:.2f})回踩MA20({latest_ma20:.2f}) "
                        f"放量企稳 vol={latest_vol:.0f}"
                    ),
                    time_stop_days=120,
                    hard_stop_pct=0.15,
                ))
                logger.info(f"[策略L] 回调买入 close={latest_close:.2f} shares={shares}")

        # —— 平仓 ——
        elif context.position.shares > 0:
            should_sell = False
            reason = ""

            # MA60 转空
            if latest_ma60 > 0 and prev_ma60 > 0 and latest_ma60 < prev_ma60:
                should_sell = True
                reason = "MA60 转空"
            # 止盈
            if not should_sell and context.position.entry_price > 0:
                gain_pct = (latest_close - context.position.entry_price) / context.position.entry_price
                if gain_pct >= self.take_profit_pct:
                    should_sell = True
                    reason = f"+{gain_pct:.1%} 止盈"
            # 移动止盈：近 10 日最低点
            if not should_sell and len(close) >= 10:
                recent_low = float(close.iloc[-10:].min())
                if latest_close < recent_low:
                    should_sell = True
                    reason = f"跌破10日低点({recent_low:.2f})"

            if should_sell:
                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="sell",
                    shares=context.position.shares,
                    reason=reason,
                ))

        return orders


class KeyReversalStrategy(BaseExecutionStrategy):
    """M: 关键位反转 — 价格触及支撑位时出现放量弹簧形态，紧止损博弈反转。"""

    suitable_regimes: list[str] = []

    def __init__(self, risk_budget: float = 0.40, atr_stop_mult: float = 1.5,
                 take_profit_pct: float = 0.50, max_hold_days: int = 30):
        self.risk_budget = risk_budget
        self.atr_stop_mult = atr_stop_mult
        self.take_profit_pct = take_profit_pct
        self.max_hold_days = max_hold_days

    @property
    def name(self) -> str:
        return "M 关键反转（老手）"

    @property
    def description(self) -> str:
        return f"老手：支撑位放量弹簧阳线反转，紧止损({self.atr_stop_mult}×ATR)，+{self.take_profit_pct:.0%}大止盈"

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        orders = []
        if len(df) < 20:
            return orders

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)
        ma60 = close.rolling(60).mean()

        latest_close = float(close.iloc[-1])
        latest_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else 0
        latest_low = float(low.iloc[-1])
        latest_high = float(high.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else latest_close
        prev_low = float(low.iloc[-2]) if len(low) >= 2 else latest_low
        avg_vol_20 = float(volume.rolling(20).mean().iloc[-1]) if pd.notna(volume.rolling(20).mean().iloc[-1]) else 0
        latest_vol = float(volume.iloc[-1])

        # 找关键支撑：MA60 或 20 日低点
        support_level = latest_ma60
        if len(close) >= 20:
            low20 = float(close.iloc[-20:].min())
            if low20 > 0 and (support_level <= 0 or low20 > support_level):
                support_level = low20

        # —— 开仓 ——
        if context.position.shares == 0 and support_level > 0:
            near_support = abs(latest_close - support_level) / support_level < 0.03

            # 弹簧形态：今日低点 < 昨日低点，但收盘 > 昨日收盘（盘中洗盘后收回）
            spring_candle = (
                latest_low < prev_low
                and latest_close > prev_close
            )
            # 收盘在今日振幅上半区
            daily_range = latest_high - latest_low
            in_upper_half = (
                (latest_close - latest_low) / daily_range > 0.5
                if daily_range > 0 else False
            )
            vol_spike = latest_vol > avg_vol_20 if avg_vol_20 > 0 else True

            if near_support and spring_candle and in_upper_half and vol_spike:
                atr = compute_atr(df, 14)
                latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else latest_close * 0.02
                stop_distance = self.atr_stop_mult * latest_atr
                risk_amount = self.risk_budget * context.equity
                shares = max(100, int(risk_amount / stop_distance / 100) * 100)

                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="buy",
                    shares=shares,
                    stop_loss=round(latest_close - stop_distance, 2),
                    reason=(
                        f"关键反转: 支撑{support_level:.2f}附近 弹簧阳线 "
                        f"vol={latest_vol:.0f}>{avg_vol_20:.0f}"
                    ),
                    time_stop_days=30,
                    hard_stop_pct=0.12,
                ))
                logger.info(f"[策略M] 反转买入 close={latest_close:.2f} 止损={latest_close-stop_distance:.2f}")

        # —— 平仓 ——
        elif context.position.shares > 0:
            should_sell = False
            reason = ""

            # 止盈
            if context.position.entry_price > 0:
                gain_pct = (latest_close - context.position.entry_price) / context.position.entry_price
                if gain_pct >= self.take_profit_pct:
                    should_sell = True
                    reason = f"+{gain_pct:.1%} 止盈"
            # 跌破支撑位
            if not should_sell and support_level > 0 and latest_close < support_level * 0.98:
                should_sell = True
                reason = f"跌破支撑({support_level:.2f})"
            # 超期
            if not should_sell and context.holding_days >= self.max_hold_days:
                should_sell = True
                reason = f"持仓{context.holding_days}天未达目标"

            if should_sell:
                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="sell",
                    shares=context.position.shares,
                    reason=reason,
                ))

        return orders


class MACompressionBreakoutStrategy(BaseExecutionStrategy):
    """N: 均线粘合突破 — 识别低波动压缩区，放量突破时进场预判大波动。"""

    suitable_regimes: list[str] = []

    def __init__(self, risk_budget: float = 0.50, atr_trail_mult: float = 3.0,
                 max_hold_days: int = 60):
        self.risk_budget = risk_budget
        self.atr_trail_mult = atr_trail_mult
        self.max_hold_days = max_hold_days

    @property
    def name(self) -> str:
        return "N 粘合突破（老手）"

    @property
    def description(self) -> str:
        return f"老手：均线粘合+放量突破进场，{self.atr_trail_mult}×ATR移动止盈，最长{self.max_hold_days}天"

    def generate_orders(self, df: pd.DataFrame, context: StrategyContext) -> list[Order]:
        orders = []
        if len(df) < 30:
            return orders

        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)

        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()

        latest_close = float(close.iloc[-1])
        latest_ma5 = float(ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else 0
        latest_ma10 = float(ma10.iloc[-1]) if pd.notna(ma10.iloc[-1]) else 0
        latest_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else 0

        # —— 开仓 ——
        if context.position.shares == 0 and latest_ma5 > 0:
            # 均线粘合：MA5/MA10/MA20 三者间距 < 3%
            mas = [latest_ma5, latest_ma10, latest_ma20]
            compression = (max(mas) - min(mas)) / min(mas) < 0.03 if min(mas) > 0 else False

            # 放量突破：今日振幅 > 近10日平均振幅 × 1.5
            recent_range = (high - low).iloc[-10:].mean()
            today_range = float(high.iloc[-1] - low.iloc[-1])
            range_expansion = today_range > recent_range * 1.5 if recent_range > 0 else False

            # 收盘站上全部均线
            above_all = latest_close > latest_ma5 and latest_close > latest_ma10 and latest_close > latest_ma20
            # 收阳
            today_up = latest_close > float(close.iloc[-2]) if len(close) >= 2 else True

            if compression and range_expansion and above_all and today_up:
                atr = compute_atr(df, 14)
                latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else latest_close * 0.02
                stop_distance = 1.5 * latest_atr
                risk_amount = self.risk_budget * context.equity
                shares = max(100, int(risk_amount / stop_distance / 100) * 100)

                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="buy",
                    shares=shares,
                    stop_loss=round(latest_close - stop_distance, 2),
                    reason=(
                        f"粘合突破: MA5/10/20间距<3% 今日振幅{today_range:.2f}"
                        f">{recent_range*1.5:.2f} close>{latest_ma20:.2f}"
                    ),
                    time_stop_days=60,
                    hard_stop_pct=0.12,
                ))
                logger.info(f"[策略N] 突破买入 close={latest_close:.2f} shares={shares}")

        # —— 平仓 ——
        elif context.position.shares > 0:
            should_sell = False
            reason = ""

            # 收盘跌破 MA20
            if latest_ma20 > 0 and latest_close < latest_ma20:
                should_sell = True
                reason = f"跌破MA20({latest_ma20:.2f})"
            # ATR 移动止盈
            if not should_sell and context.position.highest_close > 0:
                atr = compute_atr(df, 14)
                latest_atr = float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else latest_close * 0.02
                trail = context.position.highest_close - self.atr_trail_mult * latest_atr
                if latest_close < trail:
                    should_sell = True
                    reason = f"移动止盈: close<最高-{self.atr_trail_mult}×ATR"
            # 超期
            if not should_sell and context.holding_days >= self.max_hold_days:
                should_sell = True
                reason = f"持仓{context.holding_days}天到期"

            if should_sell:
                orders.append(Order(
                    date=str(df["date"].iloc[-1])[:10],
                    action="sell",
                    shares=context.position.shares,
                    reason=reason,
                ))

        return orders
