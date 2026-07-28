from dataclasses import replace

import pytest

from contracts import (
    ConditionExpression,
    ConditionOperand,
    ConditionOperator,
    ContractViolation,
    DecisionMode,
    EvidenceRequirement,
    FeatureStatus,
    PlanAction,
    PlanProfile,
    PlanReadiness,
    PositionState,
    StrategyFamily,
    TakeProfitMode,
    TakeProfitSpec,
    canonical_json,
    stable_hash,
)
from strategies import StrategyEngine
from strategies.conditions import evaluate
from strategies.registry import default_specs
from strategies.templates.common import Proposal, always_false, compare, feature, level

from strategy_helpers import NOW, position, strategy_input


PULLBACK = {1: "bearish", 3: "bearish", 5: "bullish", 10: "bullish"}
BEARISH = {1: "bearish", 3: "bearish", 5: "bearish", 10: "bearish"}
REBOUND = {1: "bullish", 3: "bullish", 5: "bearish", 10: "bearish"}
RANGE = {1: "neutral", 3: "neutral", 5: "neutral", 10: "neutral"}
CONFLICT = {1: "bullish", 3: "bearish", 5: "bullish", 10: "bullish"}


def _entry_plans(bundle):
    return tuple(plan for plan in bundle.entry_or_add.plans if plan.action in {PlanAction.BUY, PlanAction.ADD})


def test_sp00_parameters_change_business_identity_and_prices(us_instrument):
    specs = list(default_specs())
    original = next(item for item in specs if item.strategy_id == "trend_continuation_v1")
    changed_parameters = dict(original.parameters)
    changed_parameters["trigger_buffer"] = 0.01
    changed = replace(original, parameters=changed_parameters, parameter_hash="")
    specs[specs.index(original)] = changed
    baseline = StrategyEngine().build(strategy_input(us_instrument), generated_at=NOW)
    adjusted = StrategyEngine().build(strategy_input(us_instrument, specs=tuple(specs)), generated_at=NOW)
    first = next(plan for plan in _entry_plans(baseline) if plan.strategy_id == original.strategy_id)
    second = next(plan for plan in _entry_plans(adjusted) if plan.strategy_id == original.strategy_id)
    assert second.parameter_hash != first.parameter_hash
    assert second.trigger_level.value > first.trigger_level.value
    assert second.plan_id != first.plan_id


def test_sp00_strong_three_value_logic(now):
    missing = compare(feature("missing"), ConditionOperator.GTE, ConditionOperand("constant", "1", 1, None), "FEATURE_MISSING")
    truth = compare(ConditionOperand("constant", "1", 1, None), ConditionOperator.EQUALS, ConditionOperand("constant", "1", 1, None), "PLAN_WAITING")
    falsehood = compare(ConditionOperand("constant", "0", 0, None), ConditionOperator.EQUALS, ConditionOperand("constant", "1", 1, None), "PLAN_WAITING")
    any_condition = ConditionExpression("", ConditionOperator.ANY, children=(truth, missing))
    all_condition = ConditionExpression("", ConditionOperator.ALL, children=(falsehood, missing))
    assert evaluate(any_condition, {}, now).result.value == "true"
    assert evaluate(all_condition, {}, now).result.value == "false"


def test_sp02_bullish_pullback_only_routes_pullback_and_support(us_instrument):
    plans = _entry_plans(StrategyEngine().build(strategy_input(us_instrument, directions=PULLBACK)))
    assert {plan.family for plan in plans} == {StrategyFamily.PULLBACK_ENTRY, StrategyFamily.SUPPORT_REBOUND}

    far_above_ma20 = strategy_input(us_instrument, directions=PULLBACK, reference_price=150.0)
    pullback = next(
        plan for plan in _entry_plans(StrategyEngine().build(far_above_ma20))
        if plan.family is StrategyFamily.PULLBACK_ENTRY
    )
    assert pullback.readiness is not PlanReadiness.TRIGGERED
    confirmation = next(item for item in pullback.evaluations if item.condition_id == pullback.confirmation_condition.condition_id)
    assert confirmation.result.value == "false"


def test_sp04_bearish_rebound_is_countertrend_support_only(us_instrument):
    plans = _entry_plans(StrategyEngine().build(strategy_input(us_instrument, directions=REBOUND)))
    assert {plan.family for plan in plans} == {StrategyFamily.SUPPORT_REBOUND}
    assert all("COUNTERTREND_ONLY" in plan.reason_codes for plan in plans)


def test_sp06_forecast_conflict_keeps_observation_and_protection(us_instrument):
    held = position(us_instrument, cost="100")
    bundle = StrategyEngine().build(strategy_input(us_instrument, position=held, directions=CONFLICT))
    assert all(plan.action is PlanAction.WATCH for plan in bundle.entry_or_add.plans)
    assert any(plan.family is StrategyFamily.PROTECTIVE_EXIT for plan in bundle.reduce_or_exit.plans)


def test_sp07_no_champion_still_has_explicit_observation(us_instrument):
    bundle = StrategyEngine().build(strategy_input(us_instrument, confirmed=False))
    assert bundle.entry_or_add.plans
    assert all(plan.readiness is PlanReadiness.OBSERVATION_ONLY for plan in bundle.entry_or_add.plans)
    assert "SCENARIO_OBSERVATION_ONLY" in bundle.entry_or_add.plans[0].reason_codes
    assert any(plan.family is not StrategyFamily.OBSERVATION for plan in bundle.entry_or_add.plans)


def test_sp08_ma120_touch_and_reclaim_is_an_event_plan(us_instrument):
    input = strategy_input(
        us_instrument,
        directions=PULLBACK,
        feature_overrides={"closed.ma_120": 100.0, "closed.ma_distance_120": 0.0},
    )
    plan = next(plan for plan in _entry_plans(StrategyEngine().build(input)) if plan.family is StrategyFamily.SUPPORT_REBOUND)
    assert plan.readiness is PlanReadiness.WAITING
    assert plan.confirmation_condition.evidence_requirement is EvidenceRequirement.SNAPSHOT
    assert any(child.evidence_requirement is EvidenceRequirement.EVENT_SEQUENCE for child in plan.confirmation_condition.children)
    assert "MA120_SUPPORT_ZONE_REACHED" in plan.reason_codes


def test_sp09_missing_ma120_is_reported_in_observation(us_instrument):
    input = strategy_input(us_instrument, directions=PULLBACK, feature_statuses={"closed.ma_120": FeatureStatus.MISSING})
    bundle = StrategyEngine().build(input)
    assert not any(plan.family is StrategyFamily.SUPPORT_REBOUND for plan in _entry_plans(bundle))
    watch = next(plan for plan in bundle.entry_or_add.plans if plan.action is PlanAction.WATCH)
    assert "closed.ma_120" in watch.missing_conditions
    assert "FEATURE_MISSING" in watch.reason_codes


def test_sp10_verified_high_retreat_generates_partial_profit_lock(us_instrument):
    bundle = StrategyEngine().build(strategy_input(us_instrument, position=position(us_instrument, cost="80")))
    plan = next(plan for plan in bundle.reduce_or_exit.plans if plan.family is StrategyFamily.PROFIT_LOCK)
    assert plan.action is PlanAction.REDUCE
    assert plan.readiness is PlanReadiness.TRIGGERED
    assert "PROFIT_LOCK_TRIGGERED" in plan.reason_codes
    invalidation = next(item for item in plan.evaluations if item.condition_id == plan.invalidation_condition.condition_id)
    assert invalidation.result.value == "false"


def test_sp11_missing_high_evidence_does_not_fake_profit_lock(us_instrument):
    input = strategy_input(
        us_instrument,
        position=position(us_instrument, cost="80"),
        feature_statuses={
            "current.retreat_from_session_high": FeatureStatus.MISSING,
            "closed.high_distance_20": FeatureStatus.MISSING,
        },
    )
    bundle = StrategyEngine().build(input)
    assert not any(plan.family is StrategyFamily.PROFIT_LOCK for plan in bundle.reduce_or_exit.plans)
    watch = next(plan for plan in bundle.entry_or_add.plans if plan.action is PlanAction.WATCH)
    assert {"current.retreat_from_session_high", "closed.high_distance_20"} <= set(watch.missing_conditions)
    assert "SESSION_OHLC_REQUIRED" in watch.reason_codes


def test_profit_lock_is_absent_before_high_water_mark_reaches_profit_floor(us_instrument):
    bundle = StrategyEngine().build(
        strategy_input(us_instrument, position=position(us_instrument, cost="110"))
    )
    assert not any(
        plan.family is StrategyFamily.PROFIT_LOCK
        for plan in bundle.reduce_or_exit.plans
    )


def test_profit_lock_trigger_has_no_contradictory_activation_price(us_instrument):
    bundle = StrategyEngine().build(
        strategy_input(us_instrument, position=position(us_instrument, cost="80"))
    )
    plan = next(
        plan for plan in bundle.reduce_or_exit.plans
        if plan.family is StrategyFamily.PROFIT_LOCK
    )
    assert plan.trigger_condition.operator is not ConditionOperator.ALL


def test_sp16_conditional_take_profit_is_explicitly_unquantified():
    condition = compare(feature("current.price"), ConditionOperator.LTE, ConditionOperand("constant", "1", 1, None), "PLAN_WAITING")
    take = TakeProfitSpec(TakeProfitMode.CONDITIONAL, None, None, condition, "TAKE_PROFIT_UNQUANTIFIED")
    assert take.level is None and take.risk_multiple is None
    with pytest.raises(ContractViolation):
        TakeProfitSpec(TakeProfitMode.CONDITIONAL, None, None, condition, "TAKE_PROFIT_QUANTIFIED")


def test_sp17_profiles_share_direction_and_prices(us_instrument):
    plans = _entry_plans(StrategyEngine().build(strategy_input(us_instrument)))
    for plan in plans:
        assert plan.profiles == (PlanProfile.AGGRESSIVE, PlanProfile.CONSERVATIVE)
        assert plan.trigger_level is not None and plan.stop is not None
        assert plan.stop.level.value < plan.trigger_level.value


def test_sp19_flat_held_and_held_unknown_have_complete_branches(us_instrument):
    flat = StrategyEngine().build(strategy_input(us_instrument))
    held = StrategyEngine().build(strategy_input(us_instrument, position=position(us_instrument)))
    unknown = StrategyEngine().build(strategy_input(us_instrument, position=position(us_instrument, cost="0")))
    assert flat.position_state is PositionState.FLAT
    assert flat.reduce_or_exit.readiness is PlanReadiness.NOT_APPLICABLE
    assert held.hold.plans and held.invalidation.plans
    assert unknown.position_state is PositionState.HELD_UNKNOWN
    assert unknown.hold.plans and unknown.invalidation.plans


def test_sp20_triggered_exit_demotes_triggered_add_but_preserves_conflict(monkeypatch, us_instrument):
    engine = StrategyEngine()
    original = engine._proposal

    def proposal(context, spec):
        if spec.family is StrategyFamily.SUPPORT_REBOUND:
            trigger = compare(feature("current.price"), ConditionOperator.GTE, level("forced_add", 90.0, "closed.ma_120"), "PLAN_TRIGGERED")
            return Proposal(
                PlanAction.ADD, trigger, None, 90.0, "forced_add_v1", 80.0, "forced_stop_v1",
                110.0, TakeProfitMode.RISK_MULTIPLE, None,
                compare(feature("current.price"), ConditionOperator.LTE, level("forced_stop", 80.0, "closed.ma_120"), "PROTECTIVE_EXIT_PENDING"),
                None, ("PLAN_TRIGGERED",), ("closed.ma_120",),
            )
        return original(context, spec)

    monkeypatch.setattr(engine, "_proposal", proposal)
    bundle = engine.build(strategy_input(us_instrument, position=position(us_instrument, cost="80"), directions=RANGE))
    add = next(plan for plan in bundle.entry_or_add.plans if plan.action is PlanAction.ADD)
    assert bundle.conflict_state == "entry_exit_both_triggered"
    assert "ENTRY_EXIT_CONFLICT" in bundle.reason_codes
    assert add.readiness is PlanReadiness.OBSERVATION_ONLY
    assert any(plan.readiness is PlanReadiness.TRIGGERED for plan in bundle.reduce_or_exit.plans)


def test_sp21_a_share_pre_market_without_price_keeps_open_conditions(a_instrument):
    bundle = StrategyEngine().build(strategy_input(a_instrument, mode=DecisionMode.PRE))
    plans = _entry_plans(bundle)
    assert plans and all(plan.readiness is PlanReadiness.OBSERVATION_ONLY for plan in plans)
    assert all(plan.trigger_level is not None for plan in plans)
    assert all("current.price" in plan.missing_conditions for plan in plans)


def test_sp22_us_pre_market_missing_volume_does_not_confirm_breakout(us_instrument):
    input = strategy_input(
        us_instrument,
        mode=DecisionMode.PRE,
        quote_price=106.0,
        feature_statuses={"current.volume_vs_daily_20": FeatureStatus.MISSING},
    )
    plan = next(plan for plan in _entry_plans(StrategyEngine().build(input)) if plan.family is StrategyFamily.BREAKOUT_CONFIRMATION)
    assert plan.readiness is PlanReadiness.OBSERVATION_ONLY
    assert "current.volume_vs_daily_20" in plan.missing_conditions


def test_sp23_equivalent_markets_keep_strategy_semantics(a_instrument, us_instrument):
    a_plans = {plan.strategy_id: plan for plan in _entry_plans(StrategyEngine().build(strategy_input(a_instrument)))}
    us_plans = {plan.strategy_id: plan for plan in _entry_plans(StrategyEngine().build(strategy_input(us_instrument)))}
    assert a_plans.keys() == us_plans.keys()
    for key in a_plans:
        assert (a_plans[key].family, a_plans[key].action, a_plans[key].readiness) == (
            us_plans[key].family, us_plans[key].action, us_plans[key].readiness
        )
        assert a_plans[key].trigger_level.value == pytest.approx(us_plans[key].trigger_level.value)
        assert a_plans[key].stop.level.value == pytest.approx(us_plans[key].stop.level.value)


def test_sp24_missing_features_do_not_pollute_another_instrument(a_instrument, us_instrument):
    engine = StrategyEngine()
    broken = strategy_input(a_instrument, directions=PULLBACK, feature_statuses={"closed.ma_120": FeatureStatus.MISSING})
    healthy = strategy_input(us_instrument, directions=PULLBACK)
    broken_bundle = engine.build(broken, generated_at=NOW)
    healthy_bundle = engine.build(healthy, generated_at=NOW)
    assert "closed.ma_120" in next(plan for plan in broken_bundle.entry_or_add.plans if plan.action is PlanAction.WATCH).missing_conditions
    assert "closed.ma_120" not in next(plan for plan in healthy_bundle.entry_or_add.plans if plan.action is PlanAction.WATCH).missing_conditions
    assert any(plan.family is StrategyFamily.SUPPORT_REBOUND for plan in _entry_plans(healthy_bundle))


def test_sp28_serialized_plans_exclude_downstream_execution_fields(us_instrument):
    forbidden = {"account_equity", "position_pct", "shares", "execution_level", "order_type", "max_loss_amount"}
    payload = canonical_json(StrategyEngine().build(strategy_input(us_instrument)))
    assert all(f'"{name}"' not in payload for name in forbidden)


def test_sp29_all_initial_templates_are_registered_and_routable():
    specs = default_specs()
    expected = {
        StrategyFamily.TREND_CONTINUATION,
        StrategyFamily.PULLBACK_ENTRY,
        StrategyFamily.BREAKOUT_CONFIRMATION,
        StrategyFamily.SUPPORT_REBOUND,
        StrategyFamily.RANGE_MEAN_REVERSION,
        StrategyFamily.PROTECTIVE_EXIT,
        StrategyFamily.PROFIT_LOCK,
        StrategyFamily.FAILED_REBOUND_EXIT,
        StrategyFamily.OBSERVATION,
    }
    assert {spec.family for spec in specs} == expected
    assert len(specs) == len({(spec.strategy_id, spec.strategy_version, spec.parameter_hash) for spec in specs}) == 9
    assert all(spec.parameters is not None and spec.required_features is not None for spec in specs)
