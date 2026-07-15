"""当前订单预览；它永远不声称已经发生历史成交。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from tradehelper_v2.contracts.enums import DecisionMode, FreshnessStatus, Market
from tradehelper_v2.contracts.execution import (CurrentOrderPreview, ExecutionEvidenceGrade, ExecutionPolicy, ExecutionState, OrderIntent, PreviewStatus)
from tradehelper_v2.contracts.market_data import ContractViolation, ensure_utc, stable_hash
from tradehelper_v2.contracts.risk import MarketRuleSet, MarketState
from .costs import CostModel
from .market_rules import ExecutionMarketRules


class CurrentPreviewBuilder:
    def __init__(self, policy: ExecutionPolicy) -> None: self.policy = policy

    def build(self, intent: OrderIntent, state: ExecutionState, market_state: MarketState | None, rules: MarketRuleSet, liquidity, observed_at: datetime) -> CurrentOrderPreview:
        observed_at = ensure_utc(observed_at, "preview observed_at")
        if state.market is not intent.instrument.market or rules.market is not intent.instrument.market or rules.exchange is not intent.instrument.exchange or rules.rule_version != intent.market_rule_version or self.policy.policy_version != intent.execution_policy_version:
            raise ContractViolation("preview identities are inconsistent")
        if intent.account_hash is not None and state.account_hash != intent.account_hash:
            raise ContractViolation("preview state does not match the approved account")
        if state.captured_at > observed_at or liquidity.cutoff_at > observed_at:
            raise ContractViolation("preview contains future state or liquidity evidence")
        if not (rules.effective_from <= observed_at and (rules.effective_to is None or observed_at < rules.effective_to)):
            raise ContractViolation("preview market rules are not effective")
        if market_state is not None and (market_state.instrument != intent.instrument or market_state.observed_at > observed_at):
            raise ContractViolation("preview quote does not belong to the order instrument or time")
        reasons: list[str] = ["EXEC_CURRENT_PREVIEW_ONLY"]
        status = PreviewStatus.STAGED if intent.state.value == "staged" else PreviewStatus.READY
        price = None if market_state is None else market_state.current_price
        grade = ExecutionEvidenceGrade.INSUFFICIENT
        maximum = Decimal("0")
        low = high = costs = None
        if observed_at >= intent.expires_at:
            status = PreviewStatus.EXPIRED; reasons.append("EXEC_PLAN_EXPIRED")
        elif market_state is None or market_state.freshness_status is not FreshnessStatus.FRESH or price is None:
            status = PreviewStatus.RECHECK_REQUIRED; reasons.append("EXEC_FRESH_QUOTE_REQUIRED")
        elif market_state.mode is DecisionMode.EOD:
            status = PreviewStatus.STAGED
        elif intent.instrument.market is Market.A and market_state.mode is DecisionMode.PRE:
            status = PreviewStatus.STAGED
        else:
            grade = ExecutionEvidenceGrade.LOW
            if market_state.bid is None or market_state.ask is None: reasons.append("EXEC_NO_LEVEL2_DEPTH")
            from tradehelper_v2.contracts.execution import ExecutionEvent, EventGranularity, TradingStatus
            event = ExecutionEvent("preview", intent.instrument, observed_at.date(), observed_at, observed_at, EventGranularity.QUOTE, price, price, price, price, market_state.volume, market_state.previous_close, market_state.bid, market_state.ask, TradingStatus.OPEN, market_state.source, "preview", observed_at, observed_at)
            check = ExecutionMarketRules.check(intent, state, event, rules)
            if check.outcome:
                status = PreviewStatus.REJECTED if check.outcome.value == "rejected" else PreviewStatus.RECHECK_REQUIRED; reasons.extend(check.reason_codes)
            else:
                maximum = check.permitted_shares
                estimate = CostModel.estimate(side=intent.side, raw_price=price, requested_shares=maximum, market_rules=rules, policy=self.policy, liquidity=liquidity, event_at=observed_at, evidence_grade=grade)
                low = high = estimate.fill_price; costs = estimate.total_fee; maximum = estimate.fillable_shares; reasons.extend(estimate.reason_codes); grade = estimate.evidence_grade
        reason_tuple = tuple(sorted(set(reasons)))
        identifier = stable_hash({"intent_id": intent.intent_id, "status": status, "reference_price": price, "estimated_fill_low": low, "estimated_fill_high": high, "estimated_costs": costs, "requested": intent.requested_shares, "maximum": maximum, "grade": grade, "reasons": reason_tuple, "observed_at": observed_at})
        return CurrentOrderPreview(identifier, intent.intent_id, status, price, low, high, costs, intent.requested_shares, maximum, grade, reason_tuple, observed_at, observed_at)
