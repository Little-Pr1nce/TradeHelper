from contracts import ConditionExpression, ConditionOperand, ConditionOperator, ConditionResult, EvidenceRequirement
from strategies.conditions import evaluate


def test_sp00_missing_condition_is_unknown(now):
    condition = ConditionExpression("", ConditionOperator.GTE, ConditionOperand("feature", "closed.ma_20", None, None), ConditionOperand("constant", "1", 1, None))
    assert evaluate(condition, {}, now).result is ConditionResult.UNKNOWN


def test_sp12_crossing_waits_for_event_sequence(now):
    condition = ConditionExpression("", ConditionOperator.CROSSES_BELOW, ConditionOperand("feature", "current.price", None, None), ConditionOperand("constant", "1", 1, None), evidence_requirement=EvidenceRequirement.EVENT_SEQUENCE)
    assert evaluate(condition, {"current.price": 0.5}, now).result is ConditionResult.PENDING_EVENT
