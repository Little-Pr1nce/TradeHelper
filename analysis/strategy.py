"""
交易策略定义模块

采用策略模式 (Strategy Pattern) 组织交易策略：

  BaseStrategy (抽象基类)
  ├── MACrossoverStrategy        双均线交叉
  ├── MACDStrategy               MACD 金叉死叉
  ├── RSIStrategy                RSI 超买超卖
  ├── BollingerBandsStrategy     布林带均值回归
  ├── BuyAndHoldStrategy         买入持有（基准）
  ├── TripleMACrossoverStrategy  三均线排列
  └── PrecomputedSignalsStrategy 透传已生成的 signal 列（用于避免重复计算）

策略只负责"在 DataFrame 中填 signal 列"，不再重复计算指标——
若 DataFrame 中已包含 dif/dea/rsi/bb_* 等列（由 analysis.technical 计算），
策略会直接复用；否则按需补算。

【扩展点】添加新策略：
  1. 继承 BaseStrategy，实现 generate_signals() / name / description
  2. 在 _STRATEGIES 字典中注册
  3. 可选：在 UI 中添加策略选择项
"""

from abc import ABC, abstractmethod

import pandas as pd
import numpy as np


# ======================== 通用工具 ========================

def _crossover_signal(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """
    向量化检测两条线的金叉/死叉。

    返回与输入等长的字符串 Series：
      "buy"  — fast 上穿 slow（前一刻 <=，当前 >）
      "sell" — fast 下穿 slow（前一刻 >=，当前 <）
      ""     — 无穿越或数据缺失

    比 Python for-loop 快 ~50x，所有交叉类策略共用。
    """
    prev_fast = fast.shift(1)
    prev_slow = slow.shift(1)
    cross_up = (fast > slow) & (prev_fast <= prev_slow)
    cross_dn = (fast < slow) & (prev_fast >= prev_slow)
    return pd.Series(
        np.where(cross_up, "buy", np.where(cross_dn, "sell", "")),
        index=fast.index,
    )


def _ensure_macd(df: pd.DataFrame, fast: int, slow: int, signal: int) -> None:
    """如果 df 已有 dif/dea 列就什么都不做，否则就地补算。"""
    if "dif" in df.columns and "dea" in df.columns:
        return
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["dif"] = ema_fast - ema_slow
    df["dea"] = df["dif"].ewm(span=signal, adjust=False).mean()


def _ensure_rsi(df: pd.DataFrame, period: int) -> None:
    """如果 df 已有 rsi 列就跳过，否则补算。"""
    if "rsi" in df.columns:
        return
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))


def _ensure_bollinger(df: pd.DataFrame, period: int, std_dev: int) -> None:
    """如果 df 已有 bb_upper/bb_lower 列就跳过，否则补算。"""
    if "bb_upper" in df.columns and "bb_lower" in df.columns:
        return
    mid = df["close"].rolling(window=period).mean()
    std = df["close"].rolling(window=period).std()
    df["bb_mid"] = mid
    df["bb_upper"] = mid + std_dev * std
    df["bb_lower"] = mid - std_dev * std


def _ensure_ma(df: pd.DataFrame, period: int) -> None:
    """如果 df 已有对应 ma 列就跳过，否则补算（命名风格与 calc_ma 一致）。"""
    col = f"ma_{period}"
    if col not in df.columns:
        df[col] = df["close"].rolling(window=period).mean()


# ======================== 抽象基类 ========================

class BaseStrategy(ABC):
    """交易策略抽象基类（在 DataFrame 的 signal 列填入 'buy' / 'sell' / ''）。"""

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass


# ======================== 具体策略 ========================

class MACrossoverStrategy(BaseStrategy):
    """双均线交叉：MA{fast} 上穿 MA{slow} 买入，下穿卖出。"""

    def __init__(self, fast_period: int = 5, slow_period: int = 20):
        self.fast_period = fast_period
        self.slow_period = slow_period

    @property
    def name(self) -> str:
        return f"双均线交叉策略 (MA{self.fast_period}/MA{self.slow_period})"

    @property
    def description(self) -> str:
        return (
            f"当 {self.fast_period} 日均线上穿 {self.slow_period} 日均线时产生买入信号（金叉），"
            f"当 {self.fast_period} 日均线下穿 {self.slow_period} 日均线时产生卖出信号（死叉）。"
        )

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        result["signal"] = ""
        if len(result) < self.slow_period:
            return result
        _ensure_ma(result, self.fast_period)
        _ensure_ma(result, self.slow_period)
        result["signal"] = _crossover_signal(
            result[f"ma_{self.fast_period}"],
            result[f"ma_{self.slow_period}"],
        )
        return result


class MACDStrategy(BaseStrategy):
    """MACD 金叉死叉：DIF 上穿 DEA 买入，下穿卖出。"""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal_period = signal

    @property
    def name(self) -> str:
        return f"MACD 策略 (MACD{self.fast}/{self.slow}/{self.signal_period})"

    @property
    def description(self) -> str:
        return "当 DIF 上穿 DEA 时买入，DIF 下穿 DEA 时卖出。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        result["signal"] = ""
        if len(result) < self.slow + self.signal_period:
            return result
        _ensure_macd(result, self.fast, self.slow, self.signal_period)
        result["signal"] = _crossover_signal(result["dif"], result["dea"])
        return result


class RSIStrategy(BaseStrategy):
    """RSI 超买超卖：从超卖区上穿阈值买入，从超买区下穿阈值卖出。"""

    def __init__(self, period: int = 14, oversold: int = 30, overbought: int = 70):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    @property
    def name(self) -> str:
        return f"RSI 超买超卖策略 (周期{self.period}, 超卖<{self.oversold}, 超买>{self.overbought})"

    @property
    def description(self) -> str:
        return (
            f"RSI 低于 {self.oversold} 视为超卖区域，产生买入信号；"
            f"RSI 高于 {self.overbought} 视为超买区域，产生卖出信号。"
        )

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        result["signal"] = ""
        if len(result) < self.period + 1:
            return result
        _ensure_rsi(result, self.period)
        rsi = result["rsi"]
        prev = rsi.shift(1)
        buy = (prev <= self.oversold) & (rsi > self.oversold)
        sell = (prev >= self.overbought) & (rsi < self.overbought)
        result["signal"] = np.where(buy, "buy", np.where(sell, "sell", ""))
        return result


class BollingerBandsStrategy(BaseStrategy):
    """布林带均值回归：跌破下轨买入，突破上轨卖出。"""

    def __init__(self, period: int = 20, std_dev: int = 2):
        self.period = period
        self.std_dev = std_dev

    @property
    def name(self) -> str:
        return f"布林带策略 (周期{self.period}, {self.std_dev}σ)"

    @property
    def description(self) -> str:
        return (
            f"收盘价低于布林带下轨（中轨 - {self.std_dev}σ）时买入，"
            f"收盘价高于布林带上轨（中轨 + {self.std_dev}σ）时卖出。"
            f"基于价格均值回归原理。"
        )

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        result["signal"] = ""
        if len(result) < self.period:
            return result
        _ensure_bollinger(result, self.period, self.std_dev)
        close = result["close"]
        prev_close = close.shift(1)
        lower = result["bb_lower"]
        prev_lower = lower.shift(1)
        upper = result["bb_upper"]
        prev_upper = upper.shift(1)
        # 跌破下轨：前一日 close >= lower，今日 close < lower
        buy = (prev_close >= prev_lower) & (close < lower)
        # 突破上轨：前一日 close <= upper，今日 close > upper
        sell = (prev_close <= prev_upper) & (close > upper)
        result["signal"] = np.where(buy, "buy", np.where(sell, "sell", ""))
        return result


class BuyAndHoldStrategy(BaseStrategy):
    """买入持有：第一天买入，最后一天卖出。"""

    @property
    def name(self) -> str:
        return "买入持有策略 (Buy & Hold)"

    @property
    def description(self) -> str:
        return "回测期初买入，回测期末卖出，中间不做任何操作。作为衡量其他策略是否跑赢大盘的基准。"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        result["signal"] = ""
        if len(result) < 2:
            return result
        result.loc[result.index[0], "signal"] = "buy"
        result.loc[result.index[-1], "signal"] = "sell"
        return result


class TripleMACrossoverStrategy(BaseStrategy):
    """三均线排列：多头排列出现时买入，空头排列出现时卖出（首次状态切换）。"""

    def __init__(self, fast: int = 5, mid: int = 10, slow: int = 20):
        self.fast = fast
        self.mid = mid
        self.slow = slow

    @property
    def name(self) -> str:
        return f"三均线排列策略 (MA{self.fast}/MA{self.mid}/MA{self.slow})"

    @property
    def description(self) -> str:
        return (
            f"当 MA{self.fast} > MA{self.mid} > MA{self.slow} 形成多头排列时买入，"
            f"当 MA{self.fast} < MA{self.mid} < MA{self.slow} 形成空头排列时卖出。"
            f"相比双均线交叉，信号更少但准确率更高。"
        )

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        result["signal"] = ""
        if len(result) < self.slow:
            return result
        _ensure_ma(result, self.fast)
        _ensure_ma(result, self.mid)
        _ensure_ma(result, self.slow)
        f, m, s = result[f"ma_{self.fast}"], result[f"ma_{self.mid}"], result[f"ma_{self.slow}"]
        bull = (f > m) & (m > s)
        bear = (f < m) & (m < s)
        # 状态首次切换才发信号（避免每天连续 buy）
        prev_bull = bull.shift(1, fill_value=False)
        prev_bear = bear.shift(1, fill_value=False)
        buy = bull & (~prev_bull)
        sell = bear & (~prev_bear)
        result["signal"] = np.where(buy, "buy", np.where(sell, "sell", ""))
        return result


class PrecomputedSignalsStrategy(BaseStrategy):
    """
    透传策略：把 DataFrame 中已有的 signal 列原样保留。

    用法：
        bt_df = strategy.generate_signals(df)        # 真正生成信号
        result = engine.run(bt_df, PrecomputedSignalsStrategy(strategy))

    这样 BacktestEngine.run() 内部对 strategy.generate_signals 的调用是 no-op，
    避免对相同输入做两次相同的指标 + 信号计算。
    """

    def __init__(self, wrapped: "BaseStrategy"):
        self._wrapped = wrapped

    @property
    def name(self) -> str:
        return self._wrapped.name

    @property
    def description(self) -> str:
        return self._wrapped.description

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        # 期望调用方已经把 signal 列填好了；如果没有就退化为底层策略
        if "signal" in df.columns:
            return df
        return self._wrapped.generate_signals(df)


# ======================== 策略注册表 ========================
# 【扩展点】在此字典中注册新策略：
#   key:   策略短名（用于 API 和配置引用）
#   value: 策略类（继承自 BaseStrategy）

_STRATEGIES = {
    "ma_crossover": MACrossoverStrategy,
    "macd": MACDStrategy,
    "rsi": RSIStrategy,
    "bollinger": BollingerBandsStrategy,
    "buy_and_hold": BuyAndHoldStrategy,
    "triple_ma": TripleMACrossoverStrategy,
}


def get_strategy(name: str = "ma_crossover", **kwargs) -> BaseStrategy:
    strategy_cls = _STRATEGIES.get(name, MACrossoverStrategy)
    return strategy_cls(**kwargs)


def get_available_strategies() -> dict:
    return dict(_STRATEGIES)
