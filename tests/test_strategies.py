"""
交易策略单元测试。

验证三种策略的开平仓条件、仓位计算、冷却期逻辑
是否符合需求文档规范。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inspect
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


def test_bollinger_strategy_owns_its_no_signal_diagnosis():
    strategy = get_execution_strategy("D")
    df = make_test_df(80, final_scores=list(np.linspace(-0.2, 0.2, 80)))
    df["bb_upper"] = df["close"] + 5.0
    df["bb_mid"] = df["close"]
    df["bb_lower"] = df["close"] - 5.0

    decision = strategy.generate_decision(df, make_context())
    diagnosis = "；".join(decision.missing_conditions)

    assert "布林上轨" in diagnosis
    assert "需突破" in diagnosis


def test_trend_pullback_exit_compares_with_prior_ten_day_low():
    strategy = get_execution_strategy("L")
    rising = list(np.linspace(50.0, 100.0, 69))
    close = rising + [110.0] * 10 + [105.0]
    df = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=80, freq="B"),
        "open": close,
        "high": np.asarray(close) + 1.0,
        "low": np.asarray(close) - 1.0,
        "close": close,
        "volume": [1000000] * 80,
        "Final_Score": [0.0] * 80,
    })
    context = make_context(
        shares=100, entry_price=100.0, highest_close=110.0,
    )

    orders = strategy.generate_orders(df, context)

    assert orders and orders[0].action == "sell"
    assert "跌破10日低点" in orders[0].reason


class TestThresholdTrendStrategy:
    """策略A 测试。"""

    def setup_method(self):
        self.strategy = get_execution_strategy("A")

    def test_entry_signal(self):
        """Final_Score 处于滚动高分位应产生买入信号。"""
        scores = list(np.linspace(-0.5, 0.5, 100))
        df = make_test_df(100, final_scores=scores)
        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 1
        assert orders[0].action == "buy"
        assert orders[0].shares >= 100

    def test_no_entry_below_threshold(self):
        """Final_Score 低于入场分位不应产生买入信号。"""
        scores = list(np.linspace(-0.5, 0.5, 99)) + [-0.4]
        df = make_test_df(100, final_scores=scores)
        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0

    def test_exit_signal(self):
        """持仓中 Final_Score 跌破退出分位应产生卖出信号。"""
        scores = list(np.linspace(-0.5, 0.5, 99)) + [-0.5]
        df = make_test_df(100, final_scores=scores)
        ctx = make_context(shares=500, entry_price=100.0, highest_close=105.0)
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 1
        assert orders[0].action == "sell"

    def test_no_exit_above_threshold(self):
        """Final_Score 未跌破退出分位时持仓中不应平仓。"""
        scores = list(np.linspace(-0.5, 0.5, 100))
        df = make_test_df(100, final_scores=scores)
        ctx = make_context(shares=500, entry_price=100.0)
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0

    def test_cooldown(self):
        """冷却期内不产生买入信号。"""
        scores = list(np.linspace(-0.5, 0.5, 100))
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
        """Final_Score 未处于低分位不开仓。"""
        df = make_test_df(100, final_scores=list(np.linspace(-0.5, 0.5, 100)))
        ctx = make_context()
        orders = self.strategy.generate_orders(df, ctx)
        assert len(orders) == 0

    def test_exit_on_score_reversal(self):
        """Final_Score 回升至高分位平仓。"""
        scores = list(np.linspace(-0.5, 0.5, 100))
        df = make_test_df(100, final_scores=scores)
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
            "FinBERT_Score": [0.3] * (n - 1) + [0.1],
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
        n = 80
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        # 先涨后大跌触发移动止盈
        close = [100 + i for i in range(50)] + [150 - i * 3 for i in range(30)]
        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": [c + 5 for c in close],
            "low": [c - 5 for c in close],
            "close": close,
            "volume": [10000000] * n,
            "Final_Score": list(np.linspace(-0.2, 0.2, n)),
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


def _run_script_tests():
    """Tiny runner for environments without pytest."""
    current_module = sys.modules[__name__]
    total = 0
    failures = []

    for name, obj in vars(current_module).items():
        if callable(obj) and name.startswith("test_"):
            total += 1
            try:
                obj()
            except Exception as exc:
                failures.append((name, exc))

        if inspect.isclass(obj) and name.startswith("Test"):
            for method_name, _ in inspect.getmembers(obj, inspect.isfunction):
                if not method_name.startswith("test_"):
                    continue
                total += 1
                inst = obj()
                if hasattr(inst, "setup_method"):
                    inst.setup_method()
                try:
                    getattr(inst, method_name)()
                except Exception as exc:
                    failures.append((f"{name}.{method_name}", exc))

    if failures:
        print(f"{len(failures)}/{total} failed")
        for test_name, exc in failures:
            print(f"{test_name}: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
    print(f"{total}/{total} passed")


if __name__ == "__main__":
    _run_script_tests()
