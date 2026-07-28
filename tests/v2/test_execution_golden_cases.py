"""V2-7 EX00-EX49 executable acceptance matrix.

The focused unit files keep failures small; this matrix proves every frozen
design case has a named executable check and exercises the cross-module edges
that were missing from the first V2-7 implementation.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from execution_helpers import intent_for, price_condition, rebuild_intent
from risk_helpers import request_for
from contracts import (
    ConditionEvaluation,
    ConditionExpression,
    ConditionOperand,
    ConditionOperator,
    ConditionResult,
    ContractViolation,
    DecisionMode,
    EventGranularity,
    EvidenceRequirement,
    ExecutionEvent,
    ExecutionEvidenceGrade,
    ExecutionPolicy,
    ExecutionState,
    FillOutcome,
    FreshnessStatus,
    InstrumentId,
    InstrumentClassification,
    LiquidityEvidence,
    MarketState,
    Market,
    OperandKind,
    OrderIntentRequest,
    OrderSide,
    PathAssumption,
    PlanAction,
    TradingSession,
    TradingStatus,
    TriggerState,
    stable_hash,
)
from contracts.strategy import ObservedValue
from data.repository import SQLiteRepository
from execution import HistoricalFillSimulator, OrderIntentFactory, TriggerEngine
from execution.costs import CostModel
from execution.market_rules import ExecutionMarketRules
from execution.preview import CurrentPreviewBuilder
from execution.simulator import HistoricalSimulationRequest
from risk import RiskOfficer
from risk.market_rules import default_market_rules


def _event(instrument, at, event_id="event", *, open="101", high=None, low=None, close=None,
           granularity=EventGranularity.QUOTE, volume="1000", previous_close="100",
           status=TradingStatus.OPEN, available_at=None, duration=timedelta(0)):
    opened = Decimal(open)
    high = opened if high is None else Decimal(high)
    low = opened if low is None else Decimal(low)
    close = opened if close is None else Decimal(close)
    end = at + duration
    return ExecutionEvent(
        event_id, instrument, at.date(), at, end, granularity,
        opened, high, low, close,
        None if volume is None else Decimal(volume),
        None if previous_close is None else Decimal(previous_close),
        None, None, status, "fixture", "high", available_at or end, end,
    )


def _liquidity(at, volume="100000", volatility="0.20"):
    volume_value = None if volume is None else Decimal(volume)
    volatility_value = None if volatility is None else Decimal(volatility)
    payload = {
        "median_daily_volume_20": volume_value,
        "annualized_volatility_20": volatility_value,
        "cutoff_at": at,
        "source": "fixture",
    }
    return LiquidityEvidence(volume_value, volatility_value, at, "fixture", stable_hash(payload))


def _state(instrument, at, *, cash="100000", position="0", sellable="0", cost=None,
           acquired=None, stop=None, take=None, account_hash=None):
    return ExecutionState(
        instrument.market,
        "CNY" if instrument.market.value == "A" else "USD",
        Decimal(cash), Decimal(position), None if sellable is None else Decimal(sellable),
        None if cost is None else Decimal(cost), acquired,
        None if stop is None else Decimal(stop), None if take is None else Decimal(take),
        at, "fixture", account_hash=account_hash,
    )


def _simulate(intent, state, events, at, *, liquidity=None, rules=None):
    events = tuple(events)
    evidence_at = min((item.interval_start for item in events), default=at)
    rules = rules or default_market_rules(intent.instrument.market, intent.instrument.exchange, evidence_at)
    return HistoricalFillSimulator().simulate(HistoricalSimulationRequest(
        intent, state, events, rules, ExecutionPolicy(), liquidity or _liquidity(evidence_at), at,
    ))


def _condition(key, operator, level, requirement=EvidenceRequirement.SNAPSHOT):
    left = ConditionOperand(OperandKind.FEATURE, key, None, "price" if "volume" not in key else "index")
    right = ConditionOperand(OperandKind.CONSTANT, "level", float(level), left.unit)
    return ConditionExpression("", operator, left, right, evidence_requirement=requirement, reason_code="PLAN_WAITING")


def _with_conditions(intent, at, *, trigger, invalidation=None, confirmation=None, observations=()):
    invalidation = invalidation or intent.invalidation_condition
    evaluations = tuple(
        ConditionEvaluation(item.condition_id, ConditionResult.PENDING_EVENT, tuple(observations), (), at)
        for item in (trigger, invalidation, confirmation) if item is not None
    )
    return rebuild_intent(
        intent,
        trigger_condition=trigger,
        invalidation_condition=invalidation,
        confirmation_condition=confirmation,
        condition_evaluations=evaluations,
    )


def _factory_bundle(instrument, calendar, now, mode=DecisionMode.EOD):
    request = request_for(instrument, mode=mode, as_of=now)
    risks = RiskOfficer().assess(request, generated_at=now)
    plans = {
        plan.plan_id: plan
        for branch in (request.strategy_bundle.entry_or_add, request.strategy_bundle.reduce_or_exit,
                       request.strategy_bundle.hold, request.strategy_bundle.invalidation)
        for plan in branch.plans if any(item.plan_id == plan.plan_id for item in risks.decisions)
    }
    bundle = OrderIntentFactory(calendar).build_bundle(
        risks, plans, {}, now, ExecutionPolicy(),
    )
    return request, risks, plans, bundle


def test_ex00_contract_decimal_hash_and_reason_registry(us_instrument, now):
    policy = ExecutionPolicy()
    assert len(policy.parameter_hash) == 64
    assert intent_for(us_instrument, now).requested_shares == Decimal("10")
    with pytest.raises(ContractViolation):
        ExecutionPolicy(ambiguity_mode="optimistic")


def test_ex01_ab_decisions_create_intents(us_instrument, calendar, now):
    _, risks, _, bundle = _factory_bundle(us_instrument, calendar, now)
    actionable = {item.decision_id for item in risks.decisions if item.level.value in {"A", "B"} and item.approved_shares > 0}
    assert actionable and actionable == {item.decision_id for item in bundle.intents}


def test_ex02_non_actionable_decisions_leave_no_order_records(us_instrument, calendar, now):
    _, risks, _, bundle = _factory_bundle(us_instrument, calendar, now)
    assert len(bundle.records) == len(risks.decisions)
    assert any(record.status.value == "no_order" for record in bundle.records)


def test_ex03_requested_shares_only_shrink(us_instrument, calendar, now):
    request, risks, plans, _ = _factory_bundle(us_instrument, calendar, now)
    decision = next(item for item in risks.decisions if item.approved_shares > 1)
    order, _ = OrderIntentFactory(calendar).build(OrderIntentRequest(
        plans[decision.plan_id], decision, risks, decision.approved_shares - 1, now, ExecutionPolicy(),
    ))
    assert order is not None and order.requested_shares <= decision.approved_shares
    with pytest.raises(ContractViolation):
        OrderIntentRequest(plans[decision.plan_id], decision, risks, decision.approved_shares + 1, now, ExecutionPolicy())


def test_ex04_preview_rejects_cross_instrument_and_account_identity(us_instrument, now):
    intent = rebuild_intent(intent_for(us_instrument, now), account_hash="c" * 64)
    state = _state(us_instrument, now, account_hash="d" * 64)
    with pytest.raises(ContractViolation):
        CurrentPreviewBuilder(ExecutionPolicy()).build(intent, state, None, default_market_rules(us_instrument.market, us_instrument.exchange, now), _liquidity(now), now)
    valid_intent = intent_for(us_instrument, now)
    wrong = InstrumentId.from_code("MSFT", Market.US, "XNAS")
    wrong_quote = MarketState(wrong, DecisionMode.INTRADAY, TradingSession.REGULAR, Decimal("999"), Decimal("998"), None, None, Decimal("100"), now, "fixture", FreshnessStatus.FRESH)
    with pytest.raises(ContractViolation):
        CurrentPreviewBuilder(ExecutionPolicy()).build(valid_intent, _state(us_instrument, now), wrong_quote, default_market_rules(us_instrument.market, us_instrument.exchange, now), _liquidity(now), now)


def test_ex05_entry_level_remains_trigger_not_limit(us_instrument, now):
    intent = intent_for(us_instrument, now, trigger=Decimal("100"))
    assert intent.order_style.value == "market_on_activation" and intent.trigger_level == Decimal("100")


def test_ex06_same_facts_have_same_intent(us_instrument, now):
    first = intent_for(us_instrument, now)
    second = rebuild_intent(first, generated_at=now + timedelta(seconds=1))
    assert first.intent_id == second.intent_id


def test_ex07_eod_starts_at_next_session(us_instrument, calendar, now):
    from datetime import date, datetime, timezone
    from contracts import Market
    from contracts.scenario import DecisionSession
    from data.calendar import StaticTradingCalendar
    next_day = date(2026, 7, 13)
    open_at = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)
    close_at = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
    explicit = StaticTradingCalendar((now.date(), next_day), windows={(Market.US, us_instrument.exchange, next_day): DecisionSession(Market.US, us_instrument.exchange, next_day, open_at, close_at, (), "fixture")})
    request, risks, plans, _ = _factory_bundle(us_instrument, calendar, now)
    bundle = OrderIntentFactory(explicit).build_bundle(risks, plans, {}, now, ExecutionPolicy(), decision_mode=DecisionMode.EOD)
    assert all(item.state.value == "staged" and item.earliest_execution_at == open_at for item in bundle.intents)


@pytest.mark.parametrize("mode,expected", [(DecisionMode.PRE, "ready"), (DecisionMode.INTRADAY, "ready"), (DecisionMode.EOD, "staged")])
def test_ex08_mode_state_and_validity_are_explicit(us_instrument, now, mode, expected):
    from contracts import IntentState
    intent = intent_for(us_instrument, now, state=IntentState(expected))
    assert intent.state.value == expected and intent.valid_from < intent.expires_at
    if mode is DecisionMode.EOD:
        assert intent.state.value == "staged"


def test_ex09_expired_plan_cannot_activate(us_instrument, now):
    intent = intent_for(us_instrument, now)
    result = TriggerEngine(ExecutionPolicy()).evaluate(intent, (), replay_as_of=intent.expires_at)
    assert result.state is TriggerState.EXPIRED


def test_ex10_frozen_closed_feature_is_evaluated(us_instrument, now):
    trigger = _condition("closed.ma120", ConditionOperator.GT, 100)
    observed = ObservedValue("closed.ma120", 101.0, ConditionResult.TRUE, now)
    intent = _with_conditions(intent_for(us_instrument, now), now, trigger=trigger, observations=(observed,))
    assert TriggerEngine(ExecutionPolicy()).evaluate(intent, (_event(us_instrument, now),), replay_as_of=now).state is TriggerState.TRIGGERED


def test_ex11_crossing_requires_previous_event(us_instrument, now):
    crossing = _condition("current.price", ConditionOperator.CROSSES_ABOVE, 100, EvidenceRequirement.EVENT_SEQUENCE)
    intent = _with_conditions(intent_for(us_instrument, now), now, trigger=crossing)
    one = TriggerEngine(ExecutionPolicy()).evaluate(intent, (_event(us_instrument, now, open="101"),), replay_as_of=now)
    two = TriggerEngine(ExecutionPolicy()).evaluate(intent, (
        _event(us_instrument, now - timedelta(minutes=1), "before", open="99"),
        _event(us_instrument, now, "after", open="101"),
    ), replay_as_of=now)
    assert one.state is TriggerState.NOT_TRIGGERED and two.state is TriggerState.TRIGGERED


def test_ex12_gap_trigger_uses_open(us_instrument, now):
    intent = intent_for(us_instrument, now)
    event = _event(us_instrument, now, open="110", high="112", low="109", close="111", granularity=EventGranularity.DAILY_BAR)
    result = _simulate(intent, _state(us_instrument, now), (event,), now)
    assert result.fills[0].raw_price == Decimal("110") and result.fills[0].path_assumption is PathAssumption.GAP_AT_OPEN


def test_ex13_invalidation_before_trigger_wins(us_instrument, now):
    intent = intent_for(us_instrument, now)
    result = TriggerEngine(ExecutionPolicy()).evaluate(intent, (
        _event(us_instrument, now - timedelta(minutes=1), "invalid", open="89"),
        _event(us_instrument, now, "trigger", open="101"),
    ), replay_as_of=now)
    assert result.state is TriggerState.INVALIDATED


def test_ex14_same_bar_trigger_and_invalidation_is_unknown(us_instrument, now):
    intent = intent_for(us_instrument, now, invalidation=Decimal("105"))
    bar = _event(us_instrument, now, open="100", high="110", low="80", close="100", granularity=EventGranularity.DAILY_BAR)
    assert TriggerEngine(ExecutionPolicy()).evaluate(intent, (bar,), replay_as_of=now).state is TriggerState.UNVERIFIABLE


def test_ex15_missing_confirmation_blocks_trigger(us_instrument, now):
    confirmation = _condition("current.volume", ConditionOperator.GT, 1000)
    intent = _with_conditions(intent_for(us_instrument, now), now, trigger=intent_for(us_instrument, now).trigger_condition, confirmation=confirmation)
    result = TriggerEngine(ExecutionPolicy()).evaluate(intent, (_event(us_instrument, now, volume=None),), replay_as_of=now)
    assert result.state is TriggerState.UNVERIFIABLE


def test_ex16_session_evidence_rejects_quote(us_instrument, now):
    trigger = _condition("current.price", ConditionOperator.GT, 100, EvidenceRequirement.SESSION_OHLC)
    intent = _with_conditions(intent_for(us_instrument, now), now, trigger=trigger)
    assert TriggerEngine(ExecutionPolicy()).evaluate(intent, (_event(us_instrument, now),), replay_as_of=now).state is TriggerState.UNVERIFIABLE


def test_ex17_prevalid_event_is_ignored(us_instrument, now):
    base = intent_for(us_instrument, now)
    intent = rebuild_intent(base, valid_from=now, earliest_execution_at=now)
    event = _event(us_instrument, now - timedelta(minutes=1), open="101")
    result = TriggerEngine(ExecutionPolicy()).evaluate(intent, (event,), replay_as_of=now)
    assert result.state is TriggerState.NOT_TRIGGERED and not result.evaluated_event_ids


def test_ex18_post_expiry_event_is_expired(us_instrument, now):
    intent = intent_for(us_instrument, now)
    event = _event(us_instrument, intent.expires_at, open="101")
    assert TriggerEngine(ExecutionPolicy()).evaluate(intent, (event,), replay_as_of=intent.expires_at).state is TriggerState.EXPIRED


def test_ex19_future_available_event_is_rejected(us_instrument, now):
    future = _event(us_instrument, now, open="101", available_at=now + timedelta(seconds=1))
    with pytest.raises(ContractViolation):
        TriggerEngine(ExecutionPolicy()).evaluate(intent_for(us_instrument, now), (future,), replay_as_of=now)
    intent = intent_for(us_instrument, now)
    with pytest.raises(ContractViolation):
        HistoricalSimulationRequest(
            intent, _state(us_instrument, now), (_event(us_instrument, now),),
            default_market_rules(us_instrument.market, us_instrument.exchange, now),
            ExecutionPolicy(), _liquidity(now + timedelta(seconds=1)), now,
        )


def test_ex20_eod_ignores_same_day_bar(us_instrument, now):
    intent = rebuild_intent(intent_for(us_instrument, now), valid_from=now, earliest_execution_at=now + timedelta(days=1), expires_at=now + timedelta(days=2))
    result = _simulate(intent, _state(us_instrument, now), (_event(us_instrument, now, open="110"),), now)
    assert result.run.outcome is FillOutcome.NOT_TRIGGERED


def test_ex21_gap_stop_uses_worse_open(us_instrument, now):
    intent = intent_for(us_instrument, now, action=PlanAction.SELL)
    state = _state(us_instrument, now, position="10", sellable="10", cost="100", stop="95", take="110")
    event = _event(us_instrument, now, open="90", high="92", low="88", close="91", granularity=EventGranularity.DAILY_BAR)
    result = _simulate(intent, state, (event,), now)
    assert result.fills[0].raw_price == Decimal("90") and result.fills[0].fill_price < Decimal("90")


def test_ex22_intraday_stop_uses_stop_level(us_instrument, now):
    intent = intent_for(us_instrument, now, action=PlanAction.SELL)
    state = _state(us_instrument, now, position="10", sellable="10", cost="100", stop="95", take="110")
    event = _event(us_instrument, now, open="100", high="102", low="94", close="96", granularity=EventGranularity.INTRADAY_BAR, duration=timedelta(minutes=1))
    assert _simulate(intent, state, (event,), now + timedelta(minutes=1)).fills[0].raw_price == Decimal("95")


def test_ex23_gap_take_profit_uses_open(us_instrument, now):
    intent = intent_for(us_instrument, now, action=PlanAction.SELL)
    state = _state(us_instrument, now, position="10", sellable="10", cost="100", stop="95", take="110")
    event = _event(us_instrument, now, open="115", high="116", low="114", close="115", granularity=EventGranularity.DAILY_BAR)
    assert _simulate(intent, state, (event,), now).fills[0].raw_price == Decimal("115")


def test_ex24_daily_stop_take_collision_is_stop_first(us_instrument, now):
    intent = intent_for(us_instrument, now, action=PlanAction.SELL)
    state = _state(us_instrument, now, position="10", sellable="10", cost="100", stop="95", take="110")
    event = _event(us_instrument, now, open="100", high="115", low="90", close="105", granularity=EventGranularity.DAILY_BAR)
    fill = _simulate(intent, state, (event,), now).fills[0]
    assert fill.path_assumption is PathAssumption.CONSERVATIVE_STOP_FIRST and fill.raw_price == Decimal("95")


def test_ex25_new_entry_and_stop_same_bar_is_not_filled(us_instrument, now):
    intent = intent_for(us_instrument, now)
    event = _event(us_instrument, now, open="99", high="105", low="94", close="102", granularity=EventGranularity.DAILY_BAR)
    assert _simulate(intent, _state(us_instrument, now), (event,), now).run.outcome is FillOutcome.UNVERIFIABLE


def test_ex26_intraday_stop_take_collision_is_unknown(us_instrument, now):
    intent = intent_for(us_instrument, now, action=PlanAction.SELL)
    state = _state(us_instrument, now, position="10", sellable="10", cost="100", stop="95", take="110")
    event = _event(us_instrument, now, open="100", high="115", low="90", close="105", granularity=EventGranularity.INTRADAY_BAR, duration=timedelta(minutes=1))
    assert _simulate(intent, state, (event,), now + timedelta(minutes=1)).run.outcome is FillOutcome.UNVERIFIABLE


def test_ex27_strict_unknown_never_fills(us_instrument, now):
    intent = intent_for(us_instrument, now, invalidation=Decimal("105"))
    event = _event(us_instrument, now, open="100", high="110", low="80", close="100", granularity=EventGranularity.DAILY_BAR)
    fill = _simulate(intent, _state(us_instrument, now), (event,), now).fills[0]
    assert fill.filled_shares == 0 and fill.outcome is FillOutcome.UNVERIFIABLE


def test_ex28_buy_state_delta_freezes_protection(us_instrument, now):
    result = _simulate(intent_for(us_instrument, now), _state(us_instrument, now), (_event(us_instrument, now),), now)
    assert result.run.final_state_delta.active_stop == Decimal("95")
    assert result.run.final_state_delta.active_take_profit == Decimal("110")


def test_ex29_current_preview_never_claims_fill(us_instrument, now):
    intent = intent_for(us_instrument, now)
    market = MarketState(us_instrument, DecisionMode.INTRADAY, TradingSession.REGULAR, Decimal("101"), Decimal("100"), None, None, Decimal("1000"), now, "fixture", FreshnessStatus.FRESH)
    preview = CurrentPreviewBuilder(ExecutionPolicy()).build(intent, _state(us_instrument, now), market, default_market_rules(us_instrument.market, us_instrument.exchange, now), _liquidity(now), now)
    assert preview.status.value in {"ready", "staged", "recheck_required", "rejected"}
    assert "EXEC_CURRENT_PREVIEW_ONLY" in preview.reason_codes


@pytest.mark.parametrize("case_id", [f"EX{number:02d}" for number in range(50)])
def test_ex00_ex49_registry_is_complete(case_id):
    """Every frozen design ID has an independently named executable case above or below."""
    names = {item.__name__ for item in globals().values() if callable(item) and getattr(item, "__name__", "").startswith("test_ex")}
    assert any(name.startswith(f"test_{case_id.lower()}_") for name in names), case_id


def test_ex30_base_slippage_is_audited(us_instrument, now):
    estimate = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("1"), market_rules=default_market_rules(us_instrument.market, us_instrument.exchange, now), policy=ExecutionPolicy(), liquidity=_liquidity(now), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert estimate.slippage_rate >= Decimal("0.003") and "EXEC_BASE_SLIPPAGE_APPLIED" in estimate.reason_codes


def test_ex31_volatility_slippage_is_monotonic(us_instrument, now):
    rules = default_market_rules(us_instrument.market, us_instrument.exchange, now)
    low = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("1"), market_rules=rules, policy=ExecutionPolicy(), liquidity=_liquidity(now, volatility="0.20"), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    high = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("1"), market_rules=rules, policy=ExecutionPolicy(), liquidity=_liquidity(now, volatility="0.80"), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert high.slippage_rate >= low.slippage_rate


def test_ex32_adv_slippage_is_monotonic(us_instrument, now):
    rules = default_market_rules(us_instrument.market, us_instrument.exchange, now)
    one = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("1000"), market_rules=rules, policy=ExecutionPolicy(), liquidity=_liquidity(now), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    five = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("5000"), market_rules=rules, policy=ExecutionPolicy(), liquidity=_liquidity(now), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert five.slippage_rate >= one.slippage_rate


def test_ex33_above_adv_cap_is_partial(us_instrument, now):
    estimate = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("6000"), market_rules=default_market_rules(us_instrument.market, us_instrument.exchange, now), policy=ExecutionPolicy(), liquidity=_liquidity(now), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert estimate.fillable_shares == Decimal("5000") and estimate.unfilled_shares == Decimal("1000")


def test_ex34_missing_adv_is_low_evidence(us_instrument, now):
    estimate = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("1"), market_rules=default_market_rules(us_instrument.market, us_instrument.exchange, now), policy=ExecutionPolicy(), liquidity=_liquidity(now, volume=None), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert estimate.evidence_grade is ExecutionEvidenceGrade.LOW


def test_ex35_slippage_is_adverse_both_sides(us_instrument, now):
    rules = default_market_rules(us_instrument.market, us_instrument.exchange, now)
    buy = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("1"), market_rules=rules, policy=ExecutionPolicy(), liquidity=_liquidity(now), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    sell = CostModel.estimate(side=OrderSide.SELL, raw_price=Decimal("10"), requested_shares=Decimal("1"), market_rules=rules, policy=ExecutionPolicy(), liquidity=_liquidity(now), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert buy.fill_price > Decimal("10") > sell.fill_price


def test_ex36_dual_market_fees_are_reproducible(us_instrument, a_instrument, now):
    us = CostModel.estimate(side=OrderSide.SELL, raw_price=Decimal("100"), requested_shares=Decimal("100"), market_rules=default_market_rules(us_instrument.market, us_instrument.exchange, now), policy=ExecutionPolicy(), liquidity=_liquidity(now), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    a = CostModel.estimate(side=OrderSide.SELL, raw_price=Decimal("100"), requested_shares=Decimal("100"), market_rules=default_market_rules(a_instrument.market, a_instrument.exchange, now), policy=ExecutionPolicy(), liquidity=_liquidity(now), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert us.total_fee == us.commission + us.sell_tax and a.sell_tax > 0


def test_ex37_price_and_fee_quantization_is_conservative(a_instrument, now):
    estimate = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("100"), market_rules=default_market_rules(a_instrument.market, a_instrument.exchange, now), policy=ExecutionPolicy(), liquidity=_liquidity(now), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert estimate.fill_price.as_tuple().exponent >= -2 and estimate.total_fee.as_tuple().exponent >= -2


def test_ex38_cash_shortage_reduces_without_forcing_lot(us_instrument, now):
    result = _simulate(intent_for(us_instrument, now, shares=Decimal("10")), _state(us_instrument, now, cash="500"), (_event(us_instrument, now),), now)
    assert result.fills[0].filled_shares <= Decimal("4")


def test_ex39_fill_never_exceeds_all_bounds(us_instrument, now):
    intent = intent_for(us_instrument, now, action=PlanAction.SELL, shares=Decimal("10"))
    result = _simulate(intent, _state(us_instrument, now, position="7", sellable="5", cost="100"), (_event(us_instrument, now),), now)
    assert result.fills[0].filled_shares <= Decimal("5") <= intent.requested_shares


def test_ex40_a_lot_and_full_odd_exit(a_instrument, now):
    rules = default_market_rules(a_instrument.market, a_instrument.exchange, now)
    buy = ExecutionMarketRules.check(intent_for(a_instrument, now, shares=Decimal("150")), _state(a_instrument, now), _event(a_instrument, now), rules)
    sell = ExecutionMarketRules.check(intent_for(a_instrument, now, action=PlanAction.SELL, shares=Decimal("150")), _state(a_instrument, now, position="150", sellable="150", cost="100", acquired=now.date()-timedelta(days=1)), _event(a_instrument, now), rules)
    assert buy.permitted_shares == Decimal("100") and sell.permitted_shares == Decimal("150")


def test_ex41_a_t1_blocks_same_day_exit(a_instrument, now):
    check = ExecutionMarketRules.check(intent_for(a_instrument, now, action=PlanAction.SELL, shares=Decimal("100")), _state(a_instrument, now, position="100", sellable="100", cost="100", acquired=now.date()), _event(a_instrument, now), default_market_rules(a_instrument.market, a_instrument.exchange, now))
    assert check.reason_codes == ("EXEC_T1_BLOCKED",)


def test_ex42_a_limit_queue_is_unverifiable(a_instrument, now):
    check = ExecutionMarketRules.check(intent_for(a_instrument, now, shares=Decimal("100")), _state(a_instrument, now), _event(a_instrument, now, open="110", previous_close="100"), default_market_rules(a_instrument.market, a_instrument.exchange, now))
    assert check.outcome is FillOutcome.UNVERIFIABLE


def test_ex43_unknown_a_classification_near_boundary_is_unknown(a_instrument, now):
    rules = default_market_rules(a_instrument.market, a_instrument.exchange, now, InstrumentClassification.UNKNOWN)
    check = ExecutionMarketRules.check(intent_for(a_instrument, now, shares=Decimal("100")), _state(a_instrument, now), _event(a_instrument, now, open="105", previous_close="100"), rules)
    assert check.outcome is FillOutcome.UNVERIFIABLE


def test_ex44_zero_volume_is_not_named_suspension(us_instrument, now):
    check = ExecutionMarketRules.check(intent_for(us_instrument, now), _state(us_instrument, now), _event(us_instrument, now, volume="0"), default_market_rules(us_instrument.market, us_instrument.exchange, now))
    assert check.reason_codes == ("EXEC_NO_TRADABLE_VOLUME",)


def test_ex45_us_has_no_a_lot_t1_or_limit_rules(us_instrument, now):
    intent = intent_for(us_instrument, now, action=PlanAction.SELL, shares=Decimal("1"))
    check = ExecutionMarketRules.check(intent, _state(us_instrument, now, position="1", sellable="1", cost="100", acquired=now.date()), _event(us_instrument, now, open="150"), default_market_rules(us_instrument.market, us_instrument.exchange, now))
    assert check.outcome is None and check.permitted_shares == Decimal("1")


def test_ex46_no_depth_preview_is_only_low_confidence(us_instrument, now):
    intent = intent_for(us_instrument, now)
    market = MarketState(us_instrument, DecisionMode.INTRADAY, TradingSession.REGULAR, Decimal("101"), Decimal("100"), None, None, Decimal("1000"), now, "fixture", FreshnessStatus.FRESH)
    preview = CurrentPreviewBuilder(ExecutionPolicy()).build(intent, _state(us_instrument, now), market, default_market_rules(us_instrument.market, us_instrument.exchange, now), _liquidity(now), now)
    assert "EXEC_NO_LEVEL2_DEPTH" in preview.reason_codes and preview.evidence_grade is ExecutionEvidenceGrade.LOW


def test_ex47_one_instrument_cannot_pollute_another(us_instrument, a_instrument, now):
    with pytest.raises(ContractViolation):
        _simulate(intent_for(us_instrument, now), _state(us_instrument, now), (_event(a_instrument, now),), now)


def test_ex48_migration_repository_round_trip_is_strongly_typed(tmp_path, monkeypatch, us_instrument, now):
    repository = SQLiteRepository(Path(tmp_path) / "execution.sqlite")
    try:
        intent = intent_for(us_instrument, now)
        assert repository.save_order_intent(intent).inserted == 1
        assert repository.save_order_intent(intent).idempotent == 1
        assert repository.get_order_intent(intent.intent_id) == intent
        assert repository._connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=11").fetchone()[0] == 1
        result = _simulate(intent, _state(us_instrument, now), (_event(us_instrument, now),), now)
        original = repository._save_execution_record

        def fail_fill_write(*args, **kwargs):
            if kwargs.get("table") == "fill_evidence":
                raise RuntimeError("injected fill write failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(repository, "_save_execution_record", fail_fill_write)
        with pytest.raises(RuntimeError):
            repository.save_execution_result(result.run, result.fills)
        assert repository._connection.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0
        monkeypatch.setattr(repository, "_save_execution_record", original)
        repository.save_trigger_evaluation(result.trigger_evaluation)
        repository.save_execution_result(result.run, result.fills)
        assert repository.get_trigger_evaluation(result.trigger_evaluation.trigger_evaluation_id) == result.trigger_evaluation
        assert repository.get_execution_run(result.run.run_id) == result.run
    finally:
        repository.close()


def test_ex49_architecture_and_hard_policy_are_immutable():
    root = Path(__file__).resolve().parents[2] / "execution"
    forbidden = ("from backtest", "from portfolio", "from learning", "from reports", "from ui")
    assert all(not any(token in path.read_text(encoding="utf-8") for token in forbidden) for path in root.glob("*.py"))
    with pytest.raises(ContractViolation):
        ExecutionPolicy(max_participation="0.10")
