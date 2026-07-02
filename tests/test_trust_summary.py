"""
可信度硬摘要测试。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report.prompts import (
    INTRADAY_SYSTEM_PROMPT,
    PORTFOLIO_SYSTEM_PROMPT,
    PREMARKET_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_trust_hard_summary,
)
from report.generator import _enforce_llm_compliance


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


def test_trust_summary_labels_current_opportunity_history_scope():
    md = build_trust_hard_summary(
        signal_checks=[{"signal": "buy", "execution_level": "B"}],
        evaluation_panel={"overall": {"count": 0, "expectancy": "insufficient"}},
        scope="美股组合",
    )

    assert "当前机会关联历史验证：0 次" in md
    assert "- 历史验证：0 次" not in md


def test_portfolio_prompt_forbids_unquantified_risk_reward_claims():
    assert "风险收益比必须可计算" in PORTFOLIO_SYSTEM_PROMPT
    assert "止损距离较近不等于风险收益比优秀" in PORTFOLIO_SYSTEM_PROMPT
    for prompt in (SYSTEM_PROMPT, INTRADAY_SYSTEM_PROMPT, PREMARKET_SYSTEM_PROMPT):
        assert "严格区分止盈类型" in prompt
        assert "固定风险收益比不可量化" in prompt


def test_llm_postprocessor_removes_duplicate_heading_and_unsupported_claims():
    raw = """
### 6. 研究员观察候选
以下为候选观察。
### 研究员观察候选
| 股票 | LLM观察 | 依据 |
|------|------|------|
| NVDA | 风险收益比极佳，系统比值为 1:2.00；模型另算 1:9 | 止损较近 |
"""
    plan = "| 风险收益比 | 1:2.00 |"

    cleaned = _enforce_llm_compliance(raw, plan)

    assert cleaned.count("### 研究员观察候选") == 1
    assert "风险收益比极佳" not in cleaned
    assert "固定风险收益比须以代码方案" in cleaned
    assert "1:2.00" not in cleaned
    assert "1:9" not in cleaned
    assert "见代码方案的确定性计算" in cleaned


if __name__ == "__main__":
    tests = [
        test_trust_summary_blocks_on_data_quality,
        test_trust_summary_demotes_negative_expectancy,
        test_unrelated_demoted_strategy_does_not_block_current_signal,
        test_related_demoted_strategy_blocks_current_signal,
        test_trust_summary_labels_current_opportunity_history_scope,
        test_portfolio_prompt_forbids_unquantified_risk_reward_claims,
        test_llm_postprocessor_removes_duplicate_heading_and_unsupported_claims,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
