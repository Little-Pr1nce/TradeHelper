"""
交易策略定义模块

采用策略模式 (Strategy Pattern) 组织交易策略：

  BaseStrategy (抽象基类)
  ├── MACrossoverStrategy   双均线交叉策略（默认演示策略）
  │   规则：MA5 上穿 MA20 → 买入，下穿 → 卖出
  └── MACDStrategy          MACD 金叉死叉策略（备用演示策略）
      规则：DIF 上穿 DEA → 买入，下穿 → 卖出

策略注册机制：
  通过 _STRATEGIES 字典注册所有可用策略，
  使用 get_strategy(name) 工厂函数按名称创建实例。

【扩展点】添加新的交易策略：
  1. 继承 BaseStrategy，实现 generate_signals() 方法
     规则：在 DataFrame 的 "signal" 列填入 "buy" 或 "sell"
  2. 实现 name 和 description 属性
  3. 在 _STRATEGIES 字典中注册（key 为策略短名，value 为类引用）
  4. 可选：在 UI 中添加策略选择下拉框

示例新增策略（RSI 策略）：
  class RSIStrategy(BaseStrategy):
      def generate_signals(self, df):
          result = df.copy()
          result["signal"] = ""
          result["rsi"] = calc_rsi(result)["rsi"]
          # RSI < 30 买入，RSI > 70 卖出
          ...
          return result
"""

from abc import ABC, abstractmethod

import pandas as pd
import numpy as np


class BaseStrategy(ABC):
    """
    交易策略抽象基类。

    所有策略必须实现以下三个接口：
      - generate_signals(df):  输入股价 DataFrame，输出带 signal 列的 DataFrame
      - name:                   策略名称（用于报告展示）
      - description:           策略描述（解释买卖信号逻辑）

    signal 列约定：
      - "buy":  买入信号
      - "sell": 卖出信号
      - "":     无信号（持有/观望）
    """

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        根据策略规则生成买卖信号。

        Args:
            df: 包含至少 close 列的股价 DataFrame

        Returns:
            添加了 signal 列的 DataFrame
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名称，用于报告标题和日志。"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """策略描述，解释信号生成逻辑。"""
        pass


class MACrossoverStrategy(BaseStrategy):
    """
    双均线交叉策略（默认演示策略）。

    信号逻辑：
      - 金叉：短期均线从下方上穿长期均线 → 买入信号
      - 死叉：短期均线从上方下穿长期均线 → 卖出信号

    参数：
      - fast_period: 短期均线周期（默认 5 日，代表周线）
      - slow_period: 长期均线周期（默认 20 日，代表月线）

    优缺点：
      优点：简单直观，在趋势市场中表现良好
      缺点：震荡市场中容易频繁产生假信号（"来回被抽"）

    适用场景：趋势明显的单边市场行情。
    """

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
            return result  # 数据不足，无法生成信号

        # 计算快慢均线
        result["ma_fast"] = result["close"].rolling(window=self.fast_period).mean()
        result["ma_slow"] = result["close"].rolling(window=self.slow_period).mean()

        # 检测穿越信号
        for i in range(self.slow_period, len(result)):
            # 金叉：前一刻快线 <= 慢线，当前快线 > 慢线
            if (result["ma_fast"].iloc[i] > result["ma_slow"].iloc[i] and
                    result["ma_fast"].iloc[i-1] <= result["ma_slow"].iloc[i-1]):
                if pd.notna(result["ma_fast"].iloc[i-1]):
                    result.loc[result.index[i], "signal"] = "buy"
            # 死叉：前一刻快线 >= 慢线，当前快线 < 慢线
            elif (result["ma_fast"].iloc[i] < result["ma_slow"].iloc[i] and
                  result["ma_fast"].iloc[i-1] >= result["ma_slow"].iloc[i-1]):
                if pd.notna(result["ma_fast"].iloc[i-1]):
                    result.loc[result.index[i], "signal"] = "sell"

        return result


class MACDStrategy(BaseStrategy):
    """
    MACD 金叉死叉策略。

    信号逻辑：
      - 金叉：DIF 从下方上穿 DEA → 买入信号
      - 死叉：DIF 从上方下穿 DEA → 卖出信号

    参数：
      - fast:  快线周期（默认 12）
      - slow:  慢线周期（默认 26）
      - signal_period: 信号线周期（默认 9）

    与双均线策略的区别：
      MACD 使用 EMA 而非 SMA，对近期价格更敏感；
      通过 DEA 信号线过滤了部分噪声，假信号相对较少。
    """

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
            return result  # 数据不足

        # 计算 MACD
        result["ema_fast"] = result["close"].ewm(span=self.fast, adjust=False).mean()
        result["ema_slow"] = result["close"].ewm(span=self.slow, adjust=False).mean()
        result["dif"] = result["ema_fast"] - result["ema_slow"]
        result["dea"] = result["dif"].ewm(span=self.signal_period, adjust=False).mean()

        # 检测 DIF/DEA 穿越信号
        for i in range(1, len(result)):
            if pd.isna(result["dif"].iloc[i]) or pd.isna(result["dea"].iloc[i]):
                continue
            if (result["dif"].iloc[i] > result["dea"].iloc[i] and
                    result["dif"].iloc[i-1] <= result["dea"].iloc[i-1]):
                result.loc[result.index[i], "signal"] = "buy"
            elif (result["dif"].iloc[i] < result["dea"].iloc[i] and
                  result["dif"].iloc[i-1] >= result["dea"].iloc[i-1]):
                result.loc[result.index[i], "signal"] = "sell"

        return result


class RSIStrategy(BaseStrategy):
    """
    RSI 超买超卖策略。

    信号逻辑：
      - RSI 从超卖区域回升 → 买入信号
      - RSI 从超买区域回落 → 卖出信号

    参数：
      - period: RSI 计算周期（默认 14 日）
      - oversold: 超卖阈值（默认 30）
      - overbought: 超买阈值（默认 70）

    适用场景：震荡市场中的波段操作。
    """

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

        delta = result["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / self.period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / self.period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        result["rsi"] = 100 - (100 / (1 + rs))

        for i in range(1, len(result)):
            rsi_prev = result["rsi"].iloc[i - 1]
            rsi_curr = result["rsi"].iloc[i]
            if pd.isna(rsi_prev) or pd.isna(rsi_curr):
                continue
            if rsi_prev <= self.oversold < rsi_curr:
                result.loc[result.index[i], "signal"] = "buy"
            elif rsi_prev >= self.overbought > rsi_curr:
                result.loc[result.index[i], "signal"] = "sell"

        return result


class BollingerBandsStrategy(BaseStrategy):
    """
    布林带均值回归策略。

    信号逻辑：
      - 收盘价跌破下轨 → 超卖，产生买入信号
      - 收盘价突破上轨 → 超买，产生卖出信号

    参数：
      - period: 均值和标准差计算周期（默认 20 日）
      - std_dev: 标准差倍数（默认 2）

    适用场景：震荡市场中价格有回归中轨倾向时有效。
    """

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

        result["bb_mid"] = result["close"].rolling(window=self.period).mean()
        result["bb_std"] = result["close"].rolling(window=self.period).std()
        result["bb_upper"] = result["bb_mid"] + self.std_dev * result["bb_std"]
        result["bb_lower"] = result["bb_mid"] - self.std_dev * result["bb_std"]

        for i in range(self.period, len(result)):
            close_prev = result["close"].iloc[i - 1]
            close_curr = result["close"].iloc[i]
            lower_prev = result["bb_lower"].iloc[i - 1]
            lower_curr = result["bb_lower"].iloc[i]
            upper_prev = result["bb_upper"].iloc[i - 1]
            upper_curr = result["bb_upper"].iloc[i]

            if pd.isna(lower_prev) or pd.isna(upper_prev):
                continue

            if close_prev >= lower_prev and close_curr < lower_curr:
                result.loc[result.index[i], "signal"] = "buy"
            elif close_prev <= upper_prev and close_curr > upper_curr:
                result.loc[result.index[i], "signal"] = "sell"

        return result


class BuyAndHoldStrategy(BaseStrategy):
    """
    买入持有策略（基准对照策略）。

    信号逻辑：
      - 第一天买入
      - 最后一天卖出
      - 中间不产生任何信号

    用途：作为其他策略的对比基准（benchmark），
    衡量主动策略是否跑赢了简单持有不动。

    参数：无（策略参数固定）。
    """

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
    """
    三均线排列策略。

    信号逻辑：
      - MA5 > MA10 > MA20 且三者多头排列 → 买入信号
      - MA5 < MA10 < MA20 且三者空头排列 → 卖出信号

    与双均线交叉的区别：
      三均线要求三条线严格排列，过滤掉震荡行情中的假信号，
      信号更少但可靠性更高。

    参数：
      - fast: 快线周期（默认 5）
      - mid:  中线周期（默认 10）
      - slow: 慢线周期（默认 20）
    """

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

        result["ma_fast"] = result["close"].rolling(window=self.fast).mean()
        result["ma_mid"] = result["close"].rolling(window=self.mid).mean()
        result["ma_slow"] = result["close"].rolling(window=self.slow).mean()

        prev_signal = ""
        for i in range(self.slow, len(result)):
            if pd.isna(result["ma_fast"].iloc[i]):
                continue

            fast = result["ma_fast"].iloc[i]
            mid = result["ma_mid"].iloc[i]
            slow = result["ma_slow"].iloc[i]

            if fast > mid > slow:
                if prev_signal != "buy":
                    result.loc[result.index[i], "signal"] = "buy"
                    prev_signal = "buy"
            elif fast < mid < slow:
                if prev_signal != "sell":
                    result.loc[result.index[i], "signal"] = "sell"
                    prev_signal = "sell"

        return result


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


# ======================== 工厂函数 ========================

def get_strategy(name: str = "ma_crossover", **kwargs) -> BaseStrategy:
    """
    根据策略名称创建策略实例。

    Args:
        name: 策略短名（如 "ma_crossover", "macd"）
        **kwargs: 传递给策略构造函数的参数

    Returns:
        策略实例，未找到时默认返回 MACrossoverStrategy
    """
    strategy_cls = _STRATEGIES.get(name, MACrossoverStrategy)
    return strategy_cls(**kwargs)


def get_available_strategies() -> dict:
    """
    获取所有已注册策略的字典。

    可用于 UI 策略选择下拉框的选项填充。

    Returns:
        {策略短名: 策略类} 的字典
    """
    return dict(_STRATEGIES)
