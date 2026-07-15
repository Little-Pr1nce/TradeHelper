"""三值条件求值器；只解释 DSL，绝不执行表达式字符串。"""
from __future__ import annotations

from datetime import datetime
from typing import Mapping

from tradehelper_v2.contracts.analysis import FeatureStatus, FeatureValue
from tradehelper_v2.contracts.market_data import ContractViolation
from tradehelper_v2.contracts.strategy import (
    ConditionEvaluation, ConditionExpression, ConditionOperator, ConditionResult,
    EvidenceRequirement, ObservedValue, OperandKind,
)


def _read(operand, values: Mapping[str, object]) -> tuple[object, ObservedValue | None, str | None]:
    if operand.kind is not OperandKind.FEATURE:
        return operand.value, None, None
    raw = values.get(operand.key)
    if isinstance(raw, FeatureValue):
        observed = ObservedValue(raw.name, raw.value, raw.status, raw.available_at)
        return raw.value, observed, raw.name if raw.status is not FeatureStatus.AVAILABLE else None
    if isinstance(raw, tuple) and len(raw) == 3:
        value, status, available_at = raw
        observed = ObservedValue(operand.key, value, status, available_at)
        return value, observed, operand.key if value is None else None
    observed = ObservedValue(operand.key, raw, ConditionResult.UNKNOWN if raw is None else ConditionResult.TRUE, None)
    return raw, observed, operand.key if raw is None else None


def evaluate(expression: ConditionExpression, values: Mapping[str, object], as_of: datetime) -> ConditionEvaluation:
    """Evaluate only facts frozen in ``values`` at the supplied strategy timestamp."""
    if expression.evidence_requirement is not EvidenceRequirement.SNAPSHOT:
        return ConditionEvaluation(expression.condition_id, ConditionResult.PENDING_EVENT, (), (), as_of)
    operator = expression.operator
    if operator in {ConditionOperator.ALL, ConditionOperator.ANY, ConditionOperator.NOT}:
        children = tuple(evaluate(child, values, as_of) for child in expression.children)
        child_results = tuple(child.result for child in children)
        observations = tuple(value for child in children for value in child.observed_values)
        missing = tuple(sorted({name for child in children for name in child.missing_features}))
        if operator is ConditionOperator.ALL:
            if ConditionResult.FALSE in child_results:
                result = ConditionResult.FALSE
            elif ConditionResult.UNKNOWN in child_results:
                result = ConditionResult.UNKNOWN
            elif ConditionResult.PENDING_EVENT in child_results:
                result = ConditionResult.PENDING_EVENT
            elif ConditionResult.NOT_APPLICABLE in child_results:
                result = ConditionResult.NOT_APPLICABLE
            else:
                result = ConditionResult.TRUE
        elif operator is ConditionOperator.ANY:
            if ConditionResult.TRUE in child_results:
                result = ConditionResult.TRUE
            elif ConditionResult.UNKNOWN in child_results:
                result = ConditionResult.UNKNOWN
            elif ConditionResult.PENDING_EVENT in child_results:
                result = ConditionResult.PENDING_EVENT
            elif ConditionResult.NOT_APPLICABLE in child_results:
                result = ConditionResult.NOT_APPLICABLE
            else:
                result = ConditionResult.FALSE
        else:
            result = {
                ConditionResult.TRUE: ConditionResult.FALSE,
                ConditionResult.FALSE: ConditionResult.TRUE,
                ConditionResult.UNKNOWN: ConditionResult.UNKNOWN,
                ConditionResult.PENDING_EVENT: ConditionResult.PENDING_EVENT,
                ConditionResult.NOT_APPLICABLE: ConditionResult.NOT_APPLICABLE,
            }[child_results[0]]
        return ConditionEvaluation(expression.condition_id, result, observations, missing, as_of)

    left, left_observed, left_missing = _read(expression.left, values)
    operands = [left_observed] if left_observed else []
    missing = [left_missing] if left_missing else []
    if operator is ConditionOperator.BETWEEN:
        lower, lower_observed, lower_missing = _read(expression.lower, values)
        upper, upper_observed, upper_missing = _read(expression.upper, values)
        operands.extend(value for value in (lower_observed, upper_observed) if value)
        missing.extend(value for value in (lower_missing, upper_missing) if value)
        if missing:
            return ConditionEvaluation(expression.condition_id, ConditionResult.UNKNOWN, tuple(operands), tuple(missing), as_of)
        result = ConditionResult.TRUE if lower <= left <= upper else ConditionResult.FALSE
        return ConditionEvaluation(expression.condition_id, result, tuple(operands), (), as_of)
    right, right_observed, right_missing = _read(expression.right, values)
    if right_observed:
        operands.append(right_observed)
    if right_missing:
        missing.append(right_missing)
    if missing:
        return ConditionEvaluation(expression.condition_id, ConditionResult.UNKNOWN, tuple(operands), tuple(missing), as_of)
    if operator is ConditionOperator.GT:
        matched = left > right
    elif operator is ConditionOperator.GTE:
        matched = left >= right
    elif operator is ConditionOperator.LT:
        matched = left < right
    elif operator is ConditionOperator.LTE:
        matched = left <= right
    elif operator is ConditionOperator.EQUALS:
        matched = left == right
    else:  # Crossing operators returned before reaching snapshot evaluation.
        raise ContractViolation(f"unsupported snapshot operator: {operator}")
    return ConditionEvaluation(expression.condition_id, ConditionResult.TRUE if matched else ConditionResult.FALSE, tuple(operands), (), as_of)
