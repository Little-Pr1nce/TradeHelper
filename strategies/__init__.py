"""
交易执行策略库。

提供三种策略的注册与工厂方法：
  - "A" / "threshold_trend" → ThresholdTrendStrategy
  - "B" / "mean_reversion"  → MeanReversionStrategy
  - "C" / "momentum_news"   → MomentumNewsStrategy

使用方式：
    from strategies import get_execution_strategy
    strategy = get_execution_strategy("A")
    orders = strategy.generate_orders(df, context)
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

# 策略注册表
_STRATEGY_REGISTRY: dict[str, type[BaseExecutionStrategy]] = {
    "A": ThresholdTrendStrategy,
    "B": MeanReversionStrategy,
    "C": MomentumNewsStrategy,
    "D": BollingerBreakoutStrategy,
    "E": DualThrustStrategy,
    "F": TurtleATRStrategy,
    # 别名
    "threshold_trend": ThresholdTrendStrategy,
    "mean_reversion": MeanReversionStrategy,
    "momentum_news": MomentumNewsStrategy,
    "bollinger_breakout": BollingerBreakoutStrategy,
    "dual_thrust": DualThrustStrategy,
    "turtle_atr": TurtleATRStrategy,
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
    return ["A", "B", "C", "D", "E", "F"]


def get_strategy_info() -> dict[str, dict]:
    """返回所有策略的名称和描述。"""
    result = {}
    for key in ("A", "B", "C", "D", "E", "F"):
        s = get_execution_strategy(key)
        result[key] = {"name": s.name, "description": s.description}
    return result
