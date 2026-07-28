"""RiskOfficer：为每个冻结 TradePlan/profile 生成不可变的 ExecutionDecision。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from contracts import (
    ConstraintResult, DecisionDisposition, EvidenceStatus, ExecutionDecision, ExecutionLevel,
    MarketEligibility, PlanAction, PlanReadiness, PositionState, QuantityIntent, RiskAdjustment,
    RiskConstraintKind, RiskDecisionBundle, RiskProfile, RiskRequest, StrategyFamily, stable_hash,
)
from contracts.enums import DecisionMode, QualityStatus
from .market_rules import precheck
from .sizing import cash_required, entry_capacity_detail, friction_reserve, planned_loss, round_lot_down


_ENTRY_ACTIONS = {PlanAction.BUY, PlanAction.ADD}
_EXIT_ACTIONS = {PlanAction.REDUCE, PlanAction.SELL}
_PROTECTIVE = {StrategyFamily.PROTECTIVE_EXIT, StrategyFamily.PROFIT_LOCK, StrategyFamily.FAILED_REBOUND_EXIT}


class RiskOfficer:
    """纯内存的单计划风控官；绝不发明、删除或改写策略计划。"""

    def assess(self, request: RiskRequest, *, generated_at: datetime | None = None) -> RiskDecisionBundle:
        generated_at = generated_at or datetime.now(timezone.utc)
        plans = {plan.plan_id: plan for branch in (request.strategy_bundle.entry_or_add, request.strategy_bundle.reduce_or_exit, request.strategy_bundle.hold) for plan in branch.plans}
        decisions = []
        for plan in sorted(plans.values(), key=lambda item: item.plan_id):
            for profile in sorted((RiskProfile(item.value) for item in plan.profiles), key=lambda item: item.value):
                decisions.append(self._assess_plan(request, plan, profile, generated_at))
        decision_ids = tuple(item.decision_id for item in decisions)
        protective = tuple(sorted(item.decision_id for item in decisions if plans[item.plan_id].family in _PROTECTIVE))
        identity = {"scenario_id": request.trading_scenario.scenario_id, "strategy_bundle_id": request.strategy_bundle.bundle_id,
                    "decision_ids": tuple(sorted(decision_ids)), "account_hash": stable_hash(request.account_snapshot) if request.account_snapshot else None,
                    "valuation_id": request.valuation.valuation_id if request.valuation else None,
                    "quality_hash": request.trading_scenario.quality_hash, "market_rule_version": request.market_rules.rule_version,
                    "risk_policy_version": request.policy.policy_version}
        bundle_id = stable_hash(identity)
        session = request.trading_scenario.decision_session.session_date.isoformat() if request.trading_scenario.decision_session else "calendar-unavailable"
        return RiskDecisionBundle(bundle_id, f"{request.instrument.stable_key}|{session}|{bundle_id}", request.instrument,
                                  request.trading_scenario.scenario_id, request.strategy_bundle.bundle_id,
                                  request.strategy_bundle.position_state, tuple(decisions),
                                  tuple(sorted(item.decision_id for item in decisions if item.profile is RiskProfile.CONSERVATIVE)),
                                  tuple(sorted(item.decision_id for item in decisions if item.profile is RiskProfile.AGGRESSIVE)), protective,
                                  stable_hash(request.account_snapshot) if request.account_snapshot else None,
                                  request.valuation.valuation_id if request.valuation else None,
                                  request.trading_scenario.quality_hash, request.market_rules.rule_version,
                                  request.policy.policy_version, generated_at)

    def _assess_plan(self, request, plan, profile, generated_at):
        if plan.expires_at is not None and request.as_of >= plan.expires_at:
            reasons = ["RISK_PLAN_EXPIRED"]
            if plan.action in _EXIT_ACTIONS:
                reasons.append("RISK_EXIT_PRESERVED")
                if plan.family in _PROTECTIVE: reasons.append("RISK_PROTECTIVE_EXIT_PRIORITY")
            state = self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, self._evidence(request, plan, profile),
                                MarketEligibility.RECHECK_REQUIRED, reasons,
                                [ConstraintResult("RISK_PLAN_EXPIRED", RiskConstraintKind.HARD, False)], [])
            return self._decision(request, plan, profile, generated_at, **state)
        if plan.action in _ENTRY_ACTIONS and (plan.stop is None or plan.stop.level is None):
            state = self._state(
                ExecutionLevel.D, DecisionDisposition.REJECTED, self._evidence(request, plan, profile),
                MarketEligibility.RECHECK_REQUIRED, ["RISK_ENTRY_STOP_MISSING"],
                [ConstraintResult("RISK_ENTRY_STOP_MISSING", RiskConstraintKind.HARD, False)], [],
            )
            return self._decision(request, plan, profile, generated_at, **state)
        if plan.readiness is PlanReadiness.NOT_APPLICABLE:
            state = self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, self._evidence(request, plan, profile),
                                MarketEligibility.RECHECK_REQUIRED, ["RISK_PLAN_OBSERVATION_ONLY"],
                                [ConstraintResult("RISK_PLAN_OBSERVATION_ONLY", RiskConstraintKind.HARD, False)], [])
            return self._decision(request, plan, profile, generated_at, **state)
        if plan.readiness is PlanReadiness.OBSERVATION_ONLY and plan.action is not PlanAction.WATCH:
            reasons = ["RISK_PLAN_OBSERVATION_ONLY"]
            if plan.action in _EXIT_ACTIONS:
                reasons.append("RISK_EXIT_PRESERVED")
                if plan.family in _PROTECTIVE: reasons.append("RISK_PROTECTIVE_EXIT_PRIORITY")
            state = self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, self._evidence(request, plan, profile),
                                MarketEligibility.RECHECK_REQUIRED, reasons, [], [])
            return self._decision(request, plan, profile, generated_at, **state)
        if plan.action in _ENTRY_ACTIONS:
            state = self._entry(request, plan, profile)
        elif plan.action in _EXIT_ACTIONS:
            state = self._exit(request, plan, profile)
        elif plan.action is PlanAction.HOLD:
            state = self._hold(request, plan, profile)
        else:
            state = self._watch(request, plan, profile)
        return self._decision(request, plan, profile, generated_at, **state)

    def _entry(self, request, plan, profile):
        reasons, hard, soft = [], [], []
        evidence = self._evidence(request, plan, profile)
        eligibility = precheck(request.market_rules, request.market_state, plan.action)
        reasons.extend(eligibility.reasons)
        if plan.readiness is PlanReadiness.OBSERVATION_ONLY:
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, evidence, eligibility.eligibility, reasons + ["RISK_PLAN_OBSERVATION_ONLY"], hard, soft)
        if plan.stop is None or plan.stop.level is None:
            hard.append(ConstraintResult("RISK_ENTRY_STOP_MISSING", RiskConstraintKind.HARD, False))
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility, reasons + ["RISK_ENTRY_STOP_MISSING"], hard, soft)
        if request.data_quality.status is QualityStatus.BLOCKED or request.data_quality.block_new_entries:
            hard.append(ConstraintResult("RISK_DATA_BLOCKED", RiskConstraintKind.HARD, False))
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility, reasons + ["RISK_DATA_BLOCKED"], hard, soft)
        if eligibility.eligibility is MarketEligibility.BLOCKED:
            code = eligibility.reasons[0] if eligibility.reasons else "RISK_MARKET_RECHECK_REQUIRED"
            hard.append(ConstraintResult(code, RiskConstraintKind.HARD, False))
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility, reasons, hard, soft)
        if evidence is EvidenceStatus.CONFLICTING:
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility, reasons + ["RISK_EVIDENCE_CONFLICT"], hard, soft)
        if evidence is EvidenceStatus.NEGATIVE:
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, evidence, eligibility.eligibility, reasons + ["RISK_NEGATIVE_EXPECTANCY"], hard, soft)
        if request.account_snapshot is None or request.valuation is None:
            hard.append(ConstraintResult("RISK_ACCOUNT_MISSING", RiskConstraintKind.HARD, False))
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, evidence, eligibility.eligibility, reasons + ["RISK_ACCOUNT_MISSING"], hard, soft)
        if request.valuation.status.value != "complete":
            hard.append(ConstraintResult("RISK_VALUATION_INCOMPLETE", RiskConstraintKind.HARD, False))
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, evidence, eligibility.eligibility, reasons + ["RISK_VALUATION_INCOMPLETE"], hard, soft)
        if request.valuation.equity == 0:
            hard.append(ConstraintResult("RISK_EQUITY_ZERO", RiskConstraintKind.HARD, False, Decimal("0"), Decimal("0")))
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, evidence, eligibility.eligibility, reasons + ["RISK_EQUITY_ZERO"], hard, soft)
        level = ExecutionLevel.A if evidence is EvidenceStatus.RELIABLE_POSITIVE else ExecutionLevel.B
        if evidence is not EvidenceStatus.RELIABLE_POSITIVE: reasons.append("RISK_SMALL_SAMPLE")
        if request.data_quality.status is not QualityStatus.OK:
            # 数据降级是可审计的软闸门：可以缩容量，但不能把它包装成 A 级。
            level = ExecutionLevel.B
            reasons.append("RISK_DATA_DEGRADED")
        if "FORECAST_STOCK_NONINFERIOR" in request.trading_scenario.reason_codes:
            level = ExecutionLevel.B
            reasons.append("RISK_FORECAST_NONINFERIOR_CAP")
        multiplier = Decimal("1")
        if level is ExecutionLevel.B: multiplier *= request.policy.b_level_multiplier; soft.append(RiskAdjustment("RISK_SMALL_SAMPLE", request.policy.b_level_multiplier))
        if "COUNTERTREND_ONLY" in plan.reason_codes:
            multiplier *= request.policy.countertrend_multiplier; level = ExecutionLevel.B; reasons.append("RISK_COUNTERTREND_CAP"); soft.append(RiskAdjustment("RISK_COUNTERTREND_CAP", request.policy.countertrend_multiplier))
        quality = Decimal(str(request.data_quality.max_position_multiplier))
        if quality < 1: multiplier *= quality; reasons.append("RISK_QUALITY_MULTIPLIER_APPLIED"); soft.append(RiskAdjustment("RISK_QUALITY_MULTIPLIER_APPLIED", quality))
        multiplier *= eligibility.liquidity_multiplier
        if eligibility.liquidity_multiplier < 1: soft.append(RiskAdjustment(eligibility.reasons[0], eligibility.liquidity_multiplier))
        trigger = Decimal(str(plan.trigger_level.value)) if plan.trigger_level else None
        stop = Decimal(str(plan.stop.level.value))
        executable_reference = None
        if request.market_state and request.market_state.freshness_status.value == "fresh": executable_reference = request.market_state.ask or request.market_state.current_price
        if trigger is None or trigger <= 0:
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility, reasons + ["RISK_ENTRY_STOP_INVALID"], hard, soft)
        entry = max(trigger, executable_reference) if plan.readiness is PlanReadiness.TRIGGERED and executable_reference else trigger
        if stop >= entry:
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility, reasons + ["RISK_ENTRY_STOP_INVALID"], hard, soft, entry=entry, stop=stop)
        position = next((item for item in request.valuation.position_values if item.instrument == request.instrument), None)
        current_value = position.market_value if position else Decimal("0")
        existing_shares = position.shares if position else Decimal("0")
        if plan.action is PlanAction.ADD and position is None:
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility, reasons + ["RISK_ACCOUNT_POSITION_MISMATCH"], hard, soft, entry=entry, stop=stop)
        if plan.action is PlanAction.ADD and position and position.position_pct is not None and Decimal(str(position.position_pct)) >= request.policy.single_position_hard_cap:
            reasons.append("RISK_SINGLE_POSITION_CAP")
            if Decimal(str(position.position_pct)) >= request.policy.concentration_redline: reasons.append("RISK_CONCENTRATION_REDLINE")
            hard.append(ConstraintResult("RISK_SINGLE_POSITION_CAP", RiskConstraintKind.HARD, False, request.valuation.equity * request.policy.single_position_hard_cap, current_value))
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, evidence, eligibility.eligibility, reasons, hard, soft, entry=entry, stop=stop, current_value=current_value)
        invested = request.valuation.invested_value or Decimal("0")
        if invested >= request.valuation.equity * request.policy.total_stock_hard_cap:
            hard.append(ConstraintResult("RISK_TOTAL_STOCK_CAP", RiskConstraintKind.HARD, False, request.valuation.equity * request.policy.total_stock_hard_cap, invested))
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, evidence, eligibility.eligibility, reasons + ["RISK_TOTAL_STOCK_CAP"], hard, soft, entry=entry, stop=stop, current_value=current_value)
        risk_pct = request.policy.conservative_risk_pct if profile is RiskProfile.CONSERVATIVE else request.policy.aggressive_risk_pct
        target = request.policy.conservative_target_cap if profile is RiskProfile.CONSERVATIVE else request.policy.aggressive_target_cap
        budget = request.valuation.equity * risk_pct * multiplier
        capacity = entry_capacity_detail(equity=request.valuation.equity, cash=request.valuation.cash, invested=invested, current_value=current_value, existing_shares=existing_shares, entry=entry, stop=stop, risk_budget=budget, target_cap=target, rules=request.market_rules, is_add=plan.action is PlanAction.ADD)
        shares = capacity.shares
        if shares <= 0:
            reason_codes = capacity.binding_reasons or ("RISK_MIN_LOT_EXCEEDS_CAPACITY",)
            for code in reason_codes: hard.append(ConstraintResult(code, RiskConstraintKind.HARD, False))
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, evidence, eligibility.eligibility, reasons + list(reason_codes), hard, soft, entry=entry, stop=stop, current_value=current_value, budget=budget)
        loss = planned_loss(shares, entry, stop, request.market_rules); friction = friction_reserve(shares, entry, stop, request.market_rules)
        total_loss = loss + (existing_shares * (entry - stop) if plan.action is PlanAction.ADD else Decimal("0"))
        post_value = current_value + shares * entry
        post_pct = float(post_value / request.valuation.equity)
        hard.extend((
            ConstraintResult("RISK_HARD_CONSTRAINT_IMMUTABLE", RiskConstraintKind.HARD, True),
            ConstraintResult("RISK_BUDGET_EXHAUSTED", RiskConstraintKind.HARD, total_loss <= budget, budget, total_loss),
            ConstraintResult("RISK_CASH_INSUFFICIENT", RiskConstraintKind.HARD, cash_required(shares, entry, request.market_rules) <= request.valuation.cash, request.valuation.cash, cash_required(shares, entry, request.market_rules)),
            ConstraintResult("RISK_SINGLE_POSITION_CAP", RiskConstraintKind.HARD, post_value <= request.valuation.equity * request.policy.single_position_hard_cap, request.valuation.equity * request.policy.single_position_hard_cap, post_value),
            ConstraintResult("RISK_TOTAL_STOCK_CAP", RiskConstraintKind.HARD, invested + shares * entry <= request.valuation.equity * request.policy.total_stock_hard_cap, request.valuation.equity * request.policy.total_stock_hard_cap, invested + shares * entry),
            ConstraintResult("RISK_MIN_LOT_EXCEEDS_CAPACITY", RiskConstraintKind.HARD, shares >= request.market_rules.lot_size, request.market_rules.lot_size, shares),
        ))
        if Decimal(str(post_pct)) >= request.policy.concentration_warning: reasons.append("RISK_CONCENTRATION_WARNING")
        reasons += ["RISK_FRICTION_RESERVE_INCLUDED", "RISK_GAP_LOSS_CAN_EXCEED_PLAN", "RISK_PORTFOLIO_ALLOCATION_PENDING", "RISK_HARD_CONSTRAINT_IMMUTABLE"]
        if plan.readiness is PlanReadiness.WAITING or eligibility.eligibility is MarketEligibility.RECHECK_REQUIRED or (plan.readiness is PlanReadiness.TRIGGERED and executable_reference is None):
            return self._state(level, DecisionDisposition.CONDITIONALLY_APPROVED, evidence, eligibility.eligibility, reasons + ["RISK_CONDITIONALLY_APPROVED"], hard, soft, shares=shares, entry=entry, stop=stop, current_value=current_value, post_value=post_value, post_pct=post_pct, budget=budget, loss=loss, total_loss=total_loss, friction=friction, recheck=True)
        return self._state(level, DecisionDisposition.APPROVED_NOW, evidence, eligibility.eligibility, reasons + ["RISK_APPROVED"], hard, soft, shares=shares, entry=entry, stop=stop, current_value=current_value, post_value=post_value, post_pct=post_pct, budget=budget, loss=loss, total_loss=total_loss, friction=friction, executable=True)

    def _exit(self, request, plan, profile):
        reasons = ["RISK_EXIT_PRESERVED", "RISK_PROTECTIVE_EXIT_PRIORITY"] if plan.family in _PROTECTIVE else ["RISK_EXIT_PRESERVED"]
        eligibility = precheck(request.market_rules, request.market_state, plan.action); reasons.extend(eligibility.reasons)
        evidence = self._evidence(request, plan, profile)
        position = request.account_snapshot and next((item for item in request.account_snapshot.positions if item.instrument == request.instrument), None)
        if position is None:
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility, reasons + ["RISK_ACCOUNT_POSITION_MISMATCH"], [ConstraintResult("RISK_ACCOUNT_POSITION_MISMATCH", RiskConstraintKind.HARD, False)], [])
        if eligibility.eligibility is MarketEligibility.BLOCKED:
            code = eligibility.reasons[0] if eligibility.reasons else "RISK_MARKET_RECHECK_REQUIRED"
            return self._state(
                ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility,
                reasons + ([] if code in reasons else [code]),
                [ConstraintResult(code, RiskConstraintKind.HARD, False)], [], blocked=position.shares,
            )
        availability = request.position_availability
        sellable = availability.sellable_shares if availability else (None if request.market_rules.same_day_sell_restricted else position.shares)
        if sellable is None:
            if plan.readiness is PlanReadiness.WAITING:
                return self._state(ExecutionLevel.B, DecisionDisposition.CONDITIONALLY_APPROVED, evidence, MarketEligibility.RECHECK_REQUIRED, reasons + ["RISK_POSITION_AVAILABILITY_UNKNOWN", "RISK_CONDITIONALLY_APPROVED"], [], [], recheck=True)
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, MarketEligibility.RECHECK_REQUIRED, reasons + ["RISK_POSITION_AVAILABILITY_UNKNOWN", "RISK_T1_BLOCKED"], [ConstraintResult("RISK_T1_BLOCKED", RiskConstraintKind.HARD, False)], [])
        reduce_fraction = request.policy.conservative_reduce_fraction if profile is RiskProfile.CONSERVATIVE else request.policy.aggressive_reduce_fraction
        desired = position.shares if plan.action is PlanAction.SELL else round_lot_down(position.shares * reduce_fraction, request.market_rules.lot_size)
        approved = min(desired, sellable)
        if plan.action is PlanAction.REDUCE:
            approved = round_lot_down(approved, request.market_rules.lot_size)
        blocked = (position.shares - approved) if plan.action is PlanAction.SELL else max(Decimal("0"), desired - approved)
        if desired <= 0:
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, evidence, eligibility.eligibility,
                               reasons + ["RISK_MIN_LOT_EXCEEDS_CAPACITY"],
                               [ConstraintResult("RISK_MIN_LOT_EXCEEDS_CAPACITY", RiskConstraintKind.HARD, False, request.market_rules.lot_size, Decimal("0"))], [])
        if blocked > 0:
            reasons.append("RISK_PARTIAL_SELLABLE")
            eligibility = eligibility.__class__(MarketEligibility.PARTIALLY_ELIGIBLE, eligibility.reasons, eligibility.liquidity_multiplier)
        if plan.readiness is PlanReadiness.WAITING or eligibility.eligibility is MarketEligibility.RECHECK_REQUIRED:
            return self._state(ExecutionLevel.B, DecisionDisposition.CONDITIONALLY_APPROVED, evidence, eligibility.eligibility, reasons + ["RISK_CONDITIONALLY_APPROVED"], [], [], shares=approved, blocked=blocked, recheck=True)
        if approved <= 0:
            code = "RISK_T1_BLOCKED"
            rejected_shares = position.shares if plan.action is PlanAction.SELL else blocked
            return self._state(ExecutionLevel.D, DecisionDisposition.REJECTED, evidence, eligibility.eligibility, reasons + ([] if code in reasons else [code]), [ConstraintResult(code, RiskConstraintKind.HARD, False)], [], blocked=rejected_shares)
        level = ExecutionLevel.A if plan.action is PlanAction.SELL and blocked == 0 else ExecutionLevel.B
        return self._state(level, DecisionDisposition.APPROVED_NOW, evidence, eligibility.eligibility, reasons + ["RISK_APPROVED"], [], [], shares=approved, blocked=blocked, executable=True)

    def _hold(self, request, plan, profile):
        triggered_protective = any(item.action in _EXIT_ACTIONS and item.readiness is PlanReadiness.TRIGGERED for item in request.strategy_bundle.reduce_or_exit.plans)
        if triggered_protective:
            return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, self._evidence(request, plan, profile), MarketEligibility.RECHECK_REQUIRED, ["RISK_PROTECTIVE_EXIT_PRIORITY"], [], [])
        return self._state(ExecutionLevel.B, DecisionDisposition.NO_ORDER_REQUIRED, self._evidence(request, plan, profile), MarketEligibility.ELIGIBLE, ["RISK_APPROVED"], [], [])

    def _watch(self, request, plan, profile):
        return self._state(ExecutionLevel.C, DecisionDisposition.OBSERVE, self._evidence(request, plan, profile), MarketEligibility.RECHECK_REQUIRED, ["RISK_PLAN_OBSERVATION_ONLY"], [], [])

    @staticmethod
    def _matching_evidence(request, plan, profile):
        return [item for item in request.evidence if item.instrument == plan.instrument and item.strategy_id == plan.strategy_id and item.strategy_version == plan.strategy_version and item.parameter_hash == plan.parameter_hash and item.profile in {None, profile}]

    def _evidence(self, request, plan, profile):
        matches = self._matching_evidence(request, plan, profile)
        if not matches: return EvidenceStatus.UNAVAILABLE
        statuses = {item.status for item in matches}
        return EvidenceStatus.CONFLICTING if len(statuses) > 1 else matches[-1].status

    @staticmethod
    def _state(level, disposition, evidence, eligibility, reasons, hard, soft, *, shares=Decimal("0"), blocked=Decimal("0"), entry=None, stop=None, current_value=None, post_value=None, post_pct=None, budget=None, loss=None, total_loss=None, friction=None, executable=False, recheck=False):
        return dict(level=level, disposition=disposition, evidence=evidence, eligibility=eligibility, reasons=tuple(reasons), hard=tuple(hard), soft=tuple(soft), shares=shares, blocked=blocked, entry=entry, stop=stop, current_value=current_value, post_value=post_value, post_pct=post_pct, budget=budget, loss=loss, total_loss=total_loss, friction=friction, executable=executable, recheck=recheck)

    def _decision(self, request, plan, profile, generated_at, **state):
        account_hash = stable_hash(request.account_snapshot) if request.account_snapshot else None
        evidence_matches = [item.evidence_id for item in self._matching_evidence(request, plan, profile)]
        evidence_hash = stable_hash(tuple(sorted(evidence_matches)))
        current_pct = float(state["current_value"] / request.valuation.equity) if state["current_value"] is not None and request.valuation and request.valuation.equity else None
        max_loss = state["total_loss"] if plan.action is PlanAction.ADD else state["loss"]
        hard = tuple(sorted(state["hard"], key=lambda item: (item.kind.value, item.code))); soft = tuple(sorted(state["soft"], key=lambda item: item.code)); reasons = tuple(sorted(set(state["reasons"])))
        identity = {"plan_id": plan.plan_id, "bundle_id": request.strategy_bundle.bundle_id, "profile": profile, "level": state["level"], "disposition": state["disposition"], "approved_shares": state["shares"], "blocked_shares": state["blocked"], "entry": state["entry"], "stop": state["stop"], "position": (state["current_value"], current_pct, state["post_value"], state["post_pct"]), "risk": (state["budget"], state["loss"], state["total_loss"], max_loss, state["friction"]), "eligibility": state["eligibility"], "evidence_status": state["evidence"], "account_hash": account_hash, "valuation_id": request.valuation.valuation_id if request.valuation else None, "quality_hash": request.trading_scenario.quality_hash, "evidence_hash": evidence_hash, "market_rule_version": request.market_rules.rule_version, "risk_policy_version": request.policy.policy_version, "hard": hard, "soft": soft, "reasons": reasons, "valid_from": plan.valid_from, "expires_at": plan.expires_at}
        decision_id = stable_hash(identity); session = request.trading_scenario.decision_session.session_date.isoformat() if request.trading_scenario.decision_session else "calendar-unavailable"
        return ExecutionDecision(decision_id, f"{request.instrument.stable_key}|{session}|{plan.plan_id}|{profile.value}|{decision_id}", request.instrument, request.trading_scenario.scenario_id, request.strategy_bundle.bundle_id, plan.plan_id, profile, plan.action, plan.quantity_intent, state["level"], state["disposition"], state["executable"], state["recheck"], state["shares"], state["blocked"], state["entry"], state["stop"], state["current_value"], current_pct, state["post_value"], state["post_pct"], state["budget"], state["loss"], state["total_loss"], max_loss, state["friction"], state["eligibility"], state["evidence"], hard, soft, reasons, plan.valid_from, plan.expires_at, account_hash, request.valuation.valuation_id if request.valuation else None, request.trading_scenario.quality_hash, evidence_hash, request.market_rules.rule_version, request.policy.policy_version, generated_at)
