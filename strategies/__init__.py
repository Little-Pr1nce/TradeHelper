"""
交易执行策略库。

提供策略注册与工厂方法。新策略优先输出 StrategyDecision，
回测、信号检查和报告路径再统一转换为 Order。

使用方式：
    from strategies import get_execution_strategy
    strategy = get_execution_strategy("A")
    decision = strategy.generate_decision(df, context)
"""

from strategies.base import (
    BaseExecutionStrategy,
    Order,
    Fill,
    Position,
    StrategyContext,
)
from strategies.threshold_trend import ThresholdTrendStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum_news import MomentumNewsStrategy
from strategies.bollinger_breakout import BollingerBreakoutStrategy
from strategies.dual_thrust import DualThrustStrategy
from strategies.turtle_atr import TurtleATRStrategy
from strategies.ma_crossover import MACrossoverStrategy
from strategies.ma60_trend import MA60TrendStrategy
from strategies.trend_rider import TrendRiderStrategy
from strategies.ma120_support import MA120SupportReboundStrategy
from strategies.profit_lock import ProfitLockAfterHighStrategy
from strategies.position_risk import PositionRiskManagementStrategy
from strategies.pullback_failed_exit import PullbackFailedExitStrategy
from strategies.conditional_trigger import ConditionalTriggerStrategy
from strategies.human_strategies import (
    ChaseMomentumStrategy,
    PickBottomStrategy,
    HoldUntilBreakevenStrategy,
    TrendPullbackStrategy,
    KeyReversalStrategy,
    MACompressionBreakoutStrategy,
)

# 策略注册表
_STRATEGY_REGISTRY: dict[str, type[BaseExecutionStrategy]] = {
    # 量化策略 (A-H)
    "A": ThresholdTrendStrategy,
    "B": MeanReversionStrategy,
    "C": MomentumNewsStrategy,
    "D": BollingerBreakoutStrategy,
    "E": DualThrustStrategy,
    "F": TurtleATRStrategy,
    "G": MACrossoverStrategy,
    "H": MA60TrendStrategy,
    "O": TrendRiderStrategy,          # 趋势满仓持有（对标基准）
    # 人类策略 — 新手 (I-K)
    "I": ChaseMomentumStrategy,
    "J": PickBottomStrategy,
    "K": HoldUntilBreakevenStrategy,
    # 人类策略 — 老手 (L-N)
    "L": TrendPullbackStrategy,
    "M": KeyReversalStrategy,
    "N": MACompressionBreakoutStrategy,
    # 条件触发/持仓风控策略 (P-R)
    "P": MA120SupportReboundStrategy,
    "Q": ProfitLockAfterHighStrategy,
    "R": PositionRiskManagementStrategy,
    "S": PullbackFailedExitStrategy,
    "T": ConditionalTriggerStrategy,
    # 别名
    "threshold_trend": ThresholdTrendStrategy,
    "mean_reversion": MeanReversionStrategy,
    "momentum_news": MomentumNewsStrategy,
    "bollinger_breakout": BollingerBreakoutStrategy,
    "dual_thrust": DualThrustStrategy,
    "turtle_atr": TurtleATRStrategy,
    "ma_crossover": MACrossoverStrategy,
    "ma60_trend": MA60TrendStrategy,
    "trend_rider": TrendRiderStrategy,
    "chase_momentum": ChaseMomentumStrategy,
    "pick_bottom": PickBottomStrategy,
    "hold_breakeven": HoldUntilBreakevenStrategy,
    "trend_pullback": TrendPullbackStrategy,
    "key_reversal": KeyReversalStrategy,
    "ma_compression": MACompressionBreakoutStrategy,
    "ma120_support": MA120SupportReboundStrategy,
    "profit_lock": ProfitLockAfterHighStrategy,
    "position_risk": PositionRiskManagementStrategy,
    "pullback_failed_exit": PullbackFailedExitStrategy,
    "conditional_trigger": ConditionalTriggerStrategy,
}


def get_execution_strategy(name: str, **kwargs) -> BaseExecutionStrategy:
    """
    根据名称获取策略实例。

    Args:
        name: 策略标识（"A"/"B"/"C" 或全名）
        **kwargs: 传递给策略构造函数的参数

    Returns:
        BaseExecutionStrategy 实例

    Raises:
        ValueError: 未知策略名
    """
    cls = _STRATEGY_REGISTRY.get(name)
    if cls is None:
        available = sorted(set(k for k in _STRATEGY_REGISTRY if len(k) <= 3))
        raise ValueError(f"未知策略 '{name}'，可用选项: {available}")
    return cls(**kwargs)


def get_available_strategies() -> list[str]:
    """返回可用策略标识列表。"""
    return ["A", "B", "C", "D", "E", "F", "G", "H", "O",
            "I", "J", "K", "L", "M", "N", "P", "Q", "R", "S", "T"]


def get_strategy_info() -> dict[str, dict]:
    """返回所有策略的名称和描述。"""
    result = {}
    for key in ("A", "B", "C", "D", "E", "F", "G", "H", "O",
                "I", "J", "K", "L", "M", "N", "P", "Q", "R", "S", "T"):
        s = get_execution_strategy(key)
        result[key] = {"name": s.name, "description": s.description}
    return result
