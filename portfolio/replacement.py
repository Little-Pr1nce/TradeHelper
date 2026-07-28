"""只产出替换研究候选，不把卖出回笼资金自动串成买入订单。"""
from __future__ import annotations

from decimal import Decimal

from contracts import (
    AllocationStatus, DecisionDisposition, EvidenceStatus, ExecutionLevel,
    PlanAction, PortfolioRole, ReplacementCandidate, ReplacementStatus, stable_hash,
)
from risk.sizing import cash_required


def build_replacement_candidates(profile_decision, batch, generated_at):
    """从既有独立退出与未充分分配 entry 配对，且强制要求事后重算。"""
    if profile_decision.batch_id != batch.batch_id:
        raise ValueError("replacement batch mismatch")
    candidates = {item.candidate_id: item for item in batch.candidates}
    valuations = {item.instrument: item for item in batch.valuation.position_values}
    exits = [item for item in profile_decision.allocations
             if item.status in {AllocationStatus.ALLOCATED_NOW, AllocationStatus.RESERVED_CONDITIONAL,
                                AllocationStatus.SHARED_EXIT_RESERVATION}
             and item.action in {PlanAction.SELL, PlanAction.REDUCE}
             and item.final_requested_shares > 0]
    targets = [item for item in profile_decision.allocations
               if item.action is PlanAction.BUY
               and item.status in {AllocationStatus.BLOCKED, AllocationStatus.ALLOCATED_NOW,
                                   AllocationStatus.RESERVED_CONDITIONAL}
               and item.final_requested_shares < item.approved_shares
               and item.reference_entry_price is not None
               and set(item.binding_constraints) & {"PORTFOLIO_CASH_LIMITED", "PORTFOLIO_TOTAL_EXPOSURE_LIMITED",
                                                    "PORTFOLIO_ZERO_CAPACITY"}]
    result = []
    for target in targets:
        target_candidate = candidates[target.candidate_id]
        decision = target_candidate.execution_decision
        evidence_status = target_candidate.plan_evidence.status if target_candidate.plan_evidence else decision.evidence_status
        if (target_candidate.role is not PortfolioRole.WATCHLIST
                or decision.level not in {ExecutionLevel.A, ExecutionLevel.B}
                or decision.disposition not in {DecisionDisposition.APPROVED_NOW, DecisionDisposition.CONDITIONALLY_APPROVED}
                or evidence_status in {EvidenceStatus.NEGATIVE, EvidenceStatus.CONFLICTING}
                or decision.entry_price is None or decision.stop_price is None
                or decision.entry_price <= decision.stop_price or target.approved_shares <= 0):
            continue
        required = cash_required(target.approved_shares, decision.entry_price, target_candidate.market_rules)
        shortfall = max(required - profile_decision.reservation_snapshot.remaining_cash, Decimal("0"))
        if shortfall <= 0:
            continue
        for source in exits:
            if source.instrument == target.instrument:
                continue
            valued = valuations.get(source.instrument)
            if valued is None:
                continue
            release = source.final_requested_shares * valued.price
            reasons = ("PORTFOLIO_REPLACEMENT_RESEARCH_ONLY",)
            identity = {"profile": profile_decision.profile, "source": source.instrument, "source_exit": source.allocation_id,
                        "target": target.instrument, "target_entry": target.allocation_id,
                        "status": ReplacementStatus.RESEARCH_AFTER_EXIT, "release": release,
                        "required": required, "shortfall": shortfall, "reasons": reasons}
            result.append(ReplacementCandidate(
                stable_hash(identity), profile_decision.profile, source.instrument, source.allocation_id,
                target.instrument, target.allocation_id, ReplacementStatus.RESEARCH_AFTER_EXIT,
                source.reason_codes, target.rank_components, release, required, shortfall, True, reasons,
                generated_at,
            ))
            break
    return tuple(sorted(result, key=lambda item: item.replacement_id))
