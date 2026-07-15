"""模板共享的轻量数据结构和 DSL 构造器。"""
from __future__ import annotations

from dataclasses import dataclass

from tradehelper_v2.contracts import (
    ConditionExpression, ConditionOperand, ConditionOperator, ContractViolation,
    EvidenceRequirement, PlanAction, TakeProfitMode,
)


def feature(name: str) -> ConditionOperand:
    return ConditionOperand("feature", name, None, None, (name,))


def constant(value: float | int | bool) -> ConditionOperand:
    return ConditionOperand("constant", str(value), value, None, ())


def level(name: str, value: float, *sources: str) -> ConditionOperand:
    return ConditionOperand("derived_level", name, value, "price", tuple(sources))


def compare(left, operator: ConditionOperator, right, reason: str) -> ConditionExpression:
    return ConditionExpression("", operator, left, right, reason_code=reason)


def all_of(*children: ConditionExpression, reason: str) -> ConditionExpression:
    return ConditionExpression("", ConditionOperator.ALL, children=children, reason_code=reason)


def crossing(left, operator: ConditionOperator, right, reason: str) -> ConditionExpression:
    return ConditionExpression("", operator, left, right, evidence_requirement=EvidenceRequirement.EVENT_SEQUENCE, reason_code=reason)


def always_false(reason: str = "PLAN_WAITING") -> ConditionExpression:
    return compare(constant(False), ConditionOperator.EQUALS, constant(True), reason)


def parameter(spec, name: str) -> float:
    value = spec.parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolation(f"strategy parameter {name} must be numeric")
    return float(value)


@dataclass(frozen=True, slots=True)
class Proposal:
    """模板输出的业务骨架；身份和求值统一由 StrategyEngine 生成。"""
    action: PlanAction
    trigger: ConditionExpression
    confirmation: ConditionExpression | None
    trigger_price: float | None
    trigger_code: str | None
    stop_price: float | None
    stop_code: str | None
    take_price: float | None
    take_mode: TakeProfitMode
    take_condition: ConditionExpression | None
    invalidation: ConditionExpression
    hold: ConditionExpression | None
    reason_codes: tuple[str, ...]
    evidence_features: tuple[str, ...]
    profiles: tuple[str, ...] = ("conservative", "aggressive")
    explicit_missing_conditions: tuple[str, ...] = ()
