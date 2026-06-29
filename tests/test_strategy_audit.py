"""
策略审计判定测试。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.strategy_audit import (
    StrategyAuditEntry,
    StrategyAuditReport,
    _apply_verdict,
    _block_bootstrap_metrics,
)
from report.prompts import build_strategy_audit_section


def test_negative_test_return_fails_even_with_good_sharpe():
    verdict, overfit, reason = _apply_verdict(
        train_trades=10,
        test_trades=5,
        train_sharpe=1.2,
        test_sharpe=1.3,
        test_return=-0.01,
        test_drawdown=0.05,
        test_win_rate=0.6,
        sharpe_degradation=1.0,
        return_degradation=1.0,
    )

    assert verdict == "FAIL"
    assert "验证期未赚钱" in reason


def test_thin_positive_test_return_is_conditional_not_pass():
    verdict, overfit, reason = _apply_verdict(
        train_trades=10,
        test_trades=5,
        train_sharpe=1.2,
        test_sharpe=1.3,
        test_return=0.01,
        test_drawdown=0.05,
        test_win_rate=0.6,
        sharpe_degradation=1.0,
        return_degradation=1.0,
    )

    assert verdict == "CONDITIONAL"
    assert "验证收益偏薄" in reason


def test_block_bootstrap_reports_positive_expectancy_and_tail_risk():
    equities = [100000.0]
    for i in range(60):
        daily_return = 0.002 if i % 5 else -0.001
        equities.append(equities[-1] * (1 + daily_return))

    result = _block_bootstrap_metrics(
        equities, trade_count=6, simulations=200, seed=7
    )

    assert result["status"] == "ok"
    assert result["samples"] == 200
    assert result["positive_expectancy_prob"] > 0.95
    assert result["return_ci_low"] > 0
    assert result["drawdown_p95"] >= 0
    assert result["ruin_probability"] == 0


def test_block_bootstrap_refuses_small_samples():
    result = _block_bootstrap_metrics(
        [100000.0, 101000.0, 102000.0], trade_count=2,
        simulations=50, seed=1,
    )

    assert result["status"] == "insufficient"
    assert result["samples"] == 0


def test_strategy_audit_report_displays_bootstrap_uncertainty():
    entry = StrategyAuditEntry(
        strategy_key="A", strategy_name="测试策略", verdict="CONDITIONAL",
        bootstrap_status="ok", bootstrap_samples=400,
        positive_expectancy_prob=0.68,
        return_ci_low=-0.03, return_ci_high=0.12,
        sharpe_ci_low=-0.2, sharpe_ci_high=1.8,
        drawdown_p95=0.15, ruin_probability=0.02,
    )
    report = StrategyAuditReport(
        split_date="2026-01-01", train_period="2025", test_period="2026",
        entries=[entry], summary={"conditional": 1},
    )

    markdown = build_strategy_audit_section(report)

    assert "分块 Bootstrap 风险" in markdown
    assert "68%" in markdown
    assert "[-3.0%, +12.0%]" in markdown


if __name__ == "__main__":
    test_negative_test_return_fails_even_with_good_sharpe()
    test_thin_positive_test_return_is_conditional_not_pass()
    test_block_bootstrap_reports_positive_expectancy_and_tail_risk()
    test_block_bootstrap_refuses_small_samples()
    test_strategy_audit_report_displays_bootstrap_uncertainty()
    print("5/5 passed")
