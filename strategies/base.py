"""
交易执行策略基类模块。

定义：
  - Order:         交易指令数据类
  - Fill:          成交记录数据类
  - StrategyContext: 策略上下文（账户状态 + 市场环境）
  - BaseExecutionStrategy: 策略抽象基类

所有策略必须继承 BaseExecutionStrategy，实现 generate_orders() 方法。
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

    策略的唯一职责：读取 DataFrame 的 Final_Score 列，
    结合策略上下文（仓位、资金、市场环境），生成 Order 列表。
    """

    # 适配的行情类型（空列表 = 全行情通用）
    suitable_regimes: list[str] = []

    @abstractmethod
    def generate_orders(
        self, df: pd.DataFrame, context: StrategyContext
    ) -> list[Order]:
        """
        根据截止 T 日的历史数据生成交易指令。

        Args:
            df: 截止 T 日（含）的完整 DataFrame，必须有 Final_Score 列
            context: 策略运行上下文

        Returns:
            Order 列表（通常为空或 1 个订单）
        """
        ...

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


def _empty_result_dict() -> dict:
    """helper for strategies returning empty signal."""
    return {"score": 0.0, "percentile": np.nan, "signal": ""}



