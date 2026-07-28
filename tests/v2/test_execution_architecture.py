"""V2-7 架构边界：成交层不得倒灌 V1 或未来阶段职责。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_execution_layer_does_not_import_v1_or_future_layers():
    """成交层只消费冻结合同，不依赖 V1、组合、学习、报告或 UI。"""
    forbidden = ("from backtest", "import backtest", "from portfolio", "import portfolio", "from learning", "import learning", "from reports", "import reports", "from presentation", "import presentation", "from ui", "import ui")
    for path in (ROOT / "execution").glob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert not any(token in content for token in forbidden), path


def test_execution_hard_policy_cannot_be_disabled():
    from contracts import ContractViolation, ExecutionPolicy
    import pytest

    with pytest.raises(ContractViolation):
        ExecutionPolicy(ambiguity_mode="optimistic")
    with pytest.raises(ContractViolation):
        ExecutionPolicy(max_participation="0.10")
