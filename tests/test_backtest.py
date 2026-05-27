"""
回测引擎单元测试。

验证 T+1 撮合时序、滑点计算、涨跌停过滤
以及端到端无未来函数约束。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import math
import pandas as pd
import numpy as np

from strategies.base import Order, Position, StrategyContext
from strategies import get_execution_strategy
from backtest.broker import Broker, BrokerConfig, Account
from backtest.engine import BacktestEngine, BacktestConfig


def is_close(a, b, rel=0.01):
    """简易 pytest.approx 替代。"""
    return abs(a - b) <= rel * max(abs(a), abs(b), 1.0)


def assert_raises(exc_type, match=None):
    """简易 pytest.raises 替代。"""
    class _Ctx:
        def __enter__(self):
            return self
        def __exit__(self, exc, val, tb):
            if exc is None:
                raise AssertionError(f"期望抛出 {exc_type.__name__} 但未抛出")
            if not issubclass(exc, exc_type):
                return False
            if match and match not in str(val):
                raise AssertionError(f"异常信息 '{val}' 不匹配 '{match}'")
            return True
    return _Ctx()


def make_ohlcv_df(n: int = 100) -> pd.DataFrame:
    """构造简单 OHLCV DataFrame。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 100 + np.cumsum(np.random.randn(n) * 1.0)
    return pd.DataFrame({
        "date": dates,
        "open": close + np.random.randn(n) * 0.3,
        "high": close + np.abs(np.random.randn(n) * 1.5),
        "low": close - np.abs(np.random.randn(n) * 1.5),
        "close": close,
        "volume": np.random.randint(5000000, 20000000, n),
    })


class TestBroker:
    """撮合模拟器测试。"""

    def setup_method(self):
        self.broker = Broker(BrokerConfig())
        self.account = Account(initial_capital=100000.0)

    def test_buy_execution(self):
        """基本买入执行。"""
        bar = pd.Series({
            "date": "2024-01-02", "open": 100.0, "high": 102.0,
            "low": 99.0, "close": 101.0, "volume": 10000000,
        })
        order = Order(date="2024-01-01", action="buy", shares=500,
                      stop_loss=95.0, reason="test")
        fill = self.broker.execute_buy(order, bar, self.account, prev_close=99.0)

        assert fill is not None
        assert fill.action == "buy"
        assert fill.shares == 500
        # 成交价 = open × (1 + slippage) = 100 × 1.003 = 100.3
        assert is_close(fill.price, 100.3, rel=0.01)
        assert self.account.position is not None
        assert self.account.position.shares == 500

    def test_sell_execution(self):
        """基本卖出执行。"""
        self.account.position = Position(
            shares=500, avg_cost=100.0, entry_date="2024-01-02",
            entry_price=100.0, highest_close=101.0, stop_loss=92.0,
        )
        bar = pd.Series({
            "date": "2024-01-03", "open": 101.0, "high": 103.0,
            "low": 100.0, "close": 102.0, "volume": 10000000,
        })
        order = Order(date="2024-01-02", action="sell", shares=500, reason="test")
        fill = self.broker.execute_sell(order, bar, self.account, prev_close=100.0)

        assert fill is not None
        assert fill.action == "sell"
        assert self.account.position is None
        # 卖出现金应增加
        assert self.account.cash > 100000.0

    def test_limit_up_reject(self):
        """涨停时买入订单被拒绝。"""
        bar = pd.Series({
            "date": "2024-01-02", "open": 110.0, "high": 110.0,
            "low": 110.0, "close": 110.0, "volume": 10000000,
        })
        order = Order(date="2024-01-01", action="buy", shares=500, stop_loss=100.0)
        fill = self.broker.execute_buy(order, bar, self.account, prev_close=100.0)
        assert fill is None  # 涨停板拒绝

    def test_limit_down_reject(self):
        """跌停时卖出订单被拒绝。"""
        self.account.position = Position(
            shares=500, avg_cost=100.0, entry_date="2024-01-02",
            entry_price=100.0, highest_close=100.0, stop_loss=92.0,
        )
        bar = pd.Series({
            "date": "2024-01-03", "open": 90.0, "high": 90.5,
            "low": 90.0, "close": 90.0, "volume": 10000000,
        })
        order = Order(date="2024-01-02", action="sell", shares=500)
        fill = self.broker.execute_sell(order, bar, self.account, prev_close=100.0)
        assert fill is None  # 跌停板拒绝

    def test_hard_stop_intraday(self):
        """硬止损：-8% 触发。"""
        self.account.position = Position(
            shares=500, avg_cost=100.0, entry_date="2024-01-02",
            entry_price=100.0, highest_close=100.0, stop_loss=92.0,
        )
        bar = pd.Series({
            "date": "2024-01-03", "open": 95.0, "high": 96.0,
            "low": 91.0, "close": 93.0, "volume": 10000000,
        })
        fills = self.broker.check_intraday_stops(bar, self.account, prev_close=100.0,
                                                  current_date="2024-01-03")
        assert len(fills) == 1
        assert fills[0].action == "sell"
        assert "硬止损" in fills[0].reason
        assert self.account.position is None

    def test_slippage_calculation(self):
        """验证滑点计算。"""
        bar = pd.Series({
            "date": "2024-01-02", "open": 100.0, "high": 102.0,
            "low": 99.0, "close": 101.0, "volume": 10000000,
        })
        order = Order(date="2024-01-01", action="buy", shares=1000, stop_loss=95.0)
        fill = self.broker.execute_buy(order, bar, self.account, prev_close=99.0)

        # fill_price = 100 * 1.003 = 100.3
        # slippage_cost = 100.3 * 1000 - 100 * 1000 = 300
        expected_slippage = 100.0 * 0.003 * 1000
        assert is_close(fill.slippage_cost, expected_slippage, rel=0.01)


class TestBacktestEngine:
    """回测引擎端到端测试。"""

    def test_t_plus_one_execution(self):
        """验证 T 日信号 → T+1 日成交的时序。"""
        n = 100
        df = make_ohlcv_df(n)

        # 在 T=50 设置强买入信号
        scores = [0.0] * n
        scores[50] = 0.65  # T=50 触发买入
        scores[80] = 0.25  # T=80 触发平仓
        df["Final_Score"] = scores

        engine = BacktestEngine(BacktestConfig(initial_capital=100000.0))
        strategy = get_execution_strategy("A",
                                          entry_threshold=0.6, exit_threshold=0.3)

        result = engine.run(df, strategy)

        assert result.total_trades >= 1

        if result.total_trades >= 1:
            first_trade = result.trades[0]
            # 信号 T=50 (2024-03-14), 成交 T+1=51 (2024-03-15)
            signal_date = first_trade["signal_date"]
            entry_date = first_trade["entry_date"]
            assert entry_date > signal_date, \
                f"成交日 {entry_date} 应该晚于信号日 {signal_date}"

    def test_no_look_ahead_bias(self):
        """验证回测不使用未来数据。"""
        n = 100
        df = make_ohlcv_df(n)

        # 所有信号设为中性，不应产生任何交易
        df["Final_Score"] = [0.4] * n

        engine = BacktestEngine(BacktestConfig(initial_capital=100000.0))
        # 使用高阈值确保不触发
        strategy = get_execution_strategy("A",
                                          entry_threshold=0.6, exit_threshold=0.3)
        result = engine.run(df, strategy)

        assert result.total_trades == 0, \
            "中性 Final_Score 不应产生交易（可能使用了未来数据）"

    def test_end_to_end_buy_and_sell(self):
        """端到端：买入→持有→卖出。"""
        n = 80
        np.random.seed(0)
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        close = [100.0]
        for _ in range(n - 1):
            close.append(close[-1] + np.random.randn() * 1.5)

        df = pd.DataFrame({
            "date": dates,
            "open": [c + np.random.randn() * 0.3 for c in close],
            "high": [c + abs(np.random.randn()) * 2 for c in close],
            "low": [c - abs(np.random.randn()) * 2 for c in close],
            "close": close,
            "volume": [10000000] * n,
        })

        scores = [0.0] * n
        scores[30] = 0.7   # 买入
        scores[60] = 0.2   # 卖出
        df["Final_Score"] = scores

        engine = BacktestEngine(BacktestConfig(initial_capital=100000.0))
        strategy = get_execution_strategy("A",
                                          entry_threshold=0.6, exit_threshold=0.3,
                                          cooldown_bars=0)  # 无冷却期便于测试
        result = engine.run(df, strategy)

        # 应有买入和卖出
        assert result.total_trades >= 1
        assert len(result.equity_curve) > 0

    def test_invalid_final_score(self):
        """缺少 Final_Score 列应抛出异常。"""
        df = make_ohlcv_df(30)
        engine = BacktestEngine()
        strategy = get_execution_strategy("A")

        with assert_raises(ValueError, match="Final_Score"):
            engine.run(df, strategy)


class TestMultiStrategyRun:
    """多策略并行测试。"""

    def test_run_all_strategies(self):
        """运行全部三种策略不崩溃。"""
        n = 100
        df = make_ohlcv_df(n)
        scores = [0.0] * n
        scores[30] = 0.7
        scores[60] = 0.2
        df["Final_Score"] = scores
        df["FinBERT_Score"] = [0.0] * n

        engine = BacktestEngine(BacktestConfig(initial_capital=100000.0))

        strategies = [
            get_execution_strategy("A", cooldown_bars=0),
            get_execution_strategy("B", cooldown_bars=0),
            get_execution_strategy("C", cooldown_bars=0),
        ]
        results = engine.run_multi(df, strategies)

        assert len(results) == 3
        for name, result in results.items():
            assert result.initial_capital == 100000.0
            assert len(result.equity_curve) > 0

    def test_empty_data(self):
        """空数据返回空结果。"""
        engine = BacktestEngine()
        strategy = get_execution_strategy("A")
        result = engine.run(pd.DataFrame(), strategy)
        assert result.total_trades == 0
        assert result.total_return == 0.0
