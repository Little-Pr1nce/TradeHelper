"""
交易策略单元测试。

验证三种策略的开平仓条件、仓位计算、冷却期逻辑
是否符合需求文档规范。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np

from strategies.base import StrategyContext, Position
from strategies import get_execution_strategy


def make_test_df(n: int = 200, final_scores: list[float] | None = None,
                 finbert_scores: list[float] | None = None) -> pd.DataFrame:
    """构造测试用 DataFrame。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 100 + np.cumsum(np.random.randn(n) * 1.5)

    df = pd.DataFrame({
        "date": dates,
        "open": close + np.random.randn(n) * 0.3,
        "high": close + np.abs(np.random.randn(n) * 1),
        "low": close - np.abs(np.random.randn(n) * 1),
        "close": close,
        "volume": np.random.randint(5000000, 20000000, n),
    })

    if final_scores:
        df["Final_Score"] = final_scores[:n]
    else:
        df["Final_Score"] = np.random.randn(n) * 0.3

    if finbert_scores:
        df["FinBERT_Score"] = finbert_scores[:n]

    return df


def make_context(equity=100000.0, cash=100000.0, shares=0, cooldown=-1,
                 holding_days=0, entry_price=0.0, highest_close=0.0) -> StrategyContext:
    pos = Position(
        shares=shares,
        avg_cost=entry_price,
        entry_date="2024-01-01" if shares > 0 else "",
        entry_price=entry_price,
        highest_close=max(highest_close, entry_price),
        stop_loss=entry_price * 0.92 if shares > 0 else 0.0,
        added_position=False,
    )
    return StrategyContext(
        date="2024-06-15", equity=equity, cash=cash, position=pos,
        cooldown_until=cooldown, holding_days=holding_days,
    )


class TestThresholdTrendStrategy:
    """策略A 测试。"""

    def setup_method(self):
        self.strategy = get_execution_strategy("A")

    def test_entry_signal(self):
        """Final_Score > 0.6 应产生买入信号。"""
        scores = [0.0] * 99 + [0.65]
        df = make_test_df(100, final_scores=scores)
        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 1
        assert orders[0].action == "buy"
        assert orders[0].shares >= 100

    def test_no_entry_below_threshold(self):
        """Final_Score <= 0.6 不应产生买入信号。"""
        scores = [0.3] * 50 + [0.55] + [0.4] * 49
        df = make_test_df(100, final_scores=scores)
        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0

    def test_exit_signal(self):
        """持仓中 Final_Score < 0.3 应产生卖出信号。"""
        scores = [0.7] + [0.4] * 48 + [0.25]
        df = make_test_df(50, final_scores=scores)
        ctx = make_context(shares=500, entry_price=100.0, highest_close=105.0)
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 1
        assert orders[0].action == "sell"

    def test_no_exit_above_threshold(self):
        """Final_Score >= 0.3 持仓中不应平仓。"""
        scores = [0.7] + [0.5] * 48 + [0.35]
        df = make_test_df(50, final_scores=scores)
        ctx = make_context(shares=500, entry_price=100.0)
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0

    def test_cooldown(self):
        """冷却期内不产生买入信号。"""
        scores = [0.0] * 99 + [0.65]
        df = make_test_df(100, final_scores=scores)
        # cooldown_until=200，当前 idx=99，冷却期中
        ctx = make_context(cooldown=200)
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0


class TestMeanReversionStrategy:
    """策略B 测试。"""

    def setup_method(self):
        self.strategy = get_execution_strategy("B")

    def test_entry_requires_low_score_and_low_vol(self):
        """开仓需要 Final_Score < -0.5 且低波环境。"""
        # 构造明显的低波环境：前段正常波动，后段极低波动
        np.random.seed(1)
        n = 200
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        # 前 180 天：正常波动
        close1 = 100 + np.cumsum(np.random.randn(180) * 1.0)
        # 后 20 天：极低波动（微小变动，避免 vol=0 被策略拒绝）
        tiny_changes = np.random.randn(20) * 0.01
        close2 = close1[-1] + np.cumsum(tiny_changes)
        close = np.concatenate([close1, close2])
        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": [5000000] * n,
            "Final_Score": [-0.55] * n,
        })

        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) >= 1, "低波环境应触发买入"

    def test_no_entry_when_score_high(self):
        """Final_Score >= -0.5 不开仓。"""
        df = make_test_df(100, final_scores=[-0.3] * 100)
        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0

    def test_exit_on_score_reversal(self):
        """Final_Score > 0.2 平仓。"""
        scores = [-0.6] + [-0.4] * 48 + [0.3]
        df = make_test_df(50, final_scores=scores)
        ctx = make_context(shares=500, entry_price=95.0, highest_close=100.0)
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 1
        assert orders[0].action == "sell"

    def test_cooldown(self):
        """冷却期 5 根 K 线。"""
        df = make_test_df(200, final_scores=[-0.55] * 200)
        ctx = make_context(cooldown=200)
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0


class TestMomentumNewsStrategy:
    """策略C 测试。"""

    def setup_method(self):
        self.strategy = get_execution_strategy("C")

    def test_entry_requires_triple_confirmation(self):
        """三重确认缺一不可。"""
        n = 100
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        close = list(range(100, 100 + n))
        # 最后一天突破 20 日最高
        close[-1] = 200

        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": [c + 2 for c in close],
            "low": [c - 2 for c in close],
            "close": close,
            "volume": [10000000] * n,
            "Final_Score": [0.5] * (n - 1) + [0.75],
            "FinBERT_Score": [0.5] * (n - 1) + [0.85],
        })

        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 1
        assert orders[0].action == "buy"

    def test_no_entry_without_finbert(self):
        """FinBERT 不足不开仓。"""
        n = 100
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        close = list(range(100, 100 + n))
        close[-1] = 200

        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": [c + 2 for c in close],
            "low": [c - 2 for c in close],
            "close": close,
            "volume": [10000000] * n,
            "Final_Score": [0.5] * (n - 1) + [0.75],
            "FinBERT_Score": [0.3] * (n - 1) + [0.5],  # 不足 0.8
        })

        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0

    def test_no_entry_without_breakout(self):
        """价格未突破不开仓。"""
        df = make_test_df(100, final_scores=[0.5] * 99 + [0.75],
                          finbert_scores=[0.5] * 99 + [0.85])
        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0  # 随机价格不太可能刚好突破

    def test_exit_on_trailing_stop(self):
        """移动止盈触发平仓。"""
        n = 50
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        # 先涨后大跌触发移动止盈
        close = [100 + i for i in range(30)] + [130 - i * 3 for i in range(20)]
        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": [c + 5 for c in close],
            "low": [c - 5 for c in close],
            "close": close,
            "volume": [10000000] * n,
            "Final_Score": [0.5] * n,
        })

        # 持仓 entry_price=101, highest_close=129
        ctx = make_context(shares=500, entry_price=101.0, highest_close=129.0)
        orders = self.strategy.generate_orders(df, ctx)
        # 最后收盘价是 130-57=73，远低于移动止盈线
        assert len(orders) == 1
        assert orders[0].action == "sell"
        assert "移动止盈" in orders[0].reason

    def test_cooldown(self):
        """冷却期 2 根 K 线。"""
        df = make_test_df(100, final_scores=[0.75] * 100,
                          finbert_scores=[0.85] * 100)
        ctx = make_context(cooldown=200)
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0


class TestHardRiskControls:
    """通用硬风控测试。"""

    def test_hard_stop_in_broker(self):
        """硬止损由 Broker 层执行（参见 test_backtest.py）。"""
        pass  # 硬止损逻辑在 backtest/broker.py 中，此处仅占位

    def test_time_stop_in_broker(self):
        """时间止损由 Broker 层执行。"""
        pass
