"""V2-7 固定成交合同构造器；所有专项测试只使用合成冻结事件。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from tradehelper_v2.contracts import (
    ConditionEvaluation, ConditionExpression, ConditionOperand, ConditionOperator,
    ConditionResult, EvidenceRequirement, IntentState, OrderIntent, OrderSide,
    OrderStyle, OperandKind, PlanAction,
    stable_hash,
)


def price_condition(operator: ConditionOperator, level: Decimal) -> ConditionExpression:
    left = ConditionOperand(OperandKind.FEATURE, "current.price", None, "price")
    right = ConditionOperand(OperandKind.CONSTANT, "level", float(level), "price")
    identity = {"operator": operator.value, "left": left, "right": right, "lower": None, "upper": None, "children": (), "evidence_requirement": EvidenceRequirement.SNAPSHOT.value, "schema_version": 1}
    return ConditionExpression(stable_hash(identity), operator, left, right, evidence_requirement=EvidenceRequirement.SNAPSHOT, reason_code="PLAN_WAITING")


def intent_for(instrument, now: datetime, *, action=PlanAction.BUY, trigger=Decimal("100"), invalidation=Decimal("90"), state=IntentState.READY, shares=Decimal("10")) -> OrderIntent:
    trigger_condition = price_condition(ConditionOperator.GT, trigger)
    invalidation_condition = price_condition(ConditionOperator.LT, invalidation)
    evaluations = tuple(sorted((
        ConditionEvaluation(trigger_condition.condition_id, ConditionResult.PENDING_EVENT, (), (), now),
        ConditionEvaluation(invalidation_condition.condition_id, ConditionResult.PENDING_EVENT, (), (), now),
    ), key=lambda item: item.condition_id))
    side = OrderSide.BUY if action in {PlanAction.BUY, PlanAction.ADD} else OrderSide.SELL
    valid, expires = now - timedelta(minutes=1), now + timedelta(days=1)
    market_rule_version = "a_rules_v1" if instrument.market.value == "A" else "us_rules_v1"
    payload = {"instrument": instrument, "scenario_id": "scenario", "strategy_bundle_id": "strategy", "risk_bundle_id": "risk", "plan_id": "plan", "decision_id": "decision", "profile": "conservative", "action": action, "quantity_intent": "open" if side is OrderSide.BUY else "full_exit", "side": side, "order_style": OrderStyle.MARKET_ON_ACTIVATION, "state": state, "requested_shares": shares, "risk_approved_shares": shares, "trigger_condition": trigger_condition, "confirmation_condition": None, "invalidation_condition": invalidation_condition, "condition_evaluations": evaluations, "trigger_level": trigger, "stop": Decimal("95"), "take_profit": Decimal("110"), "valid_from": valid, "expires_at": expires, "earliest_execution_at": valid, "account_hash": None, "valuation_id": None, "quality_hash": "a" * 64, "evidence_hash": "b" * 64, "market_rule_version": market_rule_version, "risk_policy_version": "risk", "execution_policy_version": "execution_policy_v1"}
    identifier = stable_hash(payload)
    return OrderIntent(identifier, f"{instrument.stable_key}|fixture|{identifier}", instrument, "scenario", "strategy", "risk", "plan", "decision", "conservative", action, payload["quantity_intent"], side, OrderStyle.MARKET_ON_ACTIVATION, state, shares, shares, trigger_condition, None, invalidation_condition, evaluations, trigger, Decimal("95"), Decimal("110"), valid, expires, valid, None, None, "a" * 64, "b" * 64, market_rule_version, "risk", "execution_policy_v1", now)


def rebuild_intent(intent: OrderIntent, **changes) -> OrderIntent:
    """Rebuild an immutable intent and recompute its content identity."""
    values = {
        "instrument": intent.instrument,
        "scenario_id": intent.scenario_id,
        "strategy_bundle_id": intent.strategy_bundle_id,
        "risk_bundle_id": intent.risk_bundle_id,
        "plan_id": intent.plan_id,
        "decision_id": intent.decision_id,
        "profile": intent.profile,
        "action": intent.action,
        "quantity_intent": intent.quantity_intent,
        "side": intent.side,
        "order_style": intent.order_style,
        "state": intent.state,
        "requested_shares": intent.requested_shares,
        "risk_approved_shares": intent.risk_approved_shares,
        "trigger_condition": intent.trigger_condition,
        "confirmation_condition": intent.confirmation_condition,
        "invalidation_condition": intent.invalidation_condition,
        "condition_evaluations": intent.condition_evaluations,
        "trigger_level": intent.trigger_level,
        "stop": intent.stop,
        "take_profit": intent.take_profit,
        "valid_from": intent.valid_from,
        "expires_at": intent.expires_at,
        "earliest_execution_at": intent.earliest_execution_at,
        "account_hash": intent.account_hash,
        "valuation_id": intent.valuation_id,
        "quality_hash": intent.quality_hash,
        "evidence_hash": intent.evidence_hash,
        "market_rule_version": intent.market_rule_version,
        "risk_policy_version": intent.risk_policy_version,
        "execution_policy_version": intent.execution_policy_version,
    }
    generated_at = changes.pop("generated_at", intent.generated_at)
    values.update(changes)
    identifier = stable_hash(values)
    return OrderIntent(
        intent_id=identifier,
        event_key=f"{values['instrument'].stable_key}|fixture|{identifier}",
        generated_at=generated_at,
        **values,
    )
