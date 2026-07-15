"""V2-6 风控验收矩阵中原先缺失的 RK 场景。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from risk_helpers import NOW, evidence_for, quality, request_for
from strategy_helpers import position, strategy_input
from tradehelper_v2.contracts import (
    AccountSnapshot, AvailabilitySource, ContractViolation, DataQualityReport,
    DecisionDisposition, DecisionMode, EvidenceStatus, ExecutionLevel, FreshnessStatus,
    InstrumentClassification, InstrumentId, Market, MarketEligibility, MarketState,
    PlanAction, PlanReadiness, PositionAvailability, PositionSnapshot, QualityAction,
    QualityStatus, RiskPolicy, RiskProfile, RiskRequest, StrategyFamily, TakeProfitMode,
    TradingSession, ValuationPrice, ValuationPriceKind, canonical_json,
)
from tradehelper_v2.risk import RiskOfficer, freeze_account_valuation
from tradehelper_v2.risk.market_rules import default_market_rules, precheck
from tradehelper_v2.risk.sizing import entry_capacity_detail, planned_loss
from tradehelper_v2.strategies import StrategyEngine
from tradehelper_v2.strategies.templates.common import Proposal, compare, feature, level


RANGE = {1: "neutral", 3: "neutral", 5: "neutral", 10: "neutral"}
BEARISH_REBOUND = {1: "bullish", 3: "bullish", 5: "bearish", 10: "bearish"}
INTRADAY_NOW = datetime(2026, 7, 12, 14, 0, tzinfo=timezone.utc)


def _entry_plan(request, *, triggered=False):
    return next(
        plan for plan in request.strategy_bundle.entry_or_add.plans
        if plan.action in {PlanAction.BUY, PlanAction.ADD}
        and (not triggered or plan.readiness is PlanReadiness.TRIGGERED)
    )


def _decision(bundle, plan, profile=RiskProfile.CONSERVATIVE):
    return next(item for item in bundle.decisions if item.plan_id == plan.plan_id and item.profile is profile)


def _eod_state(instrument, price="110", *, ask=None, freshness=FreshnessStatus.FRESH):
    return MarketState(
        instrument, DecisionMode.EOD, TradingSession.CLOSED, Decimal(price), Decimal("100"),
        None, Decimal(str(ask)) if ask is not None else None, Decimal("1000"), NOW,
        "fixture", freshness,
    )


def _intraday_state(instrument, price="110", *, ask=None):
    return MarketState(
        instrument, DecisionMode.INTRADAY, TradingSession.REGULAR, Decimal(price), Decimal("100"),
        None, Decimal(str(ask)) if ask is not None else None, Decimal("1000"), INTRADAY_NOW,
        "fixture", FreshnessStatus.FRESH,
    )


def _availability(instrument, total, sellable):
    return PositionAvailability(
        instrument, Decimal(str(total)), None if sellable is None else Decimal(str(sellable)),
        NOW, AvailabilitySource.USER if sellable is not None else AvailabilitySource.UNAVAILABLE, (),
    )


def test_rk02_zero_equity_blocks_entry_without_simulated_capital(us_instrument):
    request = request_for(us_instrument, cash=Decimal("0"))
    entries = [item for item in RiskOfficer().assess(request, generated_at=NOW).decisions if item.action is PlanAction.BUY]
    assert entries and all(item.level is ExecutionLevel.C and item.approved_shares == 0 for item in entries)
    assert all("RISK_EQUITY_ZERO" in item.reason_codes for item in entries)


def test_rk04_blocked_data_rejects_entry_but_preserves_protective_exit(us_instrument):
    blocked = quality(status=QualityStatus.BLOCKED, action=QualityAction.BLOCK_NEW_ENTRIES, block=True)
    flat = request_for(us_instrument, quality_report=blocked)
    assert all(
        item.level is ExecutionLevel.D
        for item in RiskOfficer().assess(flat, generated_at=NOW).decisions
        if item.action is PlanAction.BUY
    )
    held = request_for(
        us_instrument, position=position(us_instrument), reference_price=90,
        valuation_price=Decimal("90"), quality_report=blocked,
    )
    exits = [item for item in RiskOfficer().assess(held, generated_at=NOW).decisions if item.action is PlanAction.SELL]
    assert exits and all("RISK_EXIT_PRESERVED" in item.reason_codes for item in exits)
    assert all("RISK_DATA_BLOCKED" not in item.reason_codes for item in exits)


def test_rk05_actionable_entry_without_stop_is_contract_invalid(us_instrument):
    plan = _entry_plan(request_for(us_instrument))
    with pytest.raises(ContractViolation):
        replace(plan, stop=None)


def test_rk05_expired_plan_is_rejected_for_every_action_branch(us_instrument):
    entry_request = request_for(us_instrument)
    entry = _entry_plan(entry_request)
    expired_entry = replace(entry_request, as_of=entry.expires_at)
    entry_decision = _decision(RiskOfficer().assess(expired_entry, generated_at=NOW), entry)
    assert entry_decision.level is ExecutionLevel.D and "RISK_PLAN_EXPIRED" in entry_decision.reason_codes

    held_request = request_for(
        us_instrument, position=position(us_instrument), reference_price=90,
        valuation_price=Decimal("90"),
    )
    sell = next(plan for plan in held_request.strategy_bundle.reduce_or_exit.plans if plan.action is PlanAction.SELL)
    expired_exit = replace(held_request, as_of=sell.expires_at)
    exit_decision = _decision(RiskOfficer().assess(expired_exit, generated_at=NOW), sell)
    assert exit_decision.level is ExecutionLevel.D
    assert {"RISK_PLAN_EXPIRED", "RISK_EXIT_PRESERVED"}.issubset(exit_decision.reason_codes)


def test_rk07_triggered_reliable_positive_entry_can_reach_a(us_instrument):
    base = request_for(
        us_instrument, mode=DecisionMode.INTRADAY, quote_price=101.5,
        as_of=INTRADAY_NOW,
    )
    plan = _entry_plan(base, triggered=True)
    request = replace(base, evidence=(evidence_for(plan, EvidenceStatus.RELIABLE_POSITIVE),), market_state=_intraday_state(us_instrument, price="101.5", ask="102"))
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.level is ExecutionLevel.A
    assert decision.disposition is DecisionDisposition.APPROVED_NOW
    assert decision.entry_price == Decimal("102")


def test_rk08_missing_or_small_evidence_is_at_most_b_and_future_is_rejected(us_instrument):
    base = request_for(us_instrument)
    plan = _entry_plan(base)
    for status in (EvidenceStatus.UNAVAILABLE, EvidenceStatus.INSUFFICIENT_SAMPLE):
        request = replace(base, evidence=(evidence_for(plan, status),))
        assert _decision(RiskOfficer().assess(request, generated_at=NOW), plan).level is ExecutionLevel.B
    future = replace(evidence_for(plan, EvidenceStatus.INSUFFICIENT_SAMPLE), generated_at=NOW + timedelta(seconds=1))
    with pytest.raises(ContractViolation):
        replace(base, evidence=(future,))


def test_rk09_negative_entry_evidence_is_observation_only(us_instrument):
    base = request_for(us_instrument)
    plan = _entry_plan(base)
    request = replace(base, evidence=(evidence_for(plan, EvidenceStatus.NEGATIVE),))
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.level is ExecutionLevel.C and decision.disposition is DecisionDisposition.OBSERVE
    assert "RISK_NEGATIVE_EXPECTANCY" in decision.reason_codes


def test_rk10_conflicting_evidence_is_rejected(us_instrument):
    base = request_for(us_instrument)
    plan = _entry_plan(base)
    request = replace(base, evidence=(evidence_for(plan, EvidenceStatus.CONFLICTING),))
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.level is ExecutionLevel.D and "RISK_EVIDENCE_CONFLICT" in decision.reason_codes


def test_rk12_b_countertrend_and_quality_multipliers_are_individually_audited(us_instrument):
    degraded = quality(status=QualityStatus.DEGRADED, action=QualityAction.REDUCE_POSITION, multiplier=.8)
    base = request_for(
        us_instrument, reference_price=96, directions=BEARISH_REBOUND,
        quality_report=degraded, cash=Decimal("100000"),
    )
    plan = next(plan for plan in base.strategy_bundle.entry_or_add.plans if "COUNTERTREND_ONLY" in plan.reason_codes)
    request = replace(base, evidence=(evidence_for(plan, EvidenceStatus.INSUFFICIENT_SAMPLE),))
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert {item.code for item in decision.soft_adjustments} >= {
        "RISK_SMALL_SAMPLE", "RISK_COUNTERTREND_CAP", "RISK_QUALITY_MULTIPLIER_APPLIED",
    }


def test_rk13_capacity_uses_minimum_constraint_and_fresh_ask_for_triggered_entry(us_instrument):
    rules = default_market_rules(us_instrument.market, us_instrument.exchange, NOW)
    detail = entry_capacity_detail(
        equity=Decimal("10000"), cash=Decimal("700"), invested=Decimal("0"),
        current_value=Decimal("0"), existing_shares=Decimal("0"), entry=Decimal("100"),
        stop=Decimal("90"), risk_budget=Decimal("1000"), target_cap=Decimal("0.25"),
        rules=rules, is_add=False,
    )
    assert detail.cash_cap == Decimal("7") and detail.shares <= detail.cash_cap
    base = request_for(
        us_instrument, mode=DecisionMode.INTRADAY, quote_price=101.5,
        as_of=INTRADAY_NOW,
    )
    plan = _entry_plan(base, triggered=True)
    request = replace(base, market_state=_intraday_state(us_instrument, price="101.5", ask="102.5"))
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.entry_price == Decimal("102.5") >= Decimal(str(plan.trigger_level.value))


def test_rk15_a_uses_round_lots_and_us_uses_single_shares(a_instrument, us_instrument):
    common = dict(
        equity=Decimal("100000"), cash=Decimal("100000"), invested=Decimal("0"),
        current_value=Decimal("0"), existing_shares=Decimal("0"), entry=Decimal("100"),
        stop=Decimal("99"), risk_budget=Decimal("1000"), target_cap=Decimal("0.25"), is_add=False,
    )
    a = entry_capacity_detail(**common, rules=default_market_rules(a_instrument.market, a_instrument.exchange, NOW)).shares
    us = entry_capacity_detail(**common, rules=default_market_rules(us_instrument.market, us_instrument.exchange, NOW)).shares
    assert a > 0 and a % 100 == 0
    assert us > 0 and us % 1 == 0 and us >= a


def test_rk16_a_full_exit_can_sell_odd_lot_tail(a_instrument):
    held = position(a_instrument, shares="250", cost="100")
    request = request_for(
        a_instrument, position=held, reference_price=90, valuation_price=Decimal("90"),
        availability=_availability(a_instrument, 250, 250),
    )
    sell = next(plan for plan in request.strategy_bundle.reduce_or_exit.plans if plan.action is PlanAction.SELL)
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), sell)
    assert decision.approved_shares == Decimal("250")


def test_rk17_reduce_profiles_are_50_25_and_sub_lot_is_not_promoted(a_instrument, us_instrument):
    us_position = position(us_instrument, shares="100", cost="80")
    us_request = request_for(us_instrument, position=us_position, reference_price=110, valuation_price=Decimal("110"))
    reduce_plan = next(plan for plan in us_request.strategy_bundle.reduce_or_exit.plans if plan.action is PlanAction.REDUCE)
    bundle = RiskOfficer().assess(us_request, generated_at=NOW)
    assert _decision(bundle, reduce_plan, RiskProfile.CONSERVATIVE).approved_shares == Decimal("50")
    assert _decision(bundle, reduce_plan, RiskProfile.AGGRESSIVE).approved_shares == Decimal("25")
    a_position = position(a_instrument, shares="250", cost="80")
    a_request = request_for(
        a_instrument, position=a_position, reference_price=110, valuation_price=Decimal("110"),
        availability=_availability(a_instrument, 250, 250),
    )
    a_reduce = next(plan for plan in a_request.strategy_bundle.reduce_or_exit.plans if plan.action is PlanAction.REDUCE)
    aggressive = _decision(RiskOfficer().assess(a_request, generated_at=NOW), a_reduce, RiskProfile.AGGRESSIVE)
    assert aggressive.approved_shares == 0 and aggressive.level is ExecutionLevel.C


def test_rk18_add_max_loss_includes_existing_position_risk(us_instrument):
    held = position(us_instrument, shares="100", cost="100")
    request = request_for(
        us_instrument, position=held, reference_price=96, directions=RANGE,
        valuation_price=Decimal("96"), cash=Decimal("100000"),
    )
    plan = next(plan for plan in request.strategy_bundle.entry_or_add.plans if plan.action is PlanAction.ADD)
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.total_position_planned_loss > decision.incremental_planned_loss
    assert decision.max_loss_amount == decision.total_position_planned_loss


def test_rk19_existing_risk_can_exhaust_add_budget(us_instrument):
    held = position(us_instrument, shares="200", cost="100")
    policy = RiskPolicy(conservative_risk_pct=Decimal("0.005"), aggressive_risk_pct=Decimal("0.01"))
    request = request_for(
        us_instrument, position=held, reference_price=96, directions=RANGE,
        valuation_price=Decimal("96"), cash=Decimal("60800"), policy=policy,
    )
    plan = next(plan for plan in request.strategy_bundle.entry_or_add.plans if plan.action is PlanAction.ADD)
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.approved_shares == 0 and "RISK_ADD_BLOCKED_BY_EXISTING_RISK" in decision.reason_codes


def test_rk20_single_position_cap_and_redline_block_add(us_instrument):
    at_cap = request_for(
        us_instrument, position=position(us_instrument, shares="250"), reference_price=96,
        directions=RANGE, valuation_price=Decimal("96"), cash=Decimal("72000"),
    )
    plan = next(plan for plan in at_cap.strategy_bundle.entry_or_add.plans if plan.action is PlanAction.ADD)
    assert "RISK_SINGLE_POSITION_CAP" in _decision(RiskOfficer().assess(at_cap, generated_at=NOW), plan).reason_codes
    redline = request_for(
        us_instrument, position=position(us_instrument, shares="300"), reference_price=96,
        directions=RANGE, valuation_price=Decimal("96"), cash=Decimal("67200"),
    )
    plan = next(plan for plan in redline.strategy_bundle.entry_or_add.plans if plan.action is PlanAction.ADD)
    assert "RISK_CONCENTRATION_REDLINE" in _decision(RiskOfficer().assess(redline, generated_at=NOW), plan).reason_codes


def test_rk21_total_stock_cap_blocks_new_risk(us_instrument):
    base = request_for(us_instrument)
    other = InstrumentId.from_code("MSFT", Market.US, "XNAS")
    other_position = PositionSnapshot(other, Decimal("90"), Decimal("100"), NOW)
    account = AccountSnapshot(Market.US, "USD", Decimal("1000"), (other_position,), NOW)
    price = ValuationPrice(other, Decimal("100"), NOW, "fixture", ValuationPriceKind.REFERENCE_CLOSE, FreshnessStatus.NOT_REQUIRED)
    valuation = freeze_account_valuation(account, {other: price}, NOW, generated_at=NOW)
    request = RiskRequest(
        us_instrument, base.strategy_bundle, base.trading_scenario, base.data_quality, account,
        valuation, None, (), base.market_rules, None, base.policy, NOW,
    )
    plan = _entry_plan(request)
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.approved_shares == 0 and "RISK_TOTAL_STOCK_CAP" in decision.reason_codes


def test_rk22_protective_exit_ignores_negative_entry_evidence(us_instrument):
    held = position(us_instrument, shares="10", cost="100")
    base = request_for(
        us_instrument, position=held, valuation_price=Decimal("90"),
        mode=DecisionMode.INTRADAY, quote_price=90, as_of=INTRADAY_NOW,
    )
    plan = next(plan for plan in base.strategy_bundle.reduce_or_exit.plans if plan.action is PlanAction.SELL)
    request = replace(base, evidence=(evidence_for(plan, EvidenceStatus.NEGATIVE),), market_state=_intraday_state(us_instrument, price="90"))
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.level in {ExecutionLevel.A, ExecutionLevel.B}
    assert decision.disposition is DecisionDisposition.APPROVED_NOW
    assert "RISK_NEGATIVE_EXPECTANCY" not in decision.reason_codes


def test_rk23_triggered_protective_exit_demotes_hold(us_instrument):
    request = request_for(
        us_instrument, position=position(us_instrument), reference_price=90,
        valuation_price=Decimal("90"),
    )
    holds = [item for item in RiskOfficer().assess(request, generated_at=NOW).decisions if item.action is PlanAction.HOLD]
    assert holds and all(item.level is ExecutionLevel.C for item in holds)
    assert all("RISK_PROTECTIVE_EXIT_PRIORITY" in item.reason_codes for item in holds)


def test_rk24_a_t1_unknown_and_partial_sellability_are_explicit(a_instrument):
    held = position(a_instrument, shares="250", cost="100")
    unknown = request_for(a_instrument, position=held, reference_price=90, valuation_price=Decimal("90"))
    sell = next(plan for plan in unknown.strategy_bundle.reduce_or_exit.plans if plan.action is PlanAction.SELL)
    rejected = _decision(RiskOfficer().assess(unknown, generated_at=NOW), sell)
    assert rejected.level is ExecutionLevel.D and "RISK_T1_BLOCKED" in rejected.reason_codes
    none_sellable = replace(unknown, position_availability=_availability(a_instrument, 250, 0))
    zero = _decision(RiskOfficer().assess(none_sellable, generated_at=NOW), sell)
    assert zero.level is ExecutionLevel.D and zero.blocked_shares == Decimal("250")
    partial = replace(unknown, position_availability=_availability(a_instrument, 250, 100))
    decision = _decision(RiskOfficer().assess(partial, generated_at=NOW), sell)
    assert decision.approved_shares == Decimal("100") and decision.blocked_shares == Decimal("150")
    assert "RISK_PARTIAL_SELLABLE" in decision.reason_codes


def test_rk25_a_price_limits_block_entry_and_exit(a_instrument):
    rules = default_market_rules(a_instrument.market, a_instrument.exchange, NOW)
    up = MarketState(a_instrument, DecisionMode.INTRADAY, TradingSession.REGULAR, Decimal("109.9"), Decimal("100"), None, None, Decimal("1"), NOW, "fixture", FreshnessStatus.FRESH)
    down = replace(up, current_price=Decimal("90.1"))
    assert precheck(rules, up, PlanAction.BUY).eligibility is MarketEligibility.BLOCKED
    exit_check = precheck(rules, down, PlanAction.SELL)
    assert exit_check.eligibility is MarketEligibility.BLOCKED
    assert "RISK_PROTECTIVE_EXIT_PRIORITY" in exit_check.reasons
    held = position(a_instrument, shares="200", cost="100")
    request = request_for(
        a_instrument, position=held, valuation_price=Decimal("90.1"),
        availability=_availability(a_instrument, 200, 200), mode=DecisionMode.INTRADAY,
        quote_price=90.1, as_of=INTRADAY_NOW,
    )
    sell = next(plan for plan in request.strategy_bundle.reduce_or_exit.plans if plan.action is PlanAction.SELL)
    decision = _decision(RiskOfficer().assess(replace(request, market_state=replace(down, observed_at=INTRADAY_NOW)), generated_at=NOW), sell)
    assert decision.level is ExecutionLevel.D and "RISK_PRICE_LIMIT_BLOCKED" in decision.reason_codes


def test_rk26_unknown_a_classification_blocks_only_near_strict_limit(a_instrument):
    rules = default_market_rules(a_instrument.market, a_instrument.exchange, NOW, InstrumentClassification.UNKNOWN)
    normal = MarketState(a_instrument, DecisionMode.INTRADAY, TradingSession.REGULAR, Decimal("104.8"), Decimal("100"), None, None, Decimal("1"), NOW, "fixture", FreshnessStatus.FRESH)
    strict = replace(normal, current_price=Decimal("104.9"))
    assert precheck(rules, normal, PlanAction.BUY).eligibility is MarketEligibility.ELIGIBLE
    assert precheck(rules, strict, PlanAction.BUY).eligibility is MarketEligibility.BLOCKED


def test_rk29_no_level2_never_claims_depth_or_execution_guarantee(us_instrument):
    state = MarketState(us_instrument, DecisionMode.PRE, TradingSession.PRE, Decimal("100"), Decimal("99"), Decimal("99.9"), Decimal("100.1"), None, NOW, "fixture", FreshnessStatus.FRESH)
    result = precheck(default_market_rules(us_instrument.market, us_instrument.exchange, NOW), state, PlanAction.BUY)
    assert result.eligibility is MarketEligibility.RECHECK_REQUIRED
    assert result.reasons == ("RISK_EXTENDED_TOP_OF_BOOK_ONLY",)
    assert not hasattr(result, "depth") and not hasattr(result, "fill_guarantee")


def test_rk31_max_loss_is_stop_assumption_and_gap_risk_is_disclosed(us_instrument):
    request = request_for(us_instrument)
    plan = _entry_plan(request)
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.max_loss_amount == decision.incremental_planned_loss
    assert "RISK_GAP_LOSS_CAN_EXCEED_PLAN" in decision.reason_codes


def test_rk33_every_plan_profile_has_exactly_one_decision(us_instrument):
    request = request_for(us_instrument)
    bundle = RiskOfficer().assess(request, generated_at=NOW)
    expected = {
        (plan.plan_id, RiskProfile(profile.value))
        for branch in (request.strategy_bundle.entry_or_add, request.strategy_bundle.reduce_or_exit, request.strategy_bundle.hold)
        for plan in branch.plans for profile in plan.profiles
    }
    actual = {(item.plan_id, item.profile) for item in bundle.decisions}
    assert actual == expected and len(actual) == len(bundle.decisions)
    assert any(item.level is ExecutionLevel.C for item in bundle.decisions)


def test_rk34_exit_priority_preserves_demoted_entry(monkeypatch, us_instrument):
    engine = StrategyEngine()
    original = engine._proposal

    # Use the frozen V2-5 conflict fixture shape, with the actual condition enum.
    from tradehelper_v2.contracts import ConditionOperator

    def forced_proposal(context, spec):
        if spec.family is StrategyFamily.SUPPORT_REBOUND:
            trigger = compare(feature("current.price"), ConditionOperator.GTE, level("forced_add", 90.0, "closed.ma_120"), "PLAN_TRIGGERED")
            return Proposal(
                PlanAction.ADD, trigger, None, 90.0, "forced_add_v1", 80.0, "forced_stop_v1",
                110.0, TakeProfitMode.RISK_MULTIPLE, None,
                compare(feature("current.price"), ConditionOperator.LTE, level("forced_stop", 80.0, "closed.ma_120"), "PROTECTIVE_EXIT_PENDING"),
                None, ("PLAN_TRIGGERED",), ("closed.ma_120",),
            )
        return original(context, spec)

    monkeypatch.setattr(engine, "_proposal", forced_proposal)
    held = position(us_instrument, shares="10", cost="80")
    input = strategy_input(us_instrument, position=held, directions=RANGE)
    strategy_bundle = engine.build(input, generated_at=NOW)
    account = AccountSnapshot(Market.US, "USD", Decimal("10000"), (held,), NOW)
    price = ValuationPrice(us_instrument, Decimal("100"), NOW, "fixture", ValuationPriceKind.REFERENCE_CLOSE, FreshnessStatus.NOT_REQUIRED)
    valuation = freeze_account_valuation(account, {us_instrument: price}, NOW, generated_at=NOW)
    request = RiskRequest(
        us_instrument, strategy_bundle, input.trading_scenario, quality(), account, valuation,
        None, (), default_market_rules(Market.US, us_instrument.exchange, NOW), None, RiskPolicy(), NOW,
    )
    decisions = RiskOfficer().assess(request, generated_at=NOW).decisions
    add_ids = {plan.plan_id for plan in strategy_bundle.entry_or_add.plans if plan.action is PlanAction.ADD}
    exit_ids = {plan.plan_id for plan in strategy_bundle.reduce_or_exit.plans}
    assert add_ids and exit_ids
    assert all(item.level is ExecutionLevel.C for item in decisions if item.plan_id in add_ids)
    assert {item.plan_id for item in decisions} >= exit_ids


def test_rk36_modes_have_explicit_execution_or_recheck_semantics(us_instrument):
    rules = default_market_rules(us_instrument.market, us_instrument.exchange, NOW)
    pre = MarketState(us_instrument, DecisionMode.PRE, TradingSession.PRE, Decimal("100"), Decimal("99"), None, None, None, NOW, "fixture", FreshnessStatus.FRESH)
    intraday = MarketState(us_instrument, DecisionMode.INTRADAY, TradingSession.REGULAR, Decimal("100"), Decimal("99"), None, None, Decimal("1"), NOW, "fixture", FreshnessStatus.FRESH)
    eod = MarketState(us_instrument, DecisionMode.EOD, TradingSession.CLOSED, Decimal("100"), Decimal("99"), None, None, Decimal("1"), NOW, "fixture", FreshnessStatus.NOT_REQUIRED)
    assert precheck(rules, pre, PlanAction.BUY).eligibility is MarketEligibility.RECHECK_REQUIRED
    assert precheck(rules, intraday, PlanAction.BUY).eligibility is MarketEligibility.ELIGIBLE
    assert precheck(rules, eod, PlanAction.BUY).eligibility is MarketEligibility.RECHECK_REQUIRED
    eod_request = request_for(us_instrument, reference_price=110)
    eod_plan = _entry_plan(eod_request, triggered=True)
    eod_decision = _decision(RiskOfficer().assess(replace(eod_request, market_state=_eod_state(us_instrument)), generated_at=NOW), eod_plan)
    intraday_request = request_for(
        us_instrument, mode=DecisionMode.INTRADAY, quote_price=101.5,
        as_of=INTRADAY_NOW,
    )
    intraday_plan = _entry_plan(intraday_request, triggered=True)
    intraday_decision = _decision(RiskOfficer().assess(replace(intraday_request, market_state=_intraday_state(us_instrument, price="101.5")), generated_at=NOW), intraday_plan)
    assert eod_decision.recheck_at_trigger and not eod_decision.executable_now
    assert intraday_decision.executable_now and not intraday_decision.recheck_at_trigger


def test_rk37_missing_valuation_for_one_stock_does_not_contaminate_another(us_instrument):
    missing = request_for(
        us_instrument, position=position(us_instrument), reference_price=96,
        directions=RANGE,
    )
    other = InstrumentId.from_code("MSFT", Market.US, "XNAS")
    complete = request_for(other)
    missing_levels = {item.level for item in RiskOfficer().assess(missing, generated_at=NOW).decisions if item.action in {PlanAction.BUY, PlanAction.ADD}}
    complete_levels = {item.level for item in RiskOfficer().assess(complete, generated_at=NOW).decisions if item.action is PlanAction.BUY}
    assert missing.valuation.status.value == "incomplete"
    assert complete.valuation.status.value == "complete"
    assert missing_levels == {ExecutionLevel.C}
    assert complete_levels == {ExecutionLevel.B}


def test_rk40_risk_serialization_contains_no_order_fill_or_broker_fields(us_instrument):
    payload = canonical_json(RiskOfficer().assess(request_for(us_instrument), generated_at=NOW)).lower()
    for forbidden in ("order_intent", "fill_price", "fill_quantity", "broker_order_id"):
        assert forbidden not in payload


def test_rk42_soft_policy_is_versioned_while_hard_caps_remain_immutable(us_instrument):
    with pytest.raises(ContractViolation):
        RiskPolicy(policy_version="bad_hard_cap", total_stock_hard_cap=Decimal("0.95"))
    policy = RiskPolicy(policy_version="risk_policy_soft_test", b_level_multiplier=Decimal("0.40"))
    request = request_for(us_instrument, policy=policy)
    plan = _entry_plan(request)
    decision = _decision(RiskOfficer().assess(request, generated_at=NOW), plan)
    assert decision.risk_policy_version == "risk_policy_soft_test"
    assert any(item.code == "RISK_SMALL_SAMPLE" and item.multiplier == Decimal("0.40") for item in decision.soft_adjustments)
    assert any(item.code == "RISK_HARD_CONSTRAINT_IMMUTABLE" and item.passed for item in decision.hard_constraints)
