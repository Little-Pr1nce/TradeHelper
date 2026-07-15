"""确定性 StrategyEngine：冻结输入到 TradePlan，不访问数据库或网络。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from tradehelper_v2.contracts import (
    ConditionEvaluation, ConditionExpression, ConditionOperand, ConditionOperator, ConditionResult,
    ContractViolation, CurrentPriceState, DerivedPriceLevel, EvidenceRequirement, FeatureStatus, FeatureValue,
    PlanAction, PlanProfile, PlanReadiness, PositionState, QuantityIntent, ScenarioState,
    ScenarioStatus, StopMode, StopSpec, StrategyBranch, StrategyBundle, StrategyFamily,
    StrategyInput, TakeProfitMode, TakeProfitSpec, TradePlan, stable_hash,
)
from .conditions import evaluate
from .policy import POSITION_ATR_MULTIPLIER, POSITION_EPSILON_FLOOR
from .templates import exits, observation, support, trend
from .templates.common import Proposal, always_false, compare, constant, feature, level


@dataclass(frozen=True, slots=True)
class _Context:
    input: StrategyInput
    values: dict[str, FeatureValue]
    p0: float | None
    pobs: float | None
    held: bool
    cost: float | None

    def number(self, name: str) -> float | None:
        value = self.values.get(name)
        if value is None or value.status is not FeatureStatus.AVAILABLE or not isinstance(value.value, (int, float)):
            return None
        return float(value.value)

    @property
    def bearish(self) -> bool:
        return self.input.trading_scenario.state in {ScenarioState.BEARISH_CONTINUATION, ScenarioState.BEARISH_REBOUND}

    @property
    def is_eod(self) -> bool:
        return self.input.feature_snapshot.mode.value == "eod"


def _level(value: float, role: str, code: str, scenario_id: str, sources: tuple[str, ...]) -> DerivedPriceLevel:
    return DerivedPriceLevel("", value, role, code, "v1", sources, scenario_id)


_FLAT_POSITION_HASH = stable_hash("flat")


class StrategyEngine:
    """纯内存、可回放的策略编排器。"""

    def __init__(self) -> None:
        # 回放常会对同一冻结输入重复渲染；只缓存显式冻结发行时间的不可变结果。
        self._bundle_cache: dict[tuple[str, str, str, str, datetime], StrategyBundle] = {}

    def build(self, input: StrategyInput, *, generated_at: datetime | None = None) -> StrategyBundle:
        position_hash = stable_hash(input.position_snapshot) if input.position_snapshot else _FLAT_POSITION_HASH
        cache_key = (input.trading_scenario.scenario_id, input.feature_snapshot.feature_hash, position_hash, input.policy_version, generated_at) if generated_at is not None else None
        if cache_key is not None and cache_key in self._bundle_cache:
            return self._bundle_cache[cache_key]
        generated_at = generated_at or datetime.now(timezone.utc)
        values = {item.name: item for item in input.feature_snapshot.values}
        p0, pobs = self._prices(input, values)
        state = self._position_state(input.position_snapshot, pobs, values)
        context = _Context(input, values, p0, pobs, input.position_snapshot is not None,
                           float(input.position_snapshot.cost_price) if input.position_snapshot else None)
        specs = input.strategy_specs
        plans: list[TradePlan] = []
        missing_conditions: set[str] = set()
        observation_reasons: set[str] = set()
        if input.trading_scenario.status is ScenarioStatus.BLOCKED:
            observation_reasons.add("SCENARIO_ENTRY_BLOCKED")
        elif input.trading_scenario.status is ScenarioStatus.OBSERVATION_ONLY:
            observation_reasons.add("SCENARIO_OBSERVATION_ONLY")
        if input.trading_scenario.current_overlay.price_state is CurrentPriceState.EXPECTED_MISSING:
            observation_reasons.add("CURRENT_PRICE_EXPECTED_MISSING")
        for spec in specs:
            if not spec.enabled or spec.family is StrategyFamily.OBSERVATION:
                continue
            # 持仓保护模板不受情景准入或预测证据阻断。
            protective = spec.family in {StrategyFamily.PROTECTIVE_EXIT, StrategyFamily.PROFIT_LOCK, StrategyFamily.FAILED_REBOUND_EXIT}
            if not protective and (spec.family not in input.trading_scenario.allowed_strategy_families or input.trading_scenario.state not in spec.allowed_states):
                continue
            if spec.position_applicability == "flat" and context.held:
                continue
            if spec.position_applicability == "held" and not context.held:
                continue
            unavailable, reasons = self._unavailable_required(context, spec)
            if unavailable:
                missing_conditions.update(unavailable)
                observation_reasons.update(reasons)
                if any(name != "current.price" for name in unavailable):
                    continue
            proposal = self._proposal(context, spec)
            if proposal is not None:
                if proposal.action not in spec.supported_actions:
                    raise ContractViolation(f"strategy {spec.strategy_id} emitted an unsupported action")
                if unavailable:
                    proposal = replace(
                        proposal,
                        reason_codes=tuple(sorted(set(proposal.reason_codes) | set(reasons))),
                        explicit_missing_conditions=tuple(
                            sorted(set(proposal.explicit_missing_conditions) | set(unavailable))
                        ),
                    )
                plans.append(self._make_plan(input, spec, proposal, values, position_hash, generated_at))
            else:
                template_missing, template_reasons = self._template_missing(context, spec)
                missing_conditions.update(template_missing)
                observation_reasons.update(template_reasons)

        # 每一种输入都有观察计划，避免“没有候选”退化为空白输出。
        observation_spec = next(spec for spec in specs if spec.enabled and spec.family is StrategyFamily.OBSERVATION)
        observation_proposal = observation.conditional_observation(
            context,
            observation_spec,
            tuple(missing_conditions),
            tuple(observation_reasons),
        )
        plans.append(self._make_plan(input, observation_spec, observation_proposal, values, position_hash, generated_at))
        if context.held:
            protective_spec = next((spec for spec in specs if spec.enabled and spec.family is StrategyFamily.PROTECTIVE_EXIT), None)
            plans.append(self._hold_plan(context, protective_spec, values, position_hash, generated_at))

        plans = self._deduplicate(plans)
        entry = tuple(plan for plan in plans if plan.action in {PlanAction.BUY, PlanAction.ADD, PlanAction.WATCH})
        exits = tuple(plan for plan in plans if plan.action in {PlanAction.REDUCE, PlanAction.SELL})
        holds = tuple(plan for plan in plans if plan.action is PlanAction.HOLD)
        conflict = any(plan.readiness is PlanReadiness.TRIGGERED and plan.action in {PlanAction.BUY, PlanAction.ADD} for plan in entry) and any(plan.readiness is PlanReadiness.TRIGGERED for plan in exits)
        if conflict:
            entry = tuple(self._observation_only(plan, generated_at) if plan.action in {PlanAction.BUY, PlanAction.ADD} else plan for plan in entry)
        entry_branch = self._branch("entry_or_add", entry)
        if context.held:
            exit_branch = self._branch("reduce_or_exit", exits)
            hold_branch = self._branch("hold", holds)
            invalidation = self._branch("invalidation", exits + holds)
        else:
            exit_branch = StrategyBranch("reduce_or_exit", (), PlanReadiness.NOT_APPLICABLE, "BRANCH_NOT_APPLICABLE")
            hold_branch = StrategyBranch("hold", (), PlanReadiness.NOT_APPLICABLE, "BRANCH_NOT_APPLICABLE")
            invalidation = self._branch("invalidation", tuple(plan for plan in entry if plan.action is not PlanAction.WATCH))
            if not invalidation.plans:
                invalidation = self._branch("invalidation", tuple(plan for plan in entry if plan.action is PlanAction.WATCH))
        all_plans = entry_branch.plans + exit_branch.plans + hold_branch.plans
        session = input.trading_scenario.decision_session.session_date.isoformat() if input.trading_scenario.decision_session else "calendar-unavailable"
        branches = (entry_branch, exit_branch, hold_branch, invalidation)
        bundle_payload = {"scenario_id": input.trading_scenario.scenario_id, "position_hash": position_hash,
                          "plan_ids": tuple(sorted({plan.plan_id for plan in all_plans})),
                          "branches": tuple((branch.branch, branch.readiness, tuple(plan.plan_id for plan in branch.plans)) for branch in branches),
                          "conflict_state": "entry_exit_both_triggered" if conflict else "none",
                          "policy_version": input.policy_version}
        bundle_id = stable_hash(bundle_payload)
        reasons = ("ENTRY_EXIT_CONFLICT",) if conflict else ()
        bundle = StrategyBundle(bundle_id, f"{input.instrument.stable_key}|{session}|{bundle_id}", input.instrument,
                              input.trading_scenario.scenario_id, state, entry_branch, exit_branch, hold_branch,
                              invalidation, tuple(sorted({plan.plan_id for plan in all_plans if PlanProfile.CONSERVATIVE in plan.profiles})),
                              tuple(sorted({plan.plan_id for plan in all_plans if PlanProfile.AGGRESSIVE in plan.profiles})),
                              "entry_exit_both_triggered" if conflict else "none", reasons, input.policy_version, generated_at)
        if cache_key is not None:
            self._bundle_cache[cache_key] = bundle
        return bundle

    @staticmethod
    def _unavailable_required(context: _Context, spec) -> tuple[tuple[str, ...], tuple[str, ...]]:
        missing: list[str] = []
        reasons: set[str] = set()
        reason_by_status = {
            FeatureStatus.MISSING: "FEATURE_MISSING",
            FeatureStatus.INSUFFICIENT_HISTORY: "FEATURE_INSUFFICIENT_HISTORY",
            FeatureStatus.STALE: "FEATURE_STALE",
            FeatureStatus.BLOCKED: "FEATURE_BLOCKED",
            FeatureStatus.NOT_APPLICABLE: "FEATURE_MISSING",
        }
        for name in spec.required_features:
            if name == "current.price":
                if context.pobs is None:
                    missing.append(name)
                    reasons.add(
                        "CURRENT_PRICE_EXPECTED_MISSING"
                        if context.input.trading_scenario.current_overlay.price_state is CurrentPriceState.EXPECTED_MISSING
                        else "FEATURE_MISSING"
                    )
                continue
            value = context.values.get(name)
            if value is None or value.status is not FeatureStatus.AVAILABLE or value.value is None:
                missing.append(name)
                reasons.add(reason_by_status.get(value.status if value else FeatureStatus.MISSING, "FEATURE_MISSING"))
        return tuple(sorted(set(missing))), tuple(sorted(reasons))

    @staticmethod
    def _template_missing(context: _Context, spec) -> tuple[tuple[str, ...], tuple[str, ...]]:
        missing: set[str] = set()
        reasons: set[str] = set()
        if context.p0 is None and spec.family in {
            StrategyFamily.TREND_CONTINUATION,
            StrategyFamily.PULLBACK_ENTRY,
            StrategyFamily.BREAKOUT_CONFIRMATION,
            StrategyFamily.SUPPORT_REBOUND,
            StrategyFamily.RANGE_MEAN_REVERSION,
        }:
            missing.add("reference_price")
            reasons.add("FEATURE_MISSING")
        if spec.family is StrategyFamily.PROFIT_LOCK:
            if context.cost is None or context.cost <= 0:
                missing.add("position.cost_price")
                reasons.add("POSITION_COST_UNKNOWN")
            if context.number("current.retreat_from_session_high") is None and context.number("closed.high_distance_20") is None:
                missing.update(("current.retreat_from_session_high", "closed.high_distance_20"))
                reasons.add("SESSION_OHLC_REQUIRED")
        if spec.family is StrategyFamily.PROTECTIVE_EXIT and (context.cost is None or context.cost <= 0) and context.number("closed.ma_60") is None:
            missing.update(("position.cost_price", "closed.ma_60"))
            reasons.update(("POSITION_COST_UNKNOWN", "STOP_LEVEL_UNAVAILABLE"))
        return tuple(sorted(missing)), tuple(sorted(reasons))

    def _proposal(self, context: _Context, spec):
        handlers = {
            StrategyFamily.TREND_CONTINUATION: trend.trend_continuation,
            StrategyFamily.PULLBACK_ENTRY: trend.trend_pullback,
            StrategyFamily.BREAKOUT_CONFIRMATION: trend.breakout_confirmation,
            StrategyFamily.SUPPORT_REBOUND: support.ma120_support_rebound,
            StrategyFamily.RANGE_MEAN_REVERSION: support.range_mean_reversion,
            StrategyFamily.PROTECTIVE_EXIT: exits.protective_exit,
            StrategyFamily.PROFIT_LOCK: exits.profit_lock,
            StrategyFamily.FAILED_REBOUND_EXIT: exits.failed_rebound_exit,
        }
        return handlers[spec.family](context, spec)

    def _prices(self, input: StrategyInput, values: dict[str, FeatureValue]) -> tuple[float | None, float | None]:
        overlay = input.trading_scenario.current_overlay
        pobs = float(overlay.current_price) if overlay.price_state in {CurrentPriceState.REFERENCE_CLOSE, CurrentPriceState.FRESH_QUOTE} and overlay.current_price else None
        candidates: list[float] = []
        if overlay.price_state is CurrentPriceState.REFERENCE_CLOSE and pobs:
            candidates.append(pobs)
        elif overlay.price_state is CurrentPriceState.FRESH_QUOTE and pobs and overlay.realized_return_from_origin is not None and 1 + overlay.realized_return_from_origin > 0:
            candidates.append(pobs / (1 + overlay.realized_return_from_origin))
        for period in (5, 10, 20, 60, 120):
            average, distance = values.get(f"closed.ma_{period}"), values.get(f"closed.ma_distance_{period}")
            if average and distance and average.status is FeatureStatus.AVAILABLE and distance.status is FeatureStatus.AVAILABLE and isinstance(average.value, (int, float)) and isinstance(distance.value, (int, float)) and 1 + float(distance.value) > 0:
                candidates.append(float(average.value) * (1 + float(distance.value)))
        if not candidates:
            return None, pobs
        reference = candidates[0]
        if any(abs(value-reference) > max(1e-8, 1e-8 * max(abs(value), abs(reference))) for value in candidates[1:]):
            raise ContractViolation("inconsistent P0 reconstruction")
        return reference, pobs

    def _position_state(self, position, price: float | None, values: dict[str, FeatureValue]) -> PositionState:
        if position is None:
            return PositionState.FLAT
        if price is None or position.cost_price <= 0:
            return PositionState.HELD_UNKNOWN
        atr = values.get("closed.atr_pct_14")
        atr_value = float(atr.value) if atr and atr.status is FeatureStatus.AVAILABLE and isinstance(atr.value, (int, float)) else 0.0
        epsilon = max(POSITION_EPSILON_FLOOR, POSITION_ATR_MULTIPLIER * atr_value)
        result = price / float(position.cost_price) - 1
        return PositionState.HELD_PROFIT if result > epsilon else PositionState.HELD_LOSS if result < -epsilon else PositionState.HELD_FLAT

    def _make_plan(self, input: StrategyInput, spec, proposal: Proposal, values, position_hash: str, generated_at: datetime) -> TradePlan:
        scenario = input.trading_scenario
        # 所有模板只读取快照中的 FeatureValue；overlay 价格仅作为同一冻结事实的镜像。
        evaluation_values = dict(values)
        price = scenario.current_overlay.current_price if scenario.current_overlay.price_state in {CurrentPriceState.REFERENCE_CLOSE, CurrentPriceState.FRESH_QUOTE} else None
        evaluation_values["current.price"] = (price, FeatureStatus.AVAILABLE if price is not None else FeatureStatus.MISSING, scenario.current_overlay.observed_at)
        all_conditions = [proposal.trigger, proposal.invalidation]
        if proposal.confirmation: all_conditions.append(proposal.confirmation)
        stop = None
        trigger_level = _level(proposal.trigger_price, "trigger", proposal.trigger_code or "template_trigger_v1", scenario.scenario_id, proposal.evidence_features) if proposal.trigger_price else None
        if proposal.stop_price is not None:
            stop_level = _level(proposal.stop_price, "stop", proposal.stop_code or "template_stop_v1", scenario.scenario_id, proposal.evidence_features)
            stop_condition = compare(feature("current.price"), ConditionOperator.LTE, ConditionOperand("derived_level", "stop", proposal.stop_price, "price", proposal.evidence_features), "STOP_LEVEL_DEFINED")
            stop = StopSpec(StopMode.HARD_PRICE, stop_level, stop_condition, "STOP_LEVEL_DEFINED")
            all_conditions.append(stop_condition)
        if proposal.hold: all_conditions.append(proposal.hold)
        condition_map = {condition.condition_id: condition for condition in all_conditions}
        evaluations = tuple(evaluate(condition_map[key], evaluation_values, input.as_of) for key in sorted(condition_map))
        by_id = {item.condition_id: item for item in evaluations}
        required = [by_id[proposal.trigger.condition_id]] + ([by_id[proposal.confirmation.condition_id]] if proposal.confirmation else [])
        result_set = {item.result for item in required}
        if ConditionResult.UNKNOWN in result_set:
            readiness = PlanReadiness.OBSERVATION_ONLY
        elif ConditionResult.PENDING_EVENT in result_set or ConditionResult.FALSE in result_set:
            readiness = PlanReadiness.WAITING
        else:
            readiness = PlanReadiness.TRIGGERED
        if proposal.action in {PlanAction.BUY, PlanAction.ADD} and (stop is None or trigger_level is None):
            readiness = PlanReadiness.OBSERVATION_ONLY
        if proposal.action in {PlanAction.BUY, PlanAction.ADD} and scenario.status in {ScenarioStatus.BLOCKED, ScenarioStatus.OBSERVATION_ONLY}:
            readiness = PlanReadiness.OBSERVATION_ONLY
        if proposal.action is PlanAction.WATCH:
            readiness = PlanReadiness.OBSERVATION_ONLY
        if proposal.explicit_missing_conditions:
            readiness = PlanReadiness.OBSERVATION_ONLY
        if readiness in {PlanReadiness.TRIGGERED, PlanReadiness.WAITING} and (scenario.valid_from is None or scenario.expires_at is None):
            readiness = PlanReadiness.OBSERVATION_ONLY
        take = None
        if proposal.take_mode is not TakeProfitMode.NONE:
            take_level = _level(proposal.take_price, "take_profit", "template_take_profit_v1", scenario.scenario_id, proposal.evidence_features) if proposal.take_price else None
            risk_multiple = None
            if proposal.take_mode is TakeProfitMode.RISK_MULTIPLE and take_level and trigger_level and stop and stop.level:
                risk = trigger_level.value - stop.level.value
                if risk <= 0:
                    raise ContractViolation("risk-multiple take-profit requires positive entry risk")
                risk_multiple = (take_level.value - trigger_level.value) / risk
            quantified = proposal.take_mode in {TakeProfitMode.FIXED, TakeProfitMode.RISK_MULTIPLE} and take_level is not None
            take = TakeProfitSpec(
                proposal.take_mode,
                take_level,
                risk_multiple,
                proposal.take_condition,
                "TAKE_PROFIT_QUANTIFIED" if quantified else "TAKE_PROFIT_UNQUANTIFIED",
            )
        readiness_codes = {"PLAN_TRIGGERED", "PLAN_WAITING", "PLAN_OBSERVATION_ONLY", "BRANCH_NOT_APPLICABLE"}
        reasons = [reason for reason in proposal.reason_codes if reason not in readiness_codes]
        reasons.append({PlanReadiness.TRIGGERED: "PLAN_TRIGGERED", PlanReadiness.WAITING: "PLAN_WAITING", PlanReadiness.OBSERVATION_ONLY: "PLAN_OBSERVATION_ONLY", PlanReadiness.NOT_APPLICABLE: "BRANCH_NOT_APPLICABLE"}[readiness])
        missing = tuple(sorted({name for item in evaluations for name in item.missing_features} | set(proposal.explicit_missing_conditions)))
        if missing:
            reasons.append("FEATURE_MISSING")
        profiles = tuple(PlanProfile(value) for value in proposal.profiles)
        identity = {"instrument": input.instrument, "scenario_id": scenario.scenario_id, "strategy_id": spec.strategy_id,
                    "strategy_version": spec.strategy_version, "parameter_hash": spec.parameter_hash, "family": spec.family,
                    "action": proposal.action, "quantity_intent": {PlanAction.BUY: QuantityIntent.OPEN, PlanAction.ADD: QuantityIntent.ADD, PlanAction.REDUCE: QuantityIntent.PARTIAL_EXIT, PlanAction.SELL: QuantityIntent.FULL_EXIT, PlanAction.HOLD: QuantityIntent.KEEP, PlanAction.WATCH: QuantityIntent.NONE}[proposal.action],
                    "profiles": tuple(sorted(set(profiles), key=lambda item: item.value)), "trigger": proposal.trigger,
                    "confirmation": proposal.confirmation, "trigger_level": trigger_level, "stop": stop, "take_profit": take,
                    "hold": proposal.hold, "invalidation": proposal.invalidation, "valid_from": scenario.valid_from,
                    "expires_at": scenario.expires_at, "position_hash": position_hash, "policy_version": input.policy_version}
        plan_id = stable_hash(identity)
        session = scenario.decision_session.session_date.isoformat() if scenario.decision_session else "calendar-unavailable"
        return TradePlan(plan_id, f"{input.instrument.stable_key}|{session}|{spec.strategy_id}|{proposal.action.value}|{plan_id}", input.instrument,
                         scenario.scenario_id, spec.strategy_id, spec.strategy_version, spec.parameter_hash, spec.family,
                         proposal.action, identity["quantity_intent"], tuple(identity["profiles"]), readiness, proposal.trigger,
                         proposal.confirmation, trigger_level, stop, take, proposal.hold, proposal.invalidation, evaluations,
                         proposal.evidence_features, missing, tuple(reasons), scenario.valid_from, scenario.expires_at,
                         position_hash, input.policy_version, generated_at)

    def _hold_plan(self, context: _Context, protective_spec, values, position_hash, generated_at):
        input = context.input
        candidates: list[float] = []
        if protective_spec is not None:
            cost_pct = float(protective_spec.parameters["cost_stop_pct"])
            ma60_buffer = float(protective_spec.parameters["ma60_buffer"])
            if context.cost is not None and context.cost > 0:
                candidates.append(context.cost * (1 - cost_pct))
            ma60 = context.number("closed.ma_60")
            if ma60 is not None:
                candidates.append(ma60 * (1 - ma60_buffer))
        if candidates:
            protective_line = max(candidates)
            condition = compare(feature("current.price"), ConditionOperator.GT, level("hold_above_protection", protective_line, "closed.ma_60"), "PLAN_WAITING")
            invalidation = compare(feature("current.price"), ConditionOperator.LTE, level("hold_invalidation", protective_line, "closed.ma_60"), "PROTECTIVE_EXIT_PENDING")
            evidence = ("current.price", "closed.ma_60")
        else:
            condition = compare(feature("current.price"), ConditionOperator.GT, constant(0), "PLAN_WAITING")
            invalidation = always_false("STOP_LEVEL_UNAVAILABLE")
            evidence = ("current.price",)
        spec = type("HoldSpec", (), {"strategy_id": "hold_v1", "strategy_version": "1", "parameter_hash": stable_hash({}), "family": StrategyFamily.OBSERVATION})()
        missing = () if candidates else ("protective_level",)
        reasons = ("PLAN_WAITING",) if candidates else ("PLAN_WAITING", "STOP_LEVEL_UNAVAILABLE")
        proposal = Proposal(PlanAction.HOLD, condition, None, None, None, None, None, None, TakeProfitMode.NONE, None, invalidation, condition, reasons, evidence, explicit_missing_conditions=missing)
        return self._make_plan(input, spec, proposal, values, position_hash, generated_at)

    def _observation_only(self, plan: TradePlan, generated_at: datetime) -> TradePlan:
        readiness_codes = {"PLAN_TRIGGERED", "PLAN_WAITING", "PLAN_OBSERVATION_ONLY", "BRANCH_NOT_APPLICABLE"}
        reasons = tuple(reason for reason in plan.reason_codes if reason not in readiness_codes) + ("PLAN_OBSERVATION_ONLY",)
        return TradePlan(plan.plan_id, plan.event_key, plan.instrument, plan.scenario_id, plan.strategy_id, plan.strategy_version,
                         plan.parameter_hash, plan.family, plan.action, plan.quantity_intent, plan.profiles,
                         PlanReadiness.OBSERVATION_ONLY, plan.trigger_condition, plan.confirmation_condition, plan.trigger_level,
                         plan.stop, plan.take_profit, plan.hold_condition, plan.invalidation_condition, plan.evaluations,
                         plan.evidence_features, plan.missing_conditions, tuple(sorted(set(reasons))),
                         plan.valid_from, plan.expires_at, plan.position_hash, plan.policy_version, generated_at)

    @staticmethod
    def _deduplicate(plans: list[TradePlan]) -> list[TradePlan]:
        # plan_id 是完整业务 payload 的哈希；重复仅来自同档案完全相同的规则。
        return [next(plan for plan in plans if plan.plan_id == identifier) for identifier in sorted({plan.plan_id for plan in plans})]

    @staticmethod
    def _branch(name: str, plans: tuple[TradePlan, ...]) -> StrategyBranch:
        if name == "invalidation":
            results = tuple(
                next(
                    evaluation.result
                    for evaluation in plan.evaluations
                    if evaluation.condition_id == plan.invalidation_condition.condition_id
                )
                for plan in plans
            )
            if ConditionResult.TRUE in results:
                readiness = PlanReadiness.TRIGGERED
            elif ConditionResult.UNKNOWN in results:
                readiness = PlanReadiness.OBSERVATION_ONLY
            elif plans and all(plan.action is PlanAction.WATCH for plan in plans):
                readiness = PlanReadiness.OBSERVATION_ONLY
            else:
                readiness = PlanReadiness.WAITING
            return StrategyBranch(name, plans, readiness, None)
        order = {PlanReadiness.NOT_APPLICABLE: 0, PlanReadiness.OBSERVATION_ONLY: 1, PlanReadiness.WAITING: 2, PlanReadiness.TRIGGERED: 3}
        readiness = max((plan.readiness for plan in plans), key=lambda item: order[item], default=PlanReadiness.OBSERVATION_ONLY)
        return StrategyBranch(name, plans, readiness, None)
