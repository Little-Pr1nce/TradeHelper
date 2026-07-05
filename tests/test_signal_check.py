"""
信号检查与操作方案测试。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np

from core.signal_check import (
    SignalResult,
    _apply_current_price_snapshot,
    _apply_forecast_to_signal,
    _apply_joint_oof_to_signal,
    build_forecast_consensus,
    check_signals,
    generate_operation_plan,
    rank_signals,
    run_signal_check,
    select_actionable_sell_signals,
    select_signal_family_representatives,
)
from core.data_quality import evaluate_data_quality
from strategies.base import BaseExecutionStrategy, Order, Position


class FixedBuyStrategy(BaseExecutionStrategy):
    suitable_regimes = []
    take_profit_mode = "dynamic"
    take_profit_rule = "最高收盘价减2倍ATR"

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


def test_zero_real_account_equity_never_uses_reference_capital():
    signals = check_signals(
        _df(), [Variant()], "US",
        initial_capital=100000.0,
        account_equity=0.0,
    )

    assert len(signals) == 1
    assert signals[0].signal == "no_signal"
    assert signals[0].execution_level == "D"
    assert signals[0].position_pct == 0.0
    assert "真实账户权益为0" in signals[0].no_signal_reason

    plan = generate_operation_plan(
        signals, current_price=100.0, account_equity=0.0, df=_df()
    )
    assert plan.account_equity == 0.0
    assert plan.equity_is_reference is False
    assert "禁止新开仓" in plan.markdown


def test_a_level_buy_requires_matching_positive_history():
    signal = SignalResult(
        variant_label="A", strategy_name="A", base_key="A",
        signal="buy", execution_level="A", position_pct=0.4,
        reason="条件已确认",
    )
    [without_history] = rank_signals([signal], health_data=[])
    assert without_history.execution_level == "B"
    assert without_history.position_pct == 0.2

    supported = SignalResult(
        variant_label="A", strategy_name="A", base_key="A",
        signal="buy", execution_level="A", position_pct=0.4,
        reason="条件已确认",
    )
    [with_history] = rank_signals([supported], health_data=[{
        "strategy_name": "A", "action": "keep", "total": 12,
        "accuracy": 0.67, "confidence_lower_95": 0.39,
        "avg_return": 0.03, "sample_status": "ok",
    }])
    assert with_history.execution_level == "A"
    assert with_history.position_pct == 0.4


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
    assert pd.isna(snapshot["volume"].iloc[-1])
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


def test_missing_per_stock_intraday_quote_blocks_only_that_stock():
    report = evaluate_data_quality(
        _df(),
        current_price=100.0,
        market="US",
        realtime_quote_quality={
            "required": True,
            "available": False,
            "fresh": False,
            "issues": ["当前时段实时报价缺失"],
            "warnings": [],
        },
    )

    assert report.status == "blocked"
    assert report.block_new_entries is True
    assert any("实时报价缺失" in issue for issue in report.issues)


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
    assert plan.conservative["position_pct"] < ranked[0].position_pct
    assert np.isclose(plan.conservative["max_loss_amount"], 1000.0)


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
        take_profit=116.0,
        position_pct=0.80,
        reason="测试订单",
        audit_verdict="PASS",
        test_sharpe=1.2,
        rank_score=80,
    )

    plan = generate_operation_plan([signal], current_price=100.0, market_bias="bearish", df=_df())

    assert plan.conservative["position_pct"] < 0.25
    assert plan.aggressive["position_pct"] < 0.35
    assert np.isclose(plan.conservative["max_loss_amount"], 1000.0)
    assert np.isclose(plan.aggressive["max_loss_amount"], 2000.0)
    assert "单价风险 8.0%" in plan.markdown
    assert "账户风险" in plan.markdown
    assert "最大亏损" in plan.markdown
    assert "估算滑点/佣金/税费" in plan.markdown
    assert "不含跳空" in plan.markdown
    assert "失效条件" in plan.markdown
    assert "信号强度" in plan.markdown
    assert "**2.00%** 净值" in plan.markdown
    assert "| 风险收益比 | 1:2.00 | 1:2.00 |" in plan.markdown
    assert "92.0% 风控" not in plan.markdown


def test_signal_rank_does_not_depend_on_reason_length():
    short = SignalResult(
        variant_label="O", strategy_name="趋势策略", base_key="O",
        signal="buy", entry_price=100.0, stop_loss=92.0,
        trigger_price=100.0, invalidation="跌破92", position_pct=0.2,
        execution_level="B", reason="趋势满仓",
    )
    long = SignalResult(
        variant_label="A", strategy_name="百分位策略", base_key="A",
        signal="buy", entry_price=100.0, stop_loss=92.0,
        trigger_price=100.0, invalidation="跌破92", position_pct=0.2,
        execution_level="B", reason="这是一段明显更长但不应获得额外排序分数的策略解释文本",
    )

    ranked = rank_signals([short, long])

    assert ranked[0].rank_score == ranked[1].rank_score


def test_existing_concentration_can_block_additional_position():
    signal = SignalResult(
        variant_label="T", strategy_name="测试", base_key="T",
        signal="buy", entry_price=100.0, stop_loss=92.0,
        position_pct=0.2, execution_level="B", reason="测试",
    )
    plan = generate_operation_plan(
        [signal], current_price=100.0, account_equity=100000.0, df=_df(),
        current_position=Position(shares=200, avg_cost=90.0, entry_price=90.0),
    )

    assert plan.conservative["position_pct"] == 0.0
    assert "已有仓位20.0%" in plan.conservative["position_cap_reason"]


def test_missing_take_profit_is_reported_as_unquantifiable():
    signal = SignalResult(
        variant_label="T", strategy_name="测试", base_key="T",
        signal="buy", entry_price=100.0, stop_loss=92.0,
        position_pct=0.1, execution_level="B", reason="测试",
    )
    plan = generate_operation_plan([signal], current_price=100.0, df=_df())

    assert "无主动止盈，仅止损/时间退出" in plan.markdown
    assert "不可量化（无主动止盈）" in plan.markdown


def test_dynamic_take_profit_rule_flows_into_signal_and_plan():
    signals = check_signals(_df(), [Variant()], "US", initial_capital=100000.0)

    assert signals[0].take_profit == 0.0
    assert signals[0].take_profit_mode == "dynamic"
    assert signals[0].take_profit_rule == "最高收盘价减2倍ATR"
    assert signals[0].to_dict()["take_profit_mode"] == "dynamic"

    plan = generate_operation_plan(signals, current_price=100.0, df=_df())
    assert "动态止盈：最高收盘价减2倍ATR" in plan.markdown
    assert "不可固定量化（动态止盈）" in plan.markdown


def test_single_ordinary_strategy_exit_is_reported_as_conflict_not_global_sell():
    signal = SignalResult(
        variant_label="C", strategy_name="动量突破策略", base_key="C",
        signal="sell", entry_price=100.0, position_pct=0.1,
        execution_level="A", reason="动量条件转弱",
    )
    plan = generate_operation_plan(
        [signal], current_price=100.0, account_equity=10000.0,
        current_position=Position(shares=10, avg_cost=90.0, entry_price=90.0),
        df=_df(),
    )

    assert "持仓策略分歧" in plan.markdown
    assert "不把单一策略退出升级为整只持仓卖出" in plan.markdown
    assert "持仓退出/减仓信号" not in plan.markdown


def test_same_trigger_price_explains_risk_budget_difference():
    signal = SignalResult(
        variant_label="O", strategy_name="趋势策略", base_key="O",
        signal="buy", entry_price=100.0, stop_loss=92.0,
        position_pct=0.2, execution_level="B", reason="趋势条件满足",
    )
    plan = generate_operation_plan([signal], current_price=100.0, df=_df())

    assert plan.conservative["entry"] == plan.aggressive["entry"]
    assert "不虚构第二个价格" in plan.markdown
    assert "保守性由较低仓位" in plan.markdown
    assert "激进性由较高仓位" in plan.markdown


def test_execution_plan_keeps_one_representative_per_strategy_family():
    signals = [
        SignalResult("A", "趋势A", "A", "buy", strategy_family="trend_following", rank_score=90),
        SignalResult("H", "趋势H", "H", "buy", strategy_family="trend_following", rank_score=80),
        SignalResult("B", "均值B", "B", "buy", strategy_family="mean_reversion", rank_score=70),
    ]

    selected = select_signal_family_representatives(signals)

    assert [s.base_key for s in selected] == ["A", "B"]


def test_same_family_sell_signals_do_not_create_false_consensus():
    signals = [
        SignalResult(
            "A", "趋势A", "A", "sell", strategy_family="trend_following",
            execution_level="A",
        ),
        SignalResult(
            "H", "趋势H", "H", "sell", strategy_family="trend_following",
            execution_level="A",
        ),
    ]

    assert select_actionable_sell_signals(signals) == []


def test_mature_negative_joint_oof_can_only_reduce_new_entry_risk():
    buy = SignalResult(
        "A", "趋势A", "A", "buy", signal_intent="alpha_entry",
        execution_level="B", position_pct=0.2, max_loss_amount=500.0,
    )
    health = {
        "samples": 60, "total_trades": 8, "total_return": -0.05,
        "excess_return": -0.08, "sharpe_ratio": -0.5,
    }
    _apply_joint_oof_to_signal(buy, health)

    assert buy.signal == "no_signal"
    assert buy.execution_level == "C"
    assert buy.position_pct == 0.0
    assert "联合OOF" in buy.no_signal_reason

    sell = SignalResult(
        "R", "风险退出", "R", "sell", signal_intent="risk_exit",
        execution_level="A", position_pct=1.0,
    )
    _apply_joint_oof_to_signal(sell, health)
    assert sell.signal == "sell"
    assert sell.execution_level == "A"


def test_joint_oof_drift_warning_reduces_but_never_upgrades_entry():
    buy = SignalResult(
        "A", "趋势A", "A", "buy", signal_intent="alpha_entry",
        execution_level="A", position_pct=0.4, max_loss_amount=1000.0,
    )
    _apply_joint_oof_to_signal(buy, {
        "samples": 60, "total_trades": 7, "total_return": 0.04,
        "excess_return": 0.01, "sharpe_ratio": 0.4,
        "drift_status": "warning", "drift_reasons": ["超额收益较上次下降6.0%"],
    })

    assert buy.execution_level == "B"
    assert np.isclose(buy.position_pct, 0.3)
    assert np.isclose(buy.max_loss_amount, 750.0)
    assert "性能漂移" in buy.reason


def test_multihorizon_forecast_consensus_ignores_unvalidated_and_reduces_conflict():
    unvalidated = SimpleNamespace(
        horizon=1, confidence=0.0, direction="bearish",
        prob_up=0.05, prob_down=0.90,
    )
    assert build_forecast_consensus([unvalidated])["direction"] == "unknown"

    forecasts = [
        SimpleNamespace(
            horizon=1, confidence=0.4, direction="bullish",
            prob_up=0.70, prob_down=0.10,
        ),
        SimpleNamespace(
            horizon=3, confidence=0.3, direction="bearish",
            prob_up=0.15, prob_down=0.65,
        ),
        SimpleNamespace(
            horizon=5, confidence=0.2, direction="bearish",
            prob_up=0.20, prob_down=0.60,
        ),
    ]
    consensus = build_forecast_consensus(forecasts)
    assert consensus["conflict"] is True
    assert consensus["validated_horizons"] == [1, 3, 5]

    signal = SignalResult(
        "A", "趋势A", "A", "buy", signal_intent="alpha_entry",
        execution_level="A", position_pct=0.4, max_loss_amount=1000.0,
    )
    _apply_forecast_to_signal(signal, forecasts)
    assert signal.execution_level == "B"
    assert np.isclose(signal.position_pct, 0.3)
    assert "方向冲突" in signal.reason


if __name__ == "__main__":
    test_check_signals_position_pct_comes_from_order_shares()
    test_check_signals_can_use_real_account_equity()
    test_zero_real_account_equity_never_uses_reference_capital()
    test_a_level_buy_requires_matching_positive_history()
    test_check_signals_uses_current_price_for_sizing()
    test_realtime_snapshot_appends_bar_and_recomputes_indicators()
    test_data_quality_blocks_new_buy_signals()
    test_missing_per_stock_intraday_quote_blocks_only_that_stock()
    test_data_quality_degrades_position_and_plan_markdown()
    test_strategy_health_demote_blocks_buy_execution()
    test_strategy_health_watch_reduces_buy_position()
    test_strategy_health_insufficient_sample_reduces_more()
    test_evaluate_data_quality_detects_invalid_ohlc()
    test_check_signals_uses_current_position_for_sell_signal()
    test_operation_plan_reports_real_loss_pct_and_bearish_caps()
    test_signal_rank_does_not_depend_on_reason_length()
    test_existing_concentration_can_block_additional_position()
    test_missing_take_profit_is_reported_as_unquantifiable()
    test_dynamic_take_profit_rule_flows_into_signal_and_plan()
    test_single_ordinary_strategy_exit_is_reported_as_conflict_not_global_sell()
    test_same_trigger_price_explains_risk_budget_difference()
    test_execution_plan_keeps_one_representative_per_strategy_family()
    test_same_family_sell_signals_do_not_create_false_consensus()
    test_mature_negative_joint_oof_can_only_reduce_new_entry_risk()
    test_joint_oof_drift_warning_reduces_but_never_upgrades_entry()
    test_multihorizon_forecast_consensus_ignores_unvalidated_and_reduces_conflict()
    print("26/26 passed")
