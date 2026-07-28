"""V2-7 EX00--EX09：OrderIntent 工厂与三时段订单语义。"""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from risk_helpers import request_for
from contracts import DecisionMode, ExecutionPolicy, IntentBuildStatus, Market, stable_hash
from contracts.scenario import DecisionSession
from data.calendar import StaticTradingCalendar
from execution import OrderIntentFactory
from risk import RiskOfficer


def _plans(request, bundle):
    return {plan.plan_id: plan for branch in (request.strategy_bundle.entry_or_add, request.strategy_bundle.reduce_or_exit, request.strategy_bundle.hold, request.strategy_bundle.invalidation) for plan in branch.plans if any(decision.plan_id == plan.plan_id for decision in bundle.decisions)}


def test_every_risk_decision_has_auditable_build_record(us_instrument, calendar, now):
    request = request_for(us_instrument, as_of=now)
    risks = RiskOfficer().assess(request, generated_at=now)
    result = OrderIntentFactory(calendar).build_bundle(risks, _plans(request, risks), {}, now, ExecutionPolicy())
    assert {record.decision_id for record in result.records} == {decision.decision_id for decision in risks.decisions}
    assert any(record.status is IntentBuildStatus.NO_ORDER for record in result.records)
    assert all(intent.requested_shares <= intent.risk_approved_shares for intent in result.intents)
    assert all(intent.order_style.value == "market_on_activation" for intent in result.intents)


def test_requested_shares_can_only_shrink_and_round_down(us_instrument, calendar, now):
    request = request_for(us_instrument, as_of=now)
    risks = RiskOfficer().assess(request, generated_at=now)
    target = next(item for item in risks.decisions if item.approved_shares > 1)
    result = OrderIntentFactory(calendar).build_bundle(risks, _plans(request, risks), {target.decision_id: Decimal("1.9")}, now, ExecutionPolicy())
    intent = next(item for item in result.intents if item.decision_id == target.decision_id)
    assert intent.requested_shares == Decimal("1")


def test_eod_uses_injected_next_session_open(us_instrument, now):
    next_day = date(2026, 7, 13)
    open_at = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)
    close_at = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
    cal = StaticTradingCalendar((now.date(), next_day), windows={(Market.US, us_instrument.exchange, next_day): DecisionSession(Market.US, us_instrument.exchange, next_day, open_at, close_at, (), "fixture")})
    request = request_for(us_instrument, as_of=now)
    risks = RiskOfficer().assess(request, generated_at=now)
    result = OrderIntentFactory(cal).build_bundle(risks, _plans(request, risks), {}, now, ExecutionPolicy(), decision_mode=DecisionMode.EOD)
    assert all(intent.state.value == "staged" and intent.earliest_execution_at == open_at for intent in result.intents)
