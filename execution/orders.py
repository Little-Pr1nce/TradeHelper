"""将冻结的策略与风控决定转换为订单意图。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Mapping

from contracts.enums import DecisionMode, Market
from contracts.execution import (EXECUTION_REASON_CODES, ExecutionPolicy, IntentBuildStatus, IntentState, OrderIntent, OrderIntentBuildRecord, OrderIntentBundle, OrderIntentRequest, OrderSide, OrderStyle)
from contracts.market_data import ContractViolation, stable_hash
from contracts.risk import DecisionDisposition, ExecutionLevel, RiskDecisionBundle
from contracts.strategy import PlanAction, TradePlan
from data.calendar import TradingCalendar


class OrderIntentFactory:
    """只允许把 V2-6 的批准量缩小为订单意图，绝不扩大。"""

    def __init__(self, calendar: TradingCalendar) -> None:
        self._calendar = calendar

    @staticmethod
    def _lot(plan: TradePlan) -> Decimal:
        return Decimal("100") if plan.instrument.market is Market.A else Decimal("1")

    def _earliest(self, plan: TradePlan, requested_at: datetime, mode: DecisionMode | None) -> datetime:
        # EOD 必须通过注入的交易所日历取得下一会话开盘，不能猜工作日。
        if mode is DecisionMode.EOD:
            session = self._calendar.next_session(plan.instrument.market, plan.instrument.exchange, requested_at.date())
            return self._calendar.session_window(plan.instrument.market, plan.instrument.exchange, session).regular_open
        return plan.valid_from

    def build(self, request: OrderIntentRequest, *, decision_mode: DecisionMode | None = None) -> tuple[OrderIntent | None, OrderIntentBuildRecord]:
        plan, decision = request.trade_plan, request.execution_decision
        no_order_reason = None
        if decision.level is ExecutionLevel.C: no_order_reason = "EXEC_NO_ORDER_LEVEL_C"
        elif decision.level is ExecutionLevel.D: no_order_reason = "EXEC_NO_ORDER_LEVEL_D"
        elif plan.action not in {PlanAction.BUY, PlanAction.ADD, PlanAction.REDUCE, PlanAction.SELL}: no_order_reason = "EXEC_NO_ORDER_ACTION"
        elif decision.disposition not in {DecisionDisposition.APPROVED_NOW, DecisionDisposition.CONDITIONALLY_APPROVED} or decision.approved_shares <= 0: no_order_reason = "EXEC_NO_APPROVED_SHARES"
        if no_order_reason:
            build_id = stable_hash({"decision_id": decision.decision_id, "plan_id": plan.plan_id, "status": IntentBuildStatus.NO_ORDER, "intent_id": None, "reasons": (no_order_reason,)})
            return None, OrderIntentBuildRecord(build_id, decision.decision_id, plan.plan_id, IntentBuildStatus.NO_ORDER, None, (no_order_reason,), request.requested_at)
        lot = self._lot(plan)
        # A 股全量 SELL 必须保留零股尾数；买入、加仓和部分减仓仍严格整手。
        full_a_exit = plan.instrument.market is Market.A and plan.action is PlanAction.SELL and request.requested_shares == decision.approved_shares
        requested = request.requested_shares if full_a_exit else (request.requested_shares / lot).to_integral_value(rounding=ROUND_DOWN) * lot
        if requested <= 0:
            build_id = stable_hash({"decision_id": decision.decision_id, "plan_id": plan.plan_id, "status": IntentBuildStatus.NO_ORDER, "intent_id": None, "reasons": ("EXEC_NO_APPROVED_SHARES",)})
            return None, OrderIntentBuildRecord(build_id, decision.decision_id, plan.plan_id, IntentBuildStatus.NO_ORDER, None, ("EXEC_NO_APPROVED_SHARES",), request.requested_at)
        valid, expires = plan.valid_from, plan.expires_at
        if valid is None or expires is None: raise ContractViolation("actionable plan needs a frozen validity window")
        if request.requested_at >= expires:
            build_id = stable_hash({"decision_id": decision.decision_id, "plan_id": plan.plan_id, "status": IntentBuildStatus.NO_ORDER, "intent_id": None, "reasons": ("EXEC_PLAN_EXPIRED",)})
            return None, OrderIntentBuildRecord(build_id, decision.decision_id, plan.plan_id, IntentBuildStatus.NO_ORDER, None, ("EXEC_PLAN_EXPIRED",), request.requested_at)
        earliest = self._earliest(plan, request.requested_at, decision_mode)
        state = IntentState.READY if decision.disposition is DecisionDisposition.APPROVED_NOW and decision_mode is not DecisionMode.EOD else IntentState.STAGED
        side = OrderSide.BUY if plan.action in {PlanAction.BUY, PlanAction.ADD} else OrderSide.SELL
        trigger = Decimal(str(plan.trigger_level.value)) if plan.trigger_level else None
        stop = Decimal(str(plan.stop.level.value)) if plan.stop and plan.stop.level else None
        target = Decimal(str(plan.take_profit.level.value)) if plan.take_profit and plan.take_profit.level else None
        payload = {"instrument": plan.instrument, "scenario_id": plan.scenario_id, "strategy_bundle_id": decision.bundle_id, "risk_bundle_id": request.risk_decision_bundle.risk_bundle_id, "plan_id": plan.plan_id, "decision_id": decision.decision_id, "profile": decision.profile, "action": plan.action, "quantity_intent": plan.quantity_intent, "side": side, "order_style": OrderStyle.MARKET_ON_ACTIVATION, "state": state, "requested_shares": requested, "risk_approved_shares": decision.approved_shares, "trigger_condition": plan.trigger_condition, "confirmation_condition": plan.confirmation_condition, "invalidation_condition": plan.invalidation_condition, "condition_evaluations": plan.evaluations, "trigger_level": trigger, "stop": stop, "take_profit": target, "valid_from": valid, "expires_at": expires, "earliest_execution_at": earliest, "account_hash": decision.account_hash, "valuation_id": decision.valuation_id, "quality_hash": decision.quality_hash, "evidence_hash": decision.evidence_hash, "market_rule_version": decision.market_rule_version, "risk_policy_version": decision.risk_policy_version, "execution_policy_version": request.execution_policy.policy_version}
        intent_id = stable_hash(payload)
        event_key = f"{plan.instrument.stable_key}|{plan.event_key.split('|')[1]}|{intent_id}"
        intent = OrderIntent(intent_id, event_key, plan.instrument, plan.scenario_id, decision.bundle_id, request.risk_decision_bundle.risk_bundle_id, plan.plan_id, decision.decision_id, decision.profile, plan.action, plan.quantity_intent, side, OrderStyle.MARKET_ON_ACTIVATION, state, requested, decision.approved_shares, plan.trigger_condition, plan.confirmation_condition, plan.invalidation_condition, plan.evaluations, trigger, stop, target, valid, expires, earliest, decision.account_hash, decision.valuation_id, decision.quality_hash, decision.evidence_hash, decision.market_rule_version, decision.risk_policy_version, request.execution_policy.policy_version, request.requested_at)
        reasons = ("EXEC_INTENT_CREATED",) + (("EXEC_REQUESTED_SHARES_REDUCED",) if requested != decision.approved_shares else ()) + (("EXEC_LOT_ROUNDED",) if requested != request.requested_shares else ())
        build_id = stable_hash({"decision_id": decision.decision_id, "plan_id": plan.plan_id, "status": IntentBuildStatus.CREATED, "intent_id": intent.intent_id, "reasons": tuple(sorted(reasons))})
        return intent, OrderIntentBuildRecord(build_id, decision.decision_id, plan.plan_id, IntentBuildStatus.CREATED, intent.intent_id, reasons, request.requested_at)

    def build_bundle(self, risk_decision_bundle: RiskDecisionBundle, plans_by_id: Mapping[str, TradePlan], requested_shares_by_decision_id: Mapping[str, Decimal | None], requested_at: datetime, execution_policy: ExecutionPolicy, *, decision_mode: DecisionMode | None = None) -> OrderIntentBundle:
        decisions = risk_decision_bundle.decisions
        if len({item.decision_id for item in decisions}) != len(decisions) or set(requested_shares_by_decision_id) - {item.decision_id for item in decisions}:
            raise ContractViolation("requested share keys do not match risk decisions")
        if set(plans_by_id) != {item.plan_id for item in decisions}:
            raise ContractViolation("plans do not exactly match risk decisions")
        records, intents = [], []
        for decision in decisions:
            plan = plans_by_id.get(decision.plan_id)
            if plan is None: raise ContractViolation("missing trade plan for risk decision")
            requested = requested_shares_by_decision_id.get(decision.decision_id)
            # 组合层必须显式传 0，禁止回退成单票 approved_shares。
            if requested == 0:
                reason = "EXEC_PORTFOLIO_NOT_ALLOCATED" if decision.approved_shares > 0 else "EXEC_NO_APPROVED_SHARES"
                build_id = stable_hash({"decision_id": decision.decision_id, "plan_id": plan.plan_id, "status": IntentBuildStatus.NO_ORDER, "intent_id": None, "reasons": (reason,)})
                intent, record = None, OrderIntentBuildRecord(build_id, decision.decision_id, plan.plan_id, IntentBuildStatus.NO_ORDER, None, (reason,), requested_at)
            else:
                intent, record = self.build(OrderIntentRequest(plan, decision, risk_decision_bundle, requested, requested_at, execution_policy), decision_mode=decision_mode)
            records.append(record)
            if intent: intents.append(intent)
        bundle_id = stable_hash({"risk_bundle_id": risk_decision_bundle.risk_bundle_id, "records": tuple(sorted(records, key=lambda item: item.decision_id)), "intent_ids": tuple(sorted(item.intent_id for item in intents))})
        return OrderIntentBundle(bundle_id, risk_decision_bundle.risk_bundle_id, tuple(records), tuple(intents), requested_at)
