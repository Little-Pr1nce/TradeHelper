"""可审计字典序排序；不从文本长度或自然语言评分推导优先级。"""
from __future__ import annotations

from decimal import Decimal

from tradehelper_v2.contracts import DecisionDisposition, ExecutionLevel, EvidenceStatus, MarketEligibility, PlanAction


_LEVEL = {ExecutionLevel.A: 0, ExecutionLevel.B: 1, ExecutionLevel.C: 2, ExecutionLevel.D: 3}
_DISPOSITION = {DecisionDisposition.APPROVED_NOW: 0, DecisionDisposition.CONDITIONALLY_APPROVED: 1,
                DecisionDisposition.NO_ORDER_REQUIRED: 2, DecisionDisposition.OBSERVE: 3, DecisionDisposition.REJECTED: 4}
_ELIGIBILITY = {MarketEligibility.ELIGIBLE: 0, MarketEligibility.PARTIALLY_ELIGIBLE: 1,
                MarketEligibility.RECHECK_REQUIRED: 2, MarketEligibility.BLOCKED: 3}
_EVIDENCE = {EvidenceStatus.RELIABLE_POSITIVE: 0, EvidenceStatus.POSITIVE_UNCERTAIN: 1,
             EvidenceStatus.INSUFFICIENT_SAMPLE: 2, EvidenceStatus.UNAVAILABLE: 3,
             EvidenceStatus.NEGATIVE: 4, EvidenceStatus.CONFLICTING: 5}


def rank_components(candidate):
    """返回可持久化的结构化排序分量，最后以 decision_id 稳定打破平局。"""
    decision, evidence = candidate.execution_decision, candidate.plan_evidence
    position = decision.current_position_pct
    planned = decision.planned_position_value
    loss = decision.incremental_planned_loss
    friction = decision.friction_reserve
    loss_ratio = None if not planned or planned <= 0 or loss is None else loss / planned
    friction_ratio = None if not planned or planned <= 0 or friction is None else friction / planned
    return (
        ("level", decision.level.value), ("disposition", decision.disposition.value),
        ("evidence", evidence.status.value if evidence else EvidenceStatus.UNAVAILABLE.value),
        ("current_position_pct", "" if position is None else str(position)),
        ("confidence_low", "" if not evidence or evidence.confidence_low is None else str(evidence.confidence_low)),
        ("expected_net_return", "" if not evidence or evidence.expected_net_return is None else str(evidence.expected_net_return)),
        ("win_rate", "" if not evidence or evidence.win_rate is None else str(evidence.win_rate)),
        ("loss_ratio", "" if loss_ratio is None else str(loss_ratio)),
        ("friction_ratio", "" if friction_ratio is None else str(friction_ratio)),
        ("decision_id", decision.decision_id),
    )


def rank_holdings(candidates, protective_decision_ids=()):
    protective = set(protective_decision_ids)
    def key(candidate):
        decision, plan = candidate.execution_decision, candidate.trade_plan
        protective_rank = 0 if decision.decision_id in protective and plan.action in {PlanAction.SELL, PlanAction.REDUCE} else 1
        action_rank = {PlanAction.SELL: 0, PlanAction.REDUCE: 1, PlanAction.HOLD: 2, PlanAction.ADD: 3}.get(plan.action, 4)
        return (protective_rank, action_rank, _DISPOSITION[decision.disposition], _LEVEL[decision.level],
                _ELIGIBILITY[decision.market_eligibility], -(decision.current_position_pct or 0),
                -(decision.max_loss_amount or Decimal("0")), decision.decision_id)
    return tuple(sorted(candidates, key=key))


def rank_entries(candidates):
    """A/B entry 主候选的完全确定字典序；C/D 不因指标好看进入资金池。"""
    def key(candidate):
        decision, evidence = candidate.execution_decision, candidate.plan_evidence
        value = evidence.status if evidence else EvidenceStatus.UNAVAILABLE
        confidence = evidence.confidence_low if evidence and evidence.confidence_low is not None else float("-inf")
        expected = evidence.expected_net_return if evidence and evidence.expected_net_return is not None else float("-inf")
        win_rate = evidence.win_rate if evidence and evidence.win_rate is not None else float("-inf")
        planned = decision.planned_position_value
        loss_ratio = (decision.incremental_planned_loss / planned if planned and planned > 0 and decision.incremental_planned_loss is not None else Decimal("Infinity"))
        friction_ratio = (decision.friction_reserve / planned if planned and planned > 0 and decision.friction_reserve is not None else Decimal("Infinity"))
        return (_LEVEL[decision.level], _DISPOSITION[decision.disposition], _EVIDENCE[value],
                decision.current_position_pct if decision.current_position_pct is not None else float("inf"),
                -confidence, -expected, -win_rate, loss_ratio, friction_ratio, decision.decision_id)
    return tuple(sorted(candidates, key=key))
