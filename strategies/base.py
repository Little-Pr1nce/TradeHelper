"""
交易执行策略基类模块。

定义：
  - Order:         交易指令数据类
  - StrategyDecision: 策略语义决策数据类
  - Fill:          成交记录数据类
  - StrategyContext: 策略上下文（账户状态 + 市场环境）
  - BaseExecutionStrategy: 策略抽象基类

新策略优先实现 generate_decision()；旧策略可继续实现 generate_orders()，
基类会在 StrategyDecision 和 Order 之间做兼容转换。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import numpy as np


@dataclass
class Order:
    """交易指令 — 策略输出，回测引擎消费。"""

    date: str                # T 日日期（信号产生日）
    action: str              # "buy" / "sell"
    shares: int              # 目标股数（正数）
    price_type: str = "open" # 成交价类型（"open" = T+1 开盘价）
    stop_loss: float = 0.0   # 硬止损价格（买入时计算）
    take_profit: float = float("inf")  # 止盈价格
    reason: str = ""         # 触发原因（调试用）
    # 策略级覆盖 Broker 默认值（0 = 使用 Broker 默认值）
    time_stop_days: int = 0       # 覆盖时间止损天数（0=使用默认10天）
    hard_stop_pct: float = 0.0    # 覆盖硬止损比例（0=使用默认8%）


@dataclass
class StrategyDecision:
    """策略条件化决策，用于报告和风控官分级。

    Order 仍然是回测/成交层的指令；StrategyDecision 用来表达当前
    没有下单时还差什么条件、触发价和失效条件。
    """

    action: str = "watch"          # buy / sell / hold / watch / invalid
    execution_level: str = "C"     # A=可执行 B=小仓验证 C=仅观察 D=驳回
    shares: int = 0                # 可执行股数；0 时由 position_pct/风险预算推导
    price_type: str = "open"       # 转换为 Order 时的成交价类型
    trigger_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    max_loss_amount: float = 0.0
    position_pct: float = 0.0
    invalidation: str = ""
    missing_conditions: list[str] = field(default_factory=list)
    reason: str = ""
    source: str = ""
    time_stop_days: int = 0
    hard_stop_pct: float = 0.0

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "execution_level": self.execution_level,
            "shares": int(self.shares or 0),
            "price_type": self.price_type,
            "trigger_price": round(float(self.trigger_price or 0), 4),
            "stop_loss": round(float(self.stop_loss or 0), 4),
            "take_profit": round(float(self.take_profit or 0), 4),
            "max_loss_amount": round(float(self.max_loss_amount or 0), 2),
            "position_pct": round(float(self.position_pct or 0), 4),
            "invalidation": self.invalidation,
            "missing_conditions": list(self.missing_conditions or []),
            "reason": self.reason,
            "source": self.source,
            "time_stop_days": int(self.time_stop_days or 0),
            "hard_stop_pct": round(float(self.hard_stop_pct or 0), 4),
        }


@dataclass
class Fill:
    """成交记录 — 订单撮合后的结果。"""

    date: str                # 成交日期
    order_date: str          # 信号产生日
    action: str              # "buy" / "sell"
    price: float             # 实际成交价
    shares: int              # 实际成交股数
    value: float             # 成交金额
    commission: float        # 佣金
    slippage_cost: float     # 滑点成本
    reason: str = ""         # 补充说明


@dataclass
class Position:
    """持仓信息。"""

    shares: int = 0
    avg_cost: float = 0.0
    entry_date: str = ""
    entry_price: float = 0.0
    highest_close: float = 0.0   # 持仓期间最高收盘价（移动止盈用）
    stop_loss: float = 0.0       # 当前硬止损价
    added_position: bool = False # 是否已加仓（策略C用）
    additions_count: int = 0     # 已加仓次数（策略F用）
    # 策略级 Broker 参数覆盖（0 = 使用默认值）
    time_stop_days: int = 0       # 覆盖 broker 时间止损天数
    hard_stop_pct: float = 0.0    # 覆盖 broker 硬止损比例


@dataclass
class StrategyContext:
    """策略运行上下文 — 回测引擎在每 T 日收盘时传入。"""

    date: str                      # 当前 T 日日期
    equity: float                  # 当前账户总净值
    cash: float                    # 可用现金
    position: Position             # 当前持仓
    market: str = "A"              # 市场标识
    cooldown_until: int = -1       # 冷却期结束的 K 线索引
    holding_days: int = 0          # 当前持仓天数
    # 策略 B 专用：全市场中位数波动率（单标的回测时传入外部估计值）
    market_median_volatility: float = 0.0


class BaseExecutionStrategy(ABC):
    """交易执行策略抽象基类。

    策略的唯一职责：读取截止当前时点的 DataFrame，
    结合策略上下文（仓位、资金、市场环境），生成 StrategyDecision。
    回测和实盘/报告再通过同一个 decision_to_orders() 转换为 Order。
    """

    # 适配的行情类型（空列表 = 全行情通用）
    suitable_regimes: list[str] = []

    def generate_orders(
        self, df: pd.DataFrame, context: StrategyContext
    ) -> list[Order]:
        """根据 StrategyDecision 生成交易指令。旧策略可继续覆盖此方法。"""
        return decision_to_orders(self.generate_decision(df, context), context)

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名称（用于报告显示）。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """策略描述（说明核心逻辑）。"""
        ...

    def tunable_params(self) -> list[dict]:
        """返回可调参数列表，每个元素 = {name, default, values: [候选值...]}。"""
        return []

    def generate_decision(
        self, df: pd.DataFrame, context: StrategyContext
    ) -> StrategyDecision:
        """生成条件化决策；默认由现有 Order 兼容转换。"""
        if type(self).generate_orders is BaseExecutionStrategy.generate_orders:
            raise NotImplementedError(
                f"{self.__class__.__name__} 必须实现 generate_decision() 或覆盖 generate_orders()"
            )
        orders = self.generate_orders(df, context)
        order = next((o for o in orders if o.shares > 0), None)
        if order:
            price = _latest_close(df)
            position_value = order.shares * price if price > 0 else 0.0
            position_pct = position_value / context.equity if context.equity > 0 else 0.0
            loss_amount = 0.0
            if order.action == "buy" and price > 0 and order.stop_loss > 0:
                loss_amount = max(price - order.stop_loss, 0.0) * order.shares
            return StrategyDecision(
                action=order.action,
                execution_level="A",
                shares=order.shares,
                price_type=order.price_type,
                trigger_price=price,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit if np.isfinite(order.take_profit) else 0.0,
                max_loss_amount=loss_amount,
                position_pct=position_pct,
                invalidation=(
                    f"跌破止损 {order.stop_loss:.2f}" if order.stop_loss > 0
                    else "策略条件消失"
                ),
                reason=order.reason or f"{self.name} 条件满足",
                source=self.name,
                time_stop_days=order.time_stop_days,
                hard_stop_pct=order.hard_stop_pct,
            )
        return StrategyDecision(
            action="hold" if context.position.shares > 0 else "watch",
            execution_level="C",
            trigger_price=_latest_close(df),
            invalidation="条件未触发，等待下一次分析",
            missing_conditions=["策略入场/退出条件未满足"],
            reason=f"{self.name} 当前未触发交易指令",
            source=self.name,
        )


def decision_to_orders(decision: StrategyDecision, context: StrategyContext) -> list[Order]:
    """把策略语义决策转换为 Broker 可撮合的 Order。"""
    if decision is None or decision.action not in ("buy", "sell"):
        return []
    if decision.execution_level == "D":
        return []

    shares = int(decision.shares or 0)
    if shares <= 0 and decision.action == "sell" and context.position.shares > 0:
        shares = context.position.shares
    if shares <= 0:
        return []

    return [Order(
        date=context.date,
        action=decision.action,
        shares=shares,
        price_type=decision.price_type or "open",
        stop_loss=float(decision.stop_loss or 0.0),
        take_profit=float(decision.take_profit or float("inf")) if decision.take_profit else float("inf"),
        reason=decision.reason,
        time_stop_days=int(decision.time_stop_days or 0),
        hard_stop_pct=float(decision.hard_stop_pct or 0.0),
    )]


def compute_atr(df: pd.DataFrame, period: int = 14) -> "pd.Series":
    """计算 ATR（Average True Range），使用 Wilder 平滑。"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_percentile_score(
    df: pd.DataFrame, window: int = 252, min_periods: int = 60,
) -> "pd.Series":
    """
    将 Final_Score 映射到其滚动窗口内的百分位 [0, 1]。

    解决 7 因子合成得分在单边行情中偏离 [-1,+1] 全区间的问题。
    百分位模式使得阈值（如 entry > 0.8）在任何行情下都能合理触发。

    Args:
        df: 含 Final_Score 列的 DataFrame
        window: 滚动窗口（默认 252 个交易日 ≈ 1 年）
        min_periods: 最少样本数

    Returns:
        百分位 Series（0~1），长度与 df 相同
    """
    scores = df["Final_Score"].astype(float)
    result = pd.Series(np.nan, index=scores.index, dtype=float)
    for i in range(len(scores)):
        start = max(0, i - window + 1)
        hist = scores.iloc[start:i + 1].dropna()
        if len(hist) < min_periods:
            result.iloc[i] = np.nan
        else:
            result.iloc[i] = (hist < scores.iloc[i]).mean()
    return result


def market_lot_size(market: str) -> int:
    """Return the minimum lot size for a market."""
    return 100 if market == "A" else 1


def round_lot_shares(raw_shares: float, market: str) -> int:
    """Round a raw share count down to the market lot."""
    lot = market_lot_size(market)
    if raw_shares <= 0:
        return 0
    return max(int(raw_shares / lot) * lot, lot)


def shares_from_cash(cash: float, price: float, market: str, pct: float = 1.0) -> int:
    """Convert a cash budget to a market-valid share count."""
    if cash <= 0 or price <= 0 or pct <= 0:
        return 0
    return round_lot_shares((cash * pct) / price, market)


def shares_for_risk(
    equity: float,
    cash: float,
    entry: float,
    stop_loss: float,
    market: str,
    risk_pct: float = 0.01,
    max_position_pct: float = 0.10,
) -> int:
    """按最大亏损金额和最大仓位双约束计算股数。"""
    if equity <= 0 or cash <= 0 or entry <= 0 or stop_loss <= 0 or risk_pct <= 0:
        return 0
    risk_per_share = max(entry - stop_loss, 0.0)
    if risk_per_share <= 0:
        return 0
    shares_by_risk = (equity * risk_pct) / risk_per_share
    shares_by_cash = min(cash, equity * max_position_pct) / entry
    return round_lot_shares(min(shares_by_risk, shares_by_cash), market)


def _latest_close(df: pd.DataFrame) -> float:
    if df is None or df.empty or "close" not in df.columns:
        return 0.0
    try:
        value = df["close"].iloc[-1]
        return float(value) if pd.notna(value) else 0.0
    except Exception:
        return 0.0


def _empty_result_dict() -> dict:
    """helper for strategies returning empty signal."""
    return {"score": 0.0, "percentile": np.nan, "signal": ""}
