"""
信号检查与操作方案测试。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core.signal_check import (
    SignalResult,
    _apply_current_price_snapshot,
    check_signals,
    generate_operation_plan,
    run_signal_check,
)
from core.data_quality import evaluate_data_quality
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


def test_realtime_snapshot_appends_bar_and_recomputes_indicators():
    source = _df()
    source["Tech_Normalized_Score"] = 0.0
    snapshot = _apply_current_price_snapshot(source, 120.0, "US")

    assert len(source) == 30
    assert len(snapshot) == 31
    assert source["close"].iloc[-1] == 100.0
    assert snapshot["close"].iloc[-2] == 100.0
    assert snapshot["close"].iloc[-1] == 120.0
    assert round(float(snapshot["ma_5"].iloc[-1]), 4) == 104.0
    assert "rsi" in snapshot.columns
    assert snapshot.attrs["realtime_snapshot_only"] is True


def test_data_quality_blocks_new_buy_signals():
    signals = check_signals(
        _df(), [Variant()], "US",
        account_equity=100000.0,
        data_quality={
            "status": "blocked",
            "action": "block",
            "max_position_multiplier": 0.0,
            "issues": ["OHLC 价格关系异常"],
            "warnings": [],
        },
    )

    assert len(signals) == 1
    assert signals[0].signal == "no_signal"
    assert signals[0].execution_level == "D"
    assert "数据质量阻断" in signals[0].no_signal_reason


def test_data_quality_degrades_position_and_plan_markdown():
    signals = check_signals(
        _df(), [Variant()], "US",
        account_equity=100000.0,
        data_quality={
            "status": "degraded",
            "action": "reduce_position",
            "max_position_multiplier": 0.5,
            "issues": [],
            "warnings": ["K线样本偏少"],
        },
    )

    assert signals[0].signal == "buy"
    assert signals[0].position_pct == 0.125
    assert signals[0].execution_level == "B"
    assert "数据质量降级" in signals[0].reason

    plan = generate_operation_plan(
        signals,
        current_price=100.0,
        data_quality={
            "score": 64,
            "status": "degraded",
            "action": "reduce_position",
            "max_position_multiplier": 0.5,
            "issues": [],
            "warnings": ["K线样本偏少"],
            "missing": ["新闻数据缺失"],
        },
    )
    assert "数据质量与可信度闸门" in plan.markdown
    assert "64/100" in plan.markdown


def test_strategy_health_demote_blocks_buy_execution():
    ranked, plan = run_signal_check(
        _df(), [Variant()], "US",
        account_equity=100000.0,
        health_data=[{
            "strategy_name": "T",
            "action": "demote",
            "status": "unreliable",
        }],
    )

    assert ranked[0].signal == "no_signal"
    assert ranked[0].execution_level == "C"
    assert ranked[0].position_pct == 0.0
    assert "历史健康度" in ranked[0].no_signal_reason
    assert plan and plan.conservative is None
    assert "不允许买入/加仓" in plan.markdown
    assert "激进方案也不允许抢先试探" in plan.markdown


def test_strategy_health_watch_reduces_buy_position():
    ranked, plan = run_signal_check(
        _df(), [Variant()], "US",
        account_equity=100000.0,
        health_data=[{
            "strategy_name": "固定买入测试策略",
            "action": "watch",
            "status": "unstable",
        }],
    )

    assert ranked[0].signal == "buy"
    assert ranked[0].execution_level == "B"
    assert ranked[0].position_pct == 0.125
    assert plan and plan.conservative
    assert plan.conservative["position_pct"] == 0.125


def test_strategy_health_insufficient_sample_reduces_more():
    ranked, plan = run_signal_check(
        _df(), [Variant()], "US",
        account_equity=100000.0,
        health_data=[{
            "strategy_name": "固定买入测试策略",
            "action": "watch",
            "status": "unstable",
            "total": 4,
            "accuracy": 1.0,
            "confidence_lower_95": 0.51,
            "avg_return": 0.01,
            "sample_status": "insufficient",
            "risk_note": "历史样本不足，不能作为强执行依据",
        }],
    )

    assert ranked[0].signal == "buy"
    assert ranked[0].execution_level == "B"
    assert round(ranked[0].position_pct, 4) == 0.0825
    assert "样本4次" in ranked[0].reason
    assert "95%下界51%" in ranked[0].reason
    assert plan and plan.conservative
    assert round(plan.conservative["position_pct"], 4) == 0.0825


def test_evaluate_data_quality_detects_invalid_ohlc():
    bad = _df()
    bad.loc[bad.index[-1], "high"] = 90.0
    report = evaluate_data_quality(bad, market="US")

    assert report.status == "blocked"
    assert report.block_new_entries is True
    assert any("OHLC" in issue for issue in report.issues)


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
    assert "估算滑点/佣金/税费" in plan.markdown
    assert "不含跳空" in plan.markdown
    assert "失效条件" in plan.markdown
    assert "信号强度" in plan.markdown
    assert "**2.00%** 净值" in plan.markdown
    assert "92.0% 风控" not in plan.markdown


if __name__ == "__main__":
    test_check_signals_position_pct_comes_from_order_shares()
    test_check_signals_can_use_real_account_equity()
    test_check_signals_uses_current_price_for_sizing()
    test_realtime_snapshot_appends_bar_and_recomputes_indicators()
    test_data_quality_blocks_new_buy_signals()
    test_data_quality_degrades_position_and_plan_markdown()
    test_strategy_health_demote_blocks_buy_execution()
    test_strategy_health_watch_reduces_buy_position()
    test_strategy_health_insufficient_sample_reduces_more()
    test_evaluate_data_quality_detects_invalid_ohlc()
    test_check_signals_uses_current_position_for_sell_signal()
    test_operation_plan_reports_real_loss_pct_and_bearish_caps()
    print("12/12 passed")
