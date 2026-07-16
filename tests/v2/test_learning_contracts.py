"""LE00-LE09：学习合同的哈希、枚举和不可变政策。"""
from decimal import Decimal
import pytest
from tradehelper_v2.contracts import ContractViolation, LearningPolicy

def test_le00_learning_policy_is_frozen_and_hash_stable():
    assert LearningPolicy()==LearningPolicy()
    with pytest.raises(ContractViolation): LearningPolicy(min_reliable_samples=29)
