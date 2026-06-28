"""
策略池缓存可信度测试。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core.strategy_pool import (
    _cache_params_json,
    StrategyVariant,
    generate_variants,
    _generate_recovery_variants,
    _make_cache_key,
    _select_representative_variants,
    _update_per_stock_params,
)
from strategies import get_execution_strategy
from data.database import Database


def _df(n=10):
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="B"),
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0] * n,
        "volume": [1000000] * n,
        "Final_Score": [0.0] * n,
    })


def _fresh_db():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    return Database.init(os.path.join(tmpdir, "test.db"))


def test_variant_memory_cache_key_is_stock_scoped():
    df = _df()

    aapl = _make_cache_key("AAPL", "A", {"entry": 0.6}, df)
    msft = _make_cache_key("MSFT", "A", {"entry": 0.6}, df)

    assert aapl != msft


def test_backtest_cache_context_separates_capital_and_alpha():
    df = _df()
    df["Final_Score"] = 0.1
    base = _cache_params_json({"entry_pct": 0.7}, df, 100000.0)
    other_capital = _cache_params_json({"entry_pct": 0.7}, df, 50000.0)
    changed = df.copy()
    changed.loc[changed.index[-1], "Final_Score"] = 0.2
    other_score = _cache_params_json({"entry_pct": 0.7}, changed, 100000.0)

    assert base != other_capital
    assert base != other_score


def test_sqlite_backtest_cache_respects_data_length():
    db = _fresh_db()
    db.save_backtest_cache(
        stock_code="AAPL",
        strategy_key="A",
        params_json="{}",
        data_start="2026-01-01",
        data_end="2026-01-15",
        data_length=10,
        sharpe_ratio=1.0,
        total_return=0.1,
        max_drawdown=0.05,
        win_rate=0.6,
        total_trades=3,
        result_json='{"strategy_name":"A","total_return":0.1}',
    )

    assert db.get_cached_backtest(
        "AAPL", "A", "{}", "2026-01-01", "2026-01-15", data_length=10
    ) is not None
    assert db.get_cached_backtest(
        "AAPL", "A", "{}", "2026-01-01", "2026-01-15", data_length=9
    ) is None


class AuditEntry:
    def __init__(self, strategy_key, test_sharpe, test_return=0.0, verdict="PASS"):
        self.strategy_key = strategy_key
        self.test_sharpe = test_sharpe
        self.test_return = test_return
        self.verdict = verdict


def test_representative_variants_keep_one_per_base_strategy():
    strategy = get_execution_strategy("A")
    variants = [
        StrategyVariant("A", "A_v1", strategy, {"entry_pct": 0.6}),
        StrategyVariant("A", "A_v2", strategy, {"entry_pct": 0.7}),
        StrategyVariant("B", "B_v1", get_execution_strategy("B"), {"entry_pct": 0.2}),
    ]
    audit_entries = [
        AuditEntry("A_v1", 0.9),
        AuditEntry("A_v2", 1.4),
        AuditEntry("B_v1", 0.8),
    ]

    selected_pass, selected_cond = _select_representative_variants(
        pass_variants=variants[:2],
        cond_variants=variants[2:],
        audit_entries=audit_entries,
    )

    assert [v.variant_label for v in selected_pass] == ["A_v2"]
    assert [v.variant_label for v in selected_cond] == ["B_v1"]


def test_auto_tuned_params_can_recover_demoted_strategy():
    db = _fresh_db()
    db.mark_strategy_demoted("AAPL", "A", "历史负期望")

    variant = StrategyVariant(
        "A",
        "A_v2",
        get_execution_strategy("A", entry_pct=0.7),
        {"entry_pct": 0.7},
    )
    wf = {
        "A_v2": {
            "pass_oos": True,
            "avg_oos_return": 0.04,
            "avg_oos_sharpe": 0.8,
            "oos_trades": 6,
            "selected_windows": 2,
        }
    }
    _update_per_stock_params(
        db=db,
        stock_code="AAPL",
        variants=[variant],
        bt_results={},
        audit_entries=[AuditEntry("A_v2", 1.25)],
        allowed_labels={"A_v2"},
        walk_forward=wf,
        data_end="2026-01-31",
    )

    first = db.get_best_params("AAPL", "A")
    assert first["source"] == "demoted"

    _update_per_stock_params(
        db=db,
        stock_code="AAPL",
        variants=[variant],
        bt_results={},
        audit_entries=[AuditEntry("A_v2", 1.25)],
        allowed_labels={"A_v2"},
        walk_forward=wf,
        data_end="2026-02-02",
    )

    best = db.get_best_params("AAPL", "A")
    assert best["source"] == "auto_tuned"
    assert best["params"] == {"entry_pct": 0.7}
    assert db.is_strategy_demoted("AAPL", "A") is False


def test_promoted_candidate_rolls_back_when_health_demotes():
    db = _fresh_db()
    params_json = '{"entry_pct": 0.7}'
    wf = {
        "pass_oos": True,
        "avg_oos_return": 0.03,
        "avg_oos_sharpe": 0.7,
        "oos_trades": 5,
        "selected_windows": 2,
    }
    db.record_strategy_param_candidate(
        stock_code="AAPL", strategy_key="A", params_json=params_json,
        test_sharpe=1.1, walk_forward=wf, data_end="2026-01-31",
    )
    promoted = db.record_strategy_param_candidate(
        stock_code="AAPL", strategy_key="A", params_json=params_json,
        test_sharpe=1.1, walk_forward=wf, data_end="2026-02-02",
    )
    assert promoted["promoted"] is True

    db.mark_strategy_demoted("AAPL", "A", "新版净收益转负")

    best = db.get_best_params("AAPL", "A")
    candidates = db.get_strategy_param_candidates("AAPL", "A")
    assert best["source"] == "demoted"
    assert candidates[0]["status"] == "rolled_back"


def test_deep_optimization_run_state_prevents_duplicate_jobs():
    db = _fresh_db()
    assert db.has_recent_deep_optimization("AAPL", "2026-06-27") is False

    db.mark_deep_optimization_started("AAPL", "2026-06-27")
    assert db.has_recent_deep_optimization("AAPL", "2026-06-27") is True

    db.mark_deep_optimization_finished(
        "AAPL", "2026-06-27", variant_count=24
    )
    row = db.execute(
        "SELECT * FROM deep_optimization_runs WHERE stock_code=? AND data_end=?",
        ("AAPL", "2026-06-27"),
    ).fetchone()
    assert row["status"] == "complete"
    assert row["variant_count"] == 24


def test_demoted_strategy_gets_conservative_recovery_variants():
    base_variants = generate_variants(["A"], max_per_strategy=7)
    recovery = _generate_recovery_variants("A", base_variants)

    assert recovery
    assert all(v.variant_label.startswith("A_recover_") for v in recovery)
    assert any(v.params.get("entry_pct") == 0.85 for v in recovery)
    assert any(v.params.get("exit_pct") == 0.60 for v in recovery)


if __name__ == "__main__":
    test_variant_memory_cache_key_is_stock_scoped()
    test_backtest_cache_context_separates_capital_and_alpha()
    test_sqlite_backtest_cache_respects_data_length()
    test_representative_variants_keep_one_per_base_strategy()
    test_auto_tuned_params_can_recover_demoted_strategy()
    test_promoted_candidate_rolls_back_when_health_demotes()
    test_deep_optimization_run_state_prevents_duplicate_jobs()
    test_demoted_strategy_gets_conservative_recovery_variants()
    print("8/8 passed")
