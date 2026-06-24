"""
策略池缓存可信度测试。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core.strategy_pool import StrategyVariant, _make_cache_key, _select_representative_variants
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
    def __init__(self, strategy_key, test_sharpe, test_return=0.0):
        self.strategy_key = strategy_key
        self.test_sharpe = test_sharpe
        self.test_return = test_return


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


if __name__ == "__main__":
    test_variant_memory_cache_key_is_stock_scoped()
    test_sqlite_backtest_cache_respects_data_length()
    test_representative_variants_keep_one_per_base_strategy()
    print("3/3 passed")
