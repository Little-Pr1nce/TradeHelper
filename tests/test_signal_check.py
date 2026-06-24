"""
信号检查与操作方案测试。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core.signal_check import (
    SignalResult,
    check_signals,
    generate_operation_plan,
)
from strategies.base import BaseExecutionStrategy, Order, Position


class FixedBuyStrategy(BaseExecutionStrategy):
    suitable_regimes = []

    @property
    def name(self):
        return "固定买入测试策略"

    @property
    def description(self):
        return "用于测试信号检查"

    def generate_orders(self, df, context):
        return [
            Order(
                date=context.date,
                action="buy",
                shares=250,
                stop_loss=92.0,
                reason="测试订单: 250股, 止损92",
            )
        ]


class Variant:
    base_key = "T"
    variant_label = "T_test"
    strategy = FixedBuyStrategy()
    params = {}


class FixedSellStrategy(BaseExecutionStrategy):
    suitable_regimes = []

    @property
    def name(self):
        return "固定卖出测试策略"

    @property
    def description(self):
        return "用于测试持仓退出信号"

    def generate_orders(self, df, context):
        if context.position.shares <= 0:
            return []
        return [
            Order(
                date=context.date,
                action="sell",
                shares=context.position.shares,
                reason="测试退出: 持仓风险过高",
            )
        ]


class SellVariant:
    base_key = "S"
    variant_label = "S_test"
    strategy = FixedSellStrategy()
    params = {}


def _df():
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=30, freq="B"),
        "open": [100.0] * 30,
        "high": [102.0] * 30,
        "low": [98.0] * 30,
        "close": [100.0] * 30,
        "volume": [1000000] * 30,
        "Final_Score": [0.2] * 30,
    })


def test_check_signals_position_pct_comes_from_order_shares():
    signals = check_signals(_df(), [Variant()], "US", initial_capital=100000.0)

    assert len(signals) == 1
    assert signals[0].signal == "buy"
    assert signals[0].position_pct == 0.25


def test_check_signals_can_use_real_account_equity():
    signals = check_signals(
        _df(), [Variant()], "US",
        initial_capital=100000.0,
        account_equity=50000.0,
    )

    assert len(signals) == 1
    assert signals[0].signal == "buy"
    assert signals[0].position_pct == 0.50


def test_check_signals_uses_current_price_for_sizing():
    signals = check_signals(
        _df(), [Variant()], "US",
        account_equity=100000.0,
        current_price=120.0,
    )

    assert len(signals) == 1
    assert signals[0].entry_price == 120.0
    assert signals[0].position_pct == 0.30


def test_check_signals_uses_current_position_for_sell_signal():
    signals = check_signals(
        _df(), [SellVariant()], "US",
        account_equity=100000.0,
        current_position=Position(shares=300, avg_cost=90.0, entry_price=90.0),
    )

    assert len(signals) == 1
    assert signals[0].signal == "sell"
    assert signals[0].position_pct == 0.30

    plan = generate_operation_plan(signals, current_price=100.0)
    assert "持仓退出/减仓信号" in plan.markdown
    assert "测试退出" in plan.markdown


def test_operation_plan_reports_real_loss_pct_and_bearish_caps():
    signal = SignalResult(
        variant_label="T_test",
        strategy_name="固定买入测试策略",
        base_key="T",
        signal="buy",
        entry_price=100.0,
        stop_loss=92.0,
        position_pct=0.80,
        reason="测试订单",
        audit_verdict="PASS",
        test_sharpe=1.2,
        rank_score=80,
    )

    plan = generate_operation_plan([signal], current_price=100.0, market_bias="bearish", df=_df())

    assert plan.conservative["position_pct"] == 0.25
    assert plan.aggressive["position_pct"] == 0.30
    assert "单价风险 8.0%" in plan.markdown
    assert "账户风险" in plan.markdown
    assert "最大亏损" in plan.markdown
    assert "失效条件" in plan.markdown
    assert "信号强度" in plan.markdown
    assert "**2.00%** 净值" in plan.markdown
    assert "92.0% 风控" not in plan.markdown


if __name__ == "__main__":
    test_check_signals_position_pct_comes_from_order_shares()
    test_check_signals_can_use_real_account_equity()
    test_check_signals_uses_current_price_for_sizing()
    test_check_signals_uses_current_position_for_sell_signal()
    test_operation_plan_reports_real_loss_pct_and_bearish_caps()
    print("5/5 passed")
