"""
可信度硬摘要测试。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report.prompts import build_trust_hard_summary


def test_trust_summary_blocks_on_data_quality():
    md = build_trust_hard_summary(
        data_quality_reports=[{
            "score": 42,
            "status": "blocked",
            "action": "block",
            "max_position_multiplier": 0,
        }],
        signal_checks=[{"signal": "buy", "execution_level": "A"}],
        evaluation_panel={"overall": {"count": 3, "expectancy": "positive", "avg_return": 0.04}},
        scope="AAPL",
    )
    assert "可信度硬摘要" in md
    assert "D 数据冲突/禁止新开仓" in md
    assert "数据质量阻断" in md
    assert "最弱闸门：**阻断新开仓**" in md


def test_trust_summary_demotes_negative_expectancy():
    md = build_trust_hard_summary(
        data_quality_reports=[{"score": 91, "status": "ok"}],
        audit_reports=[{"summary": {"pass": 2, "conditional": 1, "fail": 0, "overfit": 0}}],
        signal_checks=[{"signal": "buy", "execution_level": "A"}],
        evaluation_panel={"overall": {"count": 8, "expectancy": "negative", "avg_return": -0.025}},
        health_reports=[{"action": "demote"}, {"action": "watch"}, {"action": "keep"}],
        scope="组合",
    )
    assert "C 仅观察或小仓验证" in md
    assert "历史预测负期望" in md
    assert "降级 1" in md
    assert "平均方向净收益 -2.50%" in md


def test_unrelated_demoted_strategy_does_not_block_current_signal():
    md = build_trust_hard_summary(
        data_quality_reports=[{"score": 95, "status": "ok"}],
        signal_checks=[{
            "signal": "buy", "execution_level": "A",
            "name": "当前有效策略", "key": "GOOD",
        }],
        evaluation_panel={"overall": {"count": 12, "expectancy": "positive", "avg_return": 0.03}},
        health_reports=[{
            "strategy_name": "旧负期望策略", "action": "demote",
        }],
        scope="AAPL",
    )

    assert "A 可执行候选" in md
    assert "与当前可执行信号无关" in md
    assert "当前信号关联" not in md


def test_related_demoted_strategy_blocks_current_signal():
    md = build_trust_hard_summary(
        data_quality_reports=[{"score": 95, "status": "ok"}],
        signal_checks=[{
            "signal": "buy", "execution_level": "A",
            "name": "当前策略", "key": "A",
        }],
        evaluation_panel={"overall": {"count": 12, "expectancy": "positive", "avg_return": 0.03}},
        health_reports=[{
            "strategy_name": "当前策略", "action": "demote",
        }],
        scope="AAPL",
    )

    assert "C 仅观察或小仓验证" in md
    assert "当前信号关联的 1 个策略已降级" in md


if __name__ == "__main__":
    tests = [
        test_trust_summary_blocks_on_data_quality,
        test_trust_summary_demotes_negative_expectancy,
        test_unrelated_demoted_strategy_does_not_block_current_signal,
        test_related_demoted_strategy_blocks_current_signal,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
