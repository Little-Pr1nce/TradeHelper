"""
条件化策略决策测试。
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.signal_check import run_signal_check
from core.pipeline import run_pipeline
from core.strategy_pool import StrategyVariant
from indicators.technical import calc_all_indicators
from strategies import get_available_strategies, get_execution_strategy
from strategies.base import BaseExecutionStrategy, Position, StrategyContext


def _df_for_support() -> pd.DataFrame:
    rows = []
    dates = pd.date_range("2026-01-01", periods=130, freq="B")
    for i in range(130):
        close = 100.0
        rows.append({
            "date": dates[i],
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000000,
            "Final_Score": 0.0,
        })
    rows[-1].update({"open": 99.5, "high": 102.0, "low": 99.0, "close": 101.0})
    df = calc_all_indicators(pd.DataFrame(rows))
    df["Final_Score"] = 0.0
    return df


def _df_for_profit_lock() -> pd.DataFrame:
    rows = []
    dates = pd.date_range("2026-02-01", periods=130, freq="B")
    for i in range(130):
        close = 180.0 + i * 0.1
        rows.append({
            "date": dates[i],
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000000,
            "Final_Score": 0.1,
        })
    rows[-1].update({"open": 194.74, "high": 217.09, "low": 190.93, "close": 207.14})
    df = calc_all_indicators(pd.DataFrame(rows))
    df["Final_Score"] = 0.1
    return df


def _df_for_pullback_failed() -> pd.DataFrame:
    rows = []
    dates = pd.date_range("2026-03-01", periods=130, freq="B")
    for i in range(130):
        close = 100.0
        rows.append({
            "date": dates[i],
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000000,
            "Final_Score": -0.1,
        })
    rows[-1].update({"open": 98.0, "high": 100.1, "low": 96.0, "close": 98.0})
    df = calc_all_indicators(pd.DataFrame(rows))
    df["Final_Score"] = -0.1
    return df


def test_ma120_support_rebound_generates_buy_decision():
    strategy = get_execution_strategy("P")
    df = _df_for_support()
    ctx = StrategyContext(
        date="2026-01-28",
        equity=100000,
        cash=100000,
        position=Position(),
        market="US",
    )

    decision = strategy.generate_decision(df, ctx)
    orders = strategy.generate_orders(df, ctx)

    assert decision.action == "buy"
    assert decision.execution_level == "B"
    assert decision.stop_loss > 0
    assert orders and orders[0].action == "buy"


def test_profit_lock_generates_partial_sell_decision():
    strategy = get_execution_strategy("Q")
    df = _df_for_profit_lock()
    ctx = StrategyContext(
        date="2026-02-28",
        equity=100000,
        cash=5000,
        position=Position(shares=10, avg_cost=180.22, entry_price=180.22),
        market="US",
    )

    decision = strategy.generate_decision(df, ctx)
    orders = strategy.generate_orders(df, ctx)

    assert decision.action == "sell"
    assert decision.execution_level == "B"
    assert "回落" in decision.reason
    assert orders and orders[0].shares == 5


def test_position_risk_generates_hard_sell_decision():
    strategy = get_execution_strategy("R")
    df = _df_for_support()
    df.loc[df.index[-1], "close"] = 70.0
    df.loc[df.index[-1], "Final_Score"] = -0.3
    ctx = StrategyContext(
        date="2026-01-28",
        equity=100000,
        cash=5000,
        position=Position(shares=20, avg_cost=100.0, entry_price=100.0),
        market="US",
    )

    decision = strategy.generate_decision(df, ctx)
    orders = strategy.generate_orders(df, ctx)

    assert decision.action == "sell"
    assert decision.execution_level == "A"
    assert "浮亏" in decision.reason
    assert orders and orders[0].shares == 20


def test_pullback_failed_exit_generates_sell_decision():
    strategy = get_execution_strategy("S")
    df = _df_for_pullback_failed()
    ctx = StrategyContext(
        date="2026-03-28",
        equity=100000,
        cash=5000,
        position=Position(shares=20, avg_cost=105.0, entry_price=105.0),
        market="US",
    )

    decision = strategy.generate_decision(df, ctx)
    orders = strategy.generate_orders(df, ctx)

    assert decision.action == "sell"
    assert decision.execution_level in ("A", "B")
    assert "反抽" in decision.reason
    assert orders and orders[0].action == "sell"


def test_conditional_trigger_strategy_outputs_plan_without_order():
    strategy = get_execution_strategy("T")
    df = _df_for_support()
    ctx = StrategyContext(
        date="2026-01-28",
        equity=100000,
        cash=100000,
        position=Position(),
        market="US",
    )

    decision = strategy.generate_decision(df, ctx)
    orders = strategy.generate_orders(df, ctx)

    assert decision.action == "watch"
    assert decision.trigger_price > 0
    assert decision.stop_loss > 0
    assert "买入/加仓" in "；".join(decision.missing_conditions)
    assert orders == []


def test_decision_first_strategy_uses_default_order_conversion():
    strategy = get_execution_strategy("P")
    df = _df_for_support()
    ctx = StrategyContext(
        date="2026-01-28",
        equity=100000,
        cash=100000,
        position=Position(),
        market="US",
    )

    decision = strategy.generate_decision(df, ctx)
    orders = strategy.generate_orders(df, ctx)

    assert decision.shares > 0
    assert orders and orders[0].shares == decision.shares
    assert orders[0].stop_loss == decision.stop_loss


def test_signal_check_carries_decision_level_and_conditions():
    df = _df_for_profit_lock()
    strategy = get_execution_strategy("Q")
    variants = [StrategyVariant(
        base_key="Q",
        variant_label="Q",
        strategy=strategy,
        params={},
        is_default=True,
    )]
    position = Position(shares=10, avg_cost=180.22, entry_price=180.22)

    ranked, plan = run_signal_check(
        df=df,
        variants=variants,
        market="US",
        current_position=position,
        account_equity=100000,
        current_price=207.14,
    )

    assert ranked
    assert ranked[0].signal == "sell"
    assert ranked[0].execution_level == "B"
    assert "冲高回落锁利" in plan.markdown


def test_pipeline_injects_current_position_overlay_strategies():
    # 历史序列只到 T-1；当日冲高回落仅存在于内存实时报价。
    df = _df_for_profit_lock().iloc[:-1].copy()

    result = run_pipeline(
        df=df,
        news_df=None,
        initial_capital=100000,
        account_equity=100000,
        current_position=Position(shares=10, avg_cost=180.22, entry_price=180.22),
        market="US",
        strategy_names=[],
        skip_param_tuning=True,
        expand_pool=False,
        current_price=207.14,
        current_bar={
            "open": 194.74,
            "high": 217.09,
            "low": 190.93,
            "latest": 207.14,
            "volume": 2000000,
        },
    )

    signals = result.signal_check or []
    keys = {s["key"] for s in signals}
    sell = [s for s in signals if s["signal"] == "sell"]

    assert {"P", "Q", "R", "S", "T"}.issubset(keys)
    assert sell
    assert sell[0]["execution_level"] == "B"
    assert "冲高回落锁利" in (result.operation_plan or "")
    assert "持仓退出/减仓信号" in (result.operation_plan or "")
    assert len(result.df) == len(df)
    assert result.decision_df is not None
    assert len(result.decision_df) == len(df) + 1
    assert float(result.decision_df.iloc[-1]["high"]) == 217.09
    assert float(result.decision_df.iloc[-1]["low"]) == 190.93
    assert float(result.decision_df.iloc[-1]["close"]) == 207.14


def test_factor_only_pipeline_skips_backtests_and_signals():
    result = run_pipeline(
        df=_df_for_support(),
        news_df=None,
        market="US",
        strategy_names=[],
        run_backtests=False,
        run_signals=False,
        expand_pool=False,
        skip_param_tuning=True,
    )

    assert result.backtest == {}
    assert result.strategy_audit is None
    assert result.strategy_pool is None
    assert result.signal_check is None
    assert "Final_Score" in result.df.columns


def test_pipeline_future_mutation_does_not_change_past_scores():
    original = _df_for_support()
    mutated = original.copy()
    for offset, idx in enumerate(mutated.index[100:], start=1):
        close = 100.0 + offset * 0.5
        mutated.loc[idx, ["open", "close"]] = [close, close]
        mutated.loc[idx, "high"] = close + 1.0
        mutated.loc[idx, "low"] = close - 1.0

    kwargs = dict(
        news_df=None, market="US", strategy_names=[],
        run_backtests=False, run_signals=False, expand_pool=False,
        skip_param_tuning=True,
    )
    before = run_pipeline(original, **kwargs)
    after = run_pipeline(mutated, **kwargs)

    pd.testing.assert_series_equal(
        before.df["Final_Score"].iloc[:100],
        after.df["Final_Score"].iloc[:100],
    )


def test_all_registered_strategies_use_decision_first_public_path():
    for key in get_available_strategies():
        strategy = get_execution_strategy(key)
        strategy_type = type(strategy)
        assert strategy_type.generate_decision is not BaseExecutionStrategy.generate_decision, key
        assert strategy_type.generate_orders is BaseExecutionStrategy.generate_orders, key
        assert strategy_type.diagnose_no_signal is not BaseExecutionStrategy.diagnose_no_signal, key


def test_human_strategies_expose_native_missing_conditions():
    df = _df_for_support()
    df.loc[:, "open"] = 100.0
    df.loc[:, "high"] = 101.0
    df.loc[:, "low"] = 99.0
    df.loc[:, "close"] = 100.0
    df = calc_all_indicators(df)
    df["Final_Score"] = 0.0
    context = StrategyContext(
        date="2026-01-28", equity=100000, cash=100000,
        position=Position(), market="US",
    )
    for key in ("I", "J", "K", "L", "M", "N"):
        decision = get_execution_strategy(key).generate_decision(df, context)
        assert decision.action in ("watch", "hold"), key
        assert decision.missing_conditions, key
        assert all("未同时满足全部条件" not in item for item in decision.missing_conditions), key


def test_overlay_strategies_expose_diagnostic_interface():
    df = _df_for_support()
    context = StrategyContext(
        date="2026-01-28", equity=100000, cash=100000,
        position=Position(), market="US",
    )
    for key in ("P", "Q", "R", "S", "T"):
        diagnostics = get_execution_strategy(key).diagnose_no_signal(df, context)
        assert diagnostics, key
        assert all(isinstance(item, str) and item.strip() for item in diagnostics), key


if __name__ == "__main__":
    tests = [
        test_ma120_support_rebound_generates_buy_decision,
        test_profit_lock_generates_partial_sell_decision,
        test_position_risk_generates_hard_sell_decision,
        test_pullback_failed_exit_generates_sell_decision,
        test_conditional_trigger_strategy_outputs_plan_without_order,
        test_decision_first_strategy_uses_default_order_conversion,
        test_signal_check_carries_decision_level_and_conditions,
        test_pipeline_injects_current_position_overlay_strategies,
        test_factor_only_pipeline_skips_backtests_and_signals,
        test_pipeline_future_mutation_does_not_change_past_scores,
        test_all_registered_strategies_use_decision_first_public_path,
        test_human_strategies_expose_native_missing_conditions,
        test_overlay_strategies_expose_diagnostic_interface,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
