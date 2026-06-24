"""
策略审计判定测试。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.strategy_audit import _apply_verdict


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


if __name__ == "__main__":
    test_negative_test_return_fails_even_with_good_sharpe()
    test_thin_positive_test_return_is_conditional_not_pass()
    print("2/2 passed")
