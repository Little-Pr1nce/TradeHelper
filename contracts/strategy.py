"""V2-5 策略层的不可变合同。

本模块只描述可回放的交易计划；它刻意不包含账户资金、仓位比例、订单或执行
等级，避免策略层越过 V2-6/V2-7 的职责边界。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .account import PositionSnapshot
from .analysis import FeatureSnapshot, FeatureStatus
from .enums import DecisionMode
from .market_data import ContractViolation, InstrumentId, ensure_finite, ensure_utc, stable_hash
from .scenario import ScenarioState, StrategyFamily, TradingScenario


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class PlanAction(_StringEnum):
    BUY = "buy"; ADD = "add"; REDUCE = "reduce"; SELL = "sell"; HOLD = "hold"; WATCH = "watch"


class QuantityIntent(_StringEnum):
    OPEN = "open"; ADD = "add"; PARTIAL_EXIT = "partial_exit"; FULL_EXIT = "full_exit"; KEEP = "keep"; NONE = "none"


class PlanReadiness(_StringEnum):
    TRIGGERED = "triggered"; WAITING = "waiting"; OBSERVATION_ONLY = "observation_only"; NOT_APPLICABLE = "not_applicable"


class ConditionResult(_StringEnum):
    TRUE = "true"; FALSE = "false"; UNKNOWN = "unknown"; PENDING_EVENT = "pending_event"; NOT_APPLICABLE = "not_applicable"


class ConditionOperator(_StringEnum):
    GT = "gt"; GTE = "gte"; LT = "lt"; LTE = "lte"; BETWEEN = "between"; EQUALS = "equals"
    CROSSES_ABOVE = "crosses_above"; CROSSES_BELOW = "crosses_below"; ALL = "all"; ANY = "any"; NOT = "not"


class TakeProfitMode(_StringEnum):
    FIXED = "fixed"; RISK_MULTIPLE = "risk_multiple"; DYNAMIC = "dynamic"; CONDITIONAL = "conditional"; NONE = "none"


class StopMode(_StringEnum):
    HARD_PRICE = "hard_price"; CLOSE_CONFIRMATION = "close_confirmation"; STRUCTURE_INVALIDATION = "structure_invalidation"


class PlanProfile(_StringEnum):
    CONSERVATIVE = "conservative"; AGGRESSIVE = "aggressive"


class PositionState(_StringEnum):
    FLAT = "flat"; HELD_PROFIT = "held_profit"; HELD_LOSS = "held_loss"; HELD_FLAT = "held_flat"; HELD_UNKNOWN = "held_unknown"


class OperandKind(_StringEnum):
    FEATURE = "feature"; CONSTANT = "constant"; DERIVED_LEVEL = "derived_level"


class EvidenceRequirement(_StringEnum):
    SNAPSHOT = "snapshot"; EVENT_SEQUENCE = "event_sequence"; SESSION_OHLC = "session_ohlc"; SESSION_VOLUME = "session_volume"


STRATEGY_REASON_CODES = frozenset("""
PLAN_TRIGGERED PLAN_WAITING PLAN_OBSERVATION_ONLY BRANCH_NOT_APPLICABLE
SCENARIO_ENTRY_BLOCKED SCENARIO_OBSERVATION_ONLY TREND_STRUCTURE_CONFIRMED
TREND_REENTRY_PENDING PULLBACK_ZONE_REACHED PULLBACK_RECLAIM_PENDING
MA120_SUPPORT_ZONE_REACHED MA120_RECLAIM_PENDING RANGE_LOWER_ZONE_REACHED
RANGE_RECLAIM_PENDING BREAKOUT_LEVEL_PENDING BREAKOUT_VOLUME_CONFIRMED
PROFIT_LOCK_TRIGGERED PROFIT_LOCK_PENDING FAILED_REBOUND_PENDING
FAILED_REBOUND_TRIGGERED PROTECTIVE_EXIT_TRIGGERED PROTECTIVE_EXIT_PENDING
STOP_LEVEL_UNAVAILABLE TAKE_PROFIT_UNQUANTIFIED FEATURE_MISSING FEATURE_STALE
FEATURE_BLOCKED FEATURE_INSUFFICIENT_HISTORY CURRENT_PRICE_EXPECTED_MISSING
EVENT_SEQUENCE_REQUIRED SESSION_OHLC_REQUIRED SESSION_VOLUME_REQUIRED
POSITION_COST_UNKNOWN COUNTERTREND_ONLY UNMODELED_FACT_UPDATE
CALENDAR_UNAVAILABLE ENTRY_EXIT_CONFLICT PROFILES_MERGED
STOP_LEVEL_DEFINED TAKE_PROFIT_QUANTIFIED
TRIGGER_OUTSIDE_ACTIONABLE_RANGE
""".split())

_FLAT_POSITION_HASH = stable_hash("flat")


def _enum(kind, value, name):
    try:
        return value if isinstance(value, kind) else kind(str(value))
    except ValueError as exc:
        raise ContractViolation(f"unsupported {name}: {value}") from exc


def _reasons(values: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(sorted(set(values)))
    if any(value not in STRATEGY_REASON_CODES for value in result):
        raise ContractViolation("unknown strategy reason code")
    return result


@dataclass(frozen=True, slots=True)
class ConditionOperand:
    kind: OperandKind
    key: str
    value: float | str | bool | None
    unit: str | None
    source_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        kind = _enum(OperandKind, self.kind, "operand kind")
        if not self.key or self.unit not in {None, "price", "ratio", "index", "boolean"}:
            raise ContractViolation("condition operand key cannot be empty")
        if kind is OperandKind.FEATURE and self.value is not None:
            raise ContractViolation("feature operand cannot embed a value")
        if kind is OperandKind.CONSTANT and self.value is None:
            raise ContractViolation("constant operand needs a value")
        if kind is OperandKind.DERIVED_LEVEL:
            if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
                raise ContractViolation("derived level operand needs a positive value")
            ensure_finite(self.value, "derived condition level", positive=True)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source_features", tuple(sorted(set(self.source_features))))


@dataclass(frozen=True, slots=True)
class ConditionExpression:
    condition_id: str
    operator: ConditionOperator
    left: ConditionOperand | None = None
    right: ConditionOperand | None = None
    lower: ConditionOperand | None = None
    upper: ConditionOperand | None = None
    children: tuple["ConditionExpression", ...] = ()
    evidence_requirement: EvidenceRequirement = EvidenceRequirement.SNAPSHOT
    reason_code: str = "PLAN_WAITING"
    schema_version: int = 1

    def __post_init__(self) -> None:
        operator = _enum(ConditionOperator, self.operator, "condition operator")
        evidence = _enum(EvidenceRequirement, self.evidence_requirement, "evidence requirement")
        logical = operator in {ConditionOperator.ALL, ConditionOperator.ANY, ConditionOperator.NOT}
        if logical:
            if not self.children or (operator is ConditionOperator.NOT and len(self.children) != 1):
                raise ContractViolation("logical condition needs valid children")
            if any(child.left is None and not child.children for child in self.children):
                raise ContractViolation("logical condition has an empty child")
            if any(value is not None for value in (self.left, self.right, self.lower, self.upper)):
                raise ContractViolation("logical condition cannot have operands")
        elif operator is ConditionOperator.BETWEEN:
            if self.children or self.left is None or self.lower is None or self.upper is None or self.right is not None:
                raise ContractViolation("between condition has invalid operands")
            if isinstance(self.lower.value, (int, float)) and isinstance(self.upper.value, (int, float)) and self.lower.value > self.upper.value:
                raise ContractViolation("condition range is inverted")
        elif self.children or self.left is None or self.right is None or self.lower is not None or self.upper is not None:
            raise ContractViolation("comparison condition has invalid operands")
        if self.schema_version < 1:
            raise ContractViolation("condition schema version must be positive")
        if operator in {ConditionOperator.CROSSES_ABOVE, ConditionOperator.CROSSES_BELOW} and evidence is EvidenceRequirement.SNAPSHOT:
            raise ContractViolation("crossing condition needs event evidence")
        if self.reason_code not in STRATEGY_REASON_CODES:
            raise ContractViolation("unknown condition reason code")
        payload = {
            "operator": operator.value, "left": self.left, "right": self.right,
            "lower": self.lower, "upper": self.upper, "children": self.children,
            "evidence_requirement": evidence.value, "schema_version": self.schema_version,
        }
        expected = stable_hash(payload)
        if self.condition_id and self.condition_id != expected:
            raise ContractViolation("condition id does not match business payload")
        object.__setattr__(self, "condition_id", expected)
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "evidence_requirement", evidence)


@dataclass(frozen=True, slots=True)
class ObservedValue:
    key: str
    value: float | str | bool | None
    status: FeatureStatus | ConditionResult
    available_at: datetime | None

    def __post_init__(self) -> None:
        if not self.key:
            raise ContractViolation("observed value key cannot be empty")
        if isinstance(self.status, (FeatureStatus, ConditionResult)):
            status = self.status
        else:
            try:
                status = FeatureStatus(str(self.status))
            except ValueError:
                status = ConditionResult(str(self.status))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "available_at", ensure_utc(self.available_at, "observed available_at") if self.available_at else None)


@dataclass(frozen=True, slots=True)
class ConditionEvaluation:
    condition_id: str
    result: ConditionResult
    observed_values: tuple[ObservedValue, ...]
    missing_features: tuple[str, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        result = _enum(ConditionResult, self.result, "condition result")
        if not self.condition_id:
            raise ContractViolation("condition evaluation needs condition id")
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "observed_values", tuple(sorted(self.observed_values, key=lambda item: item.key)))
        object.__setattr__(self, "missing_features", tuple(sorted(set(self.missing_features))))
        object.__setattr__(self, "evaluated_at", ensure_utc(self.evaluated_at, "condition evaluated_at"))


@dataclass(frozen=True, slots=True)
class DerivedPriceLevel:
    level_id: str
    value: float
    role: str
    calculation_code: str
    calculation_version: str
    source_features: tuple[str, ...]
    source_scenario_id: str

    def __post_init__(self) -> None:
        value = ensure_finite(self.value, "derived price level", positive=True)
        if not self.role or not self.calculation_code or not self.calculation_version or not self.source_scenario_id:
            raise ContractViolation("derived price level metadata cannot be empty")
        payload = {"value": value, "role": self.role, "calculation_code": self.calculation_code,
                   "calculation_version": self.calculation_version, "source_features": tuple(sorted(set(self.source_features))),
                   "source_scenario_id": self.source_scenario_id}
        expected = stable_hash(payload)
        if self.level_id and self.level_id != expected:
            raise ContractViolation("derived level id does not match business payload")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "level_id", expected)
        object.__setattr__(self, "source_features", tuple(sorted(set(self.source_features))))


@dataclass(frozen=True, slots=True)
class StopSpec:
    mode: StopMode
    level: DerivedPriceLevel | None
    condition: ConditionExpression
    reason_code: str

    def __post_init__(self) -> None:
        mode = _enum(StopMode, self.mode, "stop mode")
        if self.reason_code not in STRATEGY_REASON_CODES or (mode is StopMode.HARD_PRICE and self.level is None):
            raise ContractViolation("invalid stop specification")
        object.__setattr__(self, "mode", mode)


@dataclass(frozen=True, slots=True)
class TakeProfitSpec:
    mode: TakeProfitMode
    level: DerivedPriceLevel | None
    risk_multiple: float | None
    condition: ConditionExpression | None
    reason_code: str

    def __post_init__(self) -> None:
        mode = _enum(TakeProfitMode, self.mode, "take-profit mode")
        if self.reason_code not in STRATEGY_REASON_CODES:
            raise ContractViolation("unknown take-profit reason code")
        if mode in {TakeProfitMode.FIXED, TakeProfitMode.RISK_MULTIPLE} and self.level is None:
            raise ContractViolation("quantified take-profit needs a level")
        if mode in {TakeProfitMode.DYNAMIC, TakeProfitMode.CONDITIONAL} and self.condition is None:
            raise ContractViolation("conditional take-profit needs a condition")
        if mode is TakeProfitMode.RISK_MULTIPLE:
            ensure_finite(self.risk_multiple, "take-profit risk multiple", positive=True)
        elif self.risk_multiple is not None:
            raise ContractViolation("risk multiple only belongs to risk-multiple take-profit")
        if mode is TakeProfitMode.NONE and any(value is not None for value in (self.level, self.risk_multiple, self.condition)):
            raise ContractViolation("none take-profit cannot contain a target")
        expected_reason = (
            "TAKE_PROFIT_QUANTIFIED"
            if mode in {TakeProfitMode.FIXED, TakeProfitMode.RISK_MULTIPLE}
            else "TAKE_PROFIT_UNQUANTIFIED"
        )
        if self.reason_code != expected_reason:
            raise ContractViolation("take-profit reason does not match quantification")
        object.__setattr__(self, "mode", mode)


@dataclass(frozen=True, slots=True)
class StrategySpec:
    strategy_id: str
    strategy_version: str
    family: StrategyFamily
    position_applicability: str
    supported_actions: tuple[PlanAction, ...]
    allowed_states: tuple[ScenarioState, ...]
    required_features: tuple[str, ...]
    optional_features: tuple[str, ...]
    parameters: Mapping[str, float | int | bool | str]
    parameter_hash: str
    enabled: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        family = _enum(StrategyFamily, self.family, "strategy family")
        actions = tuple(_enum(PlanAction, item, "strategy action") for item in self.supported_actions)
        states = tuple(_enum(ScenarioState, item, "scenario state") for item in self.allowed_states)
        if self.schema_version < 1 or self.position_applicability not in {"flat", "held", "both"} or not self.strategy_id or not self.strategy_version or not actions:
            raise ContractViolation("invalid strategy spec")
        parameters = MappingProxyType(dict(sorted(self.parameters.items())))
        for name, value in parameters.items():
            if not name or not isinstance(value, (float, int, bool, str)):
                raise ContractViolation("strategy parameters must use supported scalar values")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                ensure_finite(value, f"strategy parameter {name}")
        expected = stable_hash(dict(parameters))
        if self.parameter_hash and self.parameter_hash != expected:
            raise ContractViolation("strategy parameter hash mismatch")
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "supported_actions", tuple(sorted(set(actions), key=lambda item: item.value)))
        object.__setattr__(self, "allowed_states", tuple(sorted(set(states), key=lambda item: item.value)))
        required = tuple(sorted(set(self.required_features)))
        optional = tuple(sorted(set(self.optional_features)))
        if set(required) & set(optional) or any(not item for item in required + optional):
            raise ContractViolation("strategy required and optional features must be disjoint")
        object.__setattr__(self, "required_features", required)
        object.__setattr__(self, "optional_features", optional)
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "parameter_hash", expected)


@dataclass(frozen=True, slots=True)
class StrategyInput:
    instrument: InstrumentId
    feature_snapshot: FeatureSnapshot
    trading_scenario: TradingScenario
    position_snapshot: PositionSnapshot | None
    strategy_specs: tuple[StrategySpec, ...]
    policy_version: str
    as_of: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        as_of = ensure_utc(self.as_of, "strategy as_of")
        feature, scenario = self.feature_snapshot, self.trading_scenario
        if (feature.instrument != self.instrument or scenario.instrument != self.instrument or
                feature.feature_hash != scenario.current_feature_hash or feature.mode != scenario.mode or
                feature.cutoff_at != as_of or scenario.as_of != as_of):
            raise ContractViolation("strategy input is not frozen to scenario")
        if self.position_snapshot and (self.position_snapshot.instrument != self.instrument or self.position_snapshot.captured_at > as_of):
            raise ContractViolation("strategy position does not match input")
        keys = {(item.strategy_id, item.strategy_version, item.parameter_hash) for item in self.strategy_specs}
        if (self.schema_version < 1 or not self.strategy_specs or len(keys) != len(self.strategy_specs) or not self.policy_version or
                not any(item.enabled and item.family is StrategyFamily.OBSERVATION for item in self.strategy_specs)):
            raise ContractViolation("strategy specs must be unique and policy version cannot be empty")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "strategy_specs", tuple(sorted(self.strategy_specs, key=lambda item: (item.family.value, item.strategy_id))))


_ACTION_QUANTITY = {PlanAction.BUY: QuantityIntent.OPEN, PlanAction.ADD: QuantityIntent.ADD,
                    PlanAction.REDUCE: QuantityIntent.PARTIAL_EXIT, PlanAction.SELL: QuantityIntent.FULL_EXIT,
                    PlanAction.HOLD: QuantityIntent.KEEP, PlanAction.WATCH: QuantityIntent.NONE}


@dataclass(frozen=True, slots=True)
class TradePlan:
    plan_id: str; event_key: str; instrument: InstrumentId; scenario_id: str; strategy_id: str; strategy_version: str; parameter_hash: str
    family: StrategyFamily; action: PlanAction; quantity_intent: QuantityIntent; profiles: tuple[PlanProfile, ...]; readiness: PlanReadiness
    trigger_condition: ConditionExpression; confirmation_condition: ConditionExpression | None; trigger_level: DerivedPriceLevel | None
    stop: StopSpec | None; take_profit: TakeProfitSpec | None; hold_condition: ConditionExpression | None; invalidation_condition: ConditionExpression
    evaluations: tuple[ConditionEvaluation, ...]; evidence_features: tuple[str, ...]; missing_conditions: tuple[str, ...]; reason_codes: tuple[str, ...]
    valid_from: datetime | None; expires_at: datetime | None; position_hash: str; policy_version: str; generated_at: datetime; schema_version: int = 1

    def __post_init__(self) -> None:
        family = _enum(StrategyFamily, self.family, "strategy family")
        action = _enum(PlanAction, self.action, "plan action")
        quantity = _enum(QuantityIntent, self.quantity_intent, "quantity intent")
        readiness = _enum(PlanReadiness, self.readiness, "plan readiness")
        profiles = tuple(sorted({_enum(PlanProfile, item, "profile") for item in self.profiles}, key=lambda item: item.value))
        if self.schema_version < 1 or quantity is not _ACTION_QUANTITY[action] or not profiles or not self.position_hash or not self.policy_version:
            raise ContractViolation("invalid trade plan action or identity")
        if action is PlanAction.BUY and self.position_hash != _FLAT_POSITION_HASH:
            raise ContractViolation("buy plan requires a flat position")
        if action in {PlanAction.ADD, PlanAction.REDUCE, PlanAction.SELL, PlanAction.HOLD} and self.position_hash == _FLAT_POSITION_HASH:
            raise ContractViolation("held action requires a position")
        if action in {PlanAction.BUY, PlanAction.ADD} and self.stop is None and readiness is not PlanReadiness.OBSERVATION_ONLY:
            raise ContractViolation("entry plan without stop must be observation only")
        if self.stop and self.trigger_level and action in {PlanAction.BUY, PlanAction.ADD} and self.stop.level and self.stop.level.value >= self.trigger_level.value:
            raise ContractViolation("entry stop must be below trigger")
        valid_from = ensure_utc(self.valid_from, "plan valid_from") if self.valid_from else None
        expires_at = ensure_utc(self.expires_at, "plan expires_at") if self.expires_at else None
        if readiness in {PlanReadiness.TRIGGERED, PlanReadiness.WAITING} and (valid_from is None or expires_at is None):
            raise ContractViolation("actionable plan needs a valid window")
        if (valid_from is None) != (expires_at is None):
            raise ContractViolation("plan validity window must be complete")
        if valid_from and expires_at and valid_from >= expires_at:
            raise ContractViolation("plan valid window is inverted")
        evaluations = tuple(sorted(self.evaluations, key=lambda item: item.condition_id))
        required_ids = {self.trigger_condition.condition_id, self.invalidation_condition.condition_id}
        if self.confirmation_condition: required_ids.add(self.confirmation_condition.condition_id)
        if self.stop: required_ids.add(self.stop.condition.condition_id)
        if self.hold_condition: required_ids.add(self.hold_condition.condition_id)
        if len(evaluations) != len({item.condition_id for item in evaluations}) or {item.condition_id for item in evaluations} != required_ids:
            raise ContractViolation("trade plan evaluations do not cover top-level conditions")
        if len({item.evaluated_at for item in evaluations}) > 1:
            raise ContractViolation("trade plan evaluations must share one frozen timestamp")
        readiness_code = {
            PlanReadiness.TRIGGERED: "PLAN_TRIGGERED",
            PlanReadiness.WAITING: "PLAN_WAITING",
            PlanReadiness.OBSERVATION_ONLY: "PLAN_OBSERVATION_ONLY",
            PlanReadiness.NOT_APPLICABLE: "BRANCH_NOT_APPLICABLE",
        }[readiness]
        mutually_exclusive = {"PLAN_TRIGGERED", "PLAN_WAITING", "PLAN_OBSERVATION_ONLY", "BRANCH_NOT_APPLICABLE"}
        if readiness_code not in self.reason_codes or len(mutually_exclusive & set(self.reason_codes)) != 1:
            raise ContractViolation("trade plan readiness reason is inconsistent")
        levels = tuple(item for item in (
            self.trigger_level,
            self.stop.level if self.stop else None,
            self.take_profit.level if self.take_profit else None,
        ) if item is not None)
        if any(item.source_scenario_id != self.scenario_id for item in levels):
            raise ContractViolation("trade plan price levels must belong to its scenario")
        identity = {"instrument": self.instrument, "scenario_id": self.scenario_id, "strategy_id": self.strategy_id,
                    "strategy_version": self.strategy_version, "parameter_hash": self.parameter_hash, "family": family,
                    "action": action, "quantity_intent": quantity, "profiles": profiles, "trigger": self.trigger_condition,
                    "confirmation": self.confirmation_condition, "trigger_level": self.trigger_level, "stop": self.stop,
                    "take_profit": self.take_profit, "hold": self.hold_condition, "invalidation": self.invalidation_condition,
                    "valid_from": valid_from, "expires_at": expires_at, "position_hash": self.position_hash, "policy_version": self.policy_version}
        expected = stable_hash(identity)
        if self.plan_id and self.plan_id != expected:
            raise ContractViolation("plan id does not match business payload")
        event_parts = self.event_key.split("|")
        if (len(event_parts) != 5 or event_parts[0] != self.instrument.stable_key or not event_parts[1] or
                event_parts[2] != self.strategy_id or event_parts[3] != action.value or event_parts[4] != expected):
            raise ContractViolation("plan event key does not contain plan identity")
        object.__setattr__(self, "family", family); object.__setattr__(self, "action", action)
        object.__setattr__(self, "quantity_intent", quantity); object.__setattr__(self, "readiness", readiness)
        object.__setattr__(self, "profiles", profiles); object.__setattr__(self, "plan_id", expected)
        object.__setattr__(self, "evaluations", evaluations); object.__setattr__(self, "evidence_features", tuple(sorted(set(self.evidence_features))))
        object.__setattr__(self, "missing_conditions", tuple(sorted(set(self.missing_conditions))))
        object.__setattr__(self, "reason_codes", _reasons(self.reason_codes)); object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "expires_at", expires_at); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "plan generated_at"))


@dataclass(frozen=True, slots=True)
class StrategyBranch:
    branch: str; plans: tuple[TradePlan, ...]; readiness: PlanReadiness; not_applicable_reason: str | None

    def __post_init__(self) -> None:
        if self.branch not in {"entry_or_add", "reduce_or_exit", "hold", "invalidation"}:
            raise ContractViolation("unknown strategy branch")
        readiness = _enum(PlanReadiness, self.readiness, "branch readiness")
        plans = tuple(sorted(self.plans, key=lambda item: item.plan_id))
        if readiness is PlanReadiness.NOT_APPLICABLE and (plans or self.not_applicable_reason != "BRANCH_NOT_APPLICABLE"):
            raise ContractViolation("not applicable branch must be explicit and empty")
        if readiness is not PlanReadiness.NOT_APPLICABLE:
            if self.not_applicable_reason is not None:
                raise ContractViolation("applicable branch cannot have a not-applicable reason")
            if self.branch == "invalidation":
                results = tuple(
                    next(
                        evaluation.result
                        for evaluation in plan.evaluations
                        if evaluation.condition_id == plan.invalidation_condition.condition_id
                    )
                    for plan in plans
                )
                if ConditionResult.TRUE in results:
                    expected = PlanReadiness.TRIGGERED
                elif ConditionResult.UNKNOWN in results:
                    expected = PlanReadiness.OBSERVATION_ONLY
                elif plans and all(plan.action is PlanAction.WATCH for plan in plans):
                    expected = PlanReadiness.OBSERVATION_ONLY
                else:
                    expected = PlanReadiness.WAITING
            else:
                order = {PlanReadiness.OBSERVATION_ONLY: 1, PlanReadiness.WAITING: 2, PlanReadiness.TRIGGERED: 3}
                expected = max((item.readiness for item in plans), key=lambda item: order.get(item, 0), default=PlanReadiness.OBSERVATION_ONLY)
            if readiness is not expected:
                raise ContractViolation("branch readiness does not match its plans")
        object.__setattr__(self, "readiness", readiness); object.__setattr__(self, "plans", plans)


@dataclass(frozen=True, slots=True)
class StrategyBundle:
    bundle_id: str; event_key: str; instrument: InstrumentId; scenario_id: str; position_state: PositionState
    entry_or_add: StrategyBranch; reduce_or_exit: StrategyBranch; hold: StrategyBranch; invalidation: StrategyBranch
    conservative_plan_ids: tuple[str, ...]; aggressive_plan_ids: tuple[str, ...]; conflict_state: str; reason_codes: tuple[str, ...]
    policy_version: str; generated_at: datetime; schema_version: int = 1

    def __post_init__(self) -> None:
        state = _enum(PositionState, self.position_state, "position state")
        if self.schema_version < 1 or self.conflict_state not in {"none", "entry_exit_both_triggered"}:
            raise ContractViolation("unknown strategy bundle conflict state")
        if (self.entry_or_add.branch, self.reduce_or_exit.branch, self.hold.branch, self.invalidation.branch) != (
            "entry_or_add", "reduce_or_exit", "hold", "invalidation"
        ):
            raise ContractViolation("strategy bundle branches are misplaced")
        plans = self.entry_or_add.plans + self.reduce_or_exit.plans + self.hold.plans + self.invalidation.plans
        if not plans or any(item.instrument != self.instrument or item.scenario_id != self.scenario_id or item.policy_version != self.policy_version for item in plans):
            raise ContractViolation("strategy bundle contains foreign plans")
        position_hashes = {item.position_hash for item in plans}
        if len(position_hashes) != 1:
            raise ContractViolation("strategy bundle mixes position snapshots")
        main_plans = self.entry_or_add.plans + self.reduce_or_exit.plans + self.hold.plans
        expected_conservative = tuple(sorted({item.plan_id for item in main_plans if PlanProfile.CONSERVATIVE in item.profiles}))
        expected_aggressive = tuple(sorted({item.plan_id for item in main_plans if PlanProfile.AGGRESSIVE in item.profiles}))
        if tuple(sorted(set(self.conservative_plan_ids))) != expected_conservative or tuple(sorted(set(self.aggressive_plan_ids))) != expected_aggressive:
            raise ContractViolation("bundle profile ids do not match plans")
        if state is PositionState.FLAT and (self.reduce_or_exit.readiness is not PlanReadiness.NOT_APPLICABLE or self.hold.readiness is not PlanReadiness.NOT_APPLICABLE):
            raise ContractViolation("flat bundle must mark held branches not applicable")
        position_hash = next(iter(position_hashes))
        if (state is PositionState.FLAT) != (position_hash == _FLAT_POSITION_HASH):
            raise ContractViolation("bundle position state and position hash disagree")
        if self.conflict_state == "entry_exit_both_triggered":
            if ("ENTRY_EXIT_CONFLICT" not in self.reason_codes or
                    not any(item.action in {PlanAction.BUY, PlanAction.ADD} and item.readiness is PlanReadiness.OBSERVATION_ONLY for item in self.entry_or_add.plans) or
                    not any(item.readiness is PlanReadiness.TRIGGERED for item in self.reduce_or_exit.plans)):
                raise ContractViolation("bundle conflict state does not preserve the demoted entry and triggered exit")
        elif "ENTRY_EXIT_CONFLICT" in self.reason_codes:
            raise ContractViolation("bundle conflict reason requires conflict state")
        identity_plans = main_plans
        branches = (self.entry_or_add, self.reduce_or_exit, self.hold, self.invalidation)
        identity = {
            "scenario_id": self.scenario_id,
            "position_hash": position_hash,
            "plan_ids": tuple(sorted({item.plan_id for item in identity_plans})),
            "branches": tuple((branch.branch, branch.readiness, tuple(item.plan_id for item in branch.plans)) for branch in branches),
            "conflict_state": self.conflict_state,
            "policy_version": self.policy_version,
        }
        expected = stable_hash(identity)
        sessions = {item.event_key.split("|")[1] for item in plans}
        expected_event = f"{self.instrument.stable_key}|{next(iter(sessions))}|{expected}" if len(sessions) == 1 else ""
        if self.bundle_id != expected or self.event_key != expected_event:
            raise ContractViolation("bundle identity does not match business payload")
        object.__setattr__(self, "position_state", state)
        object.__setattr__(self, "conservative_plan_ids", tuple(sorted(set(self.conservative_plan_ids))))
        object.__setattr__(self, "aggressive_plan_ids", tuple(sorted(set(self.aggressive_plan_ids))))
        object.__setattr__(self, "reason_codes", _reasons(self.reason_codes))
        object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "bundle generated_at"))
