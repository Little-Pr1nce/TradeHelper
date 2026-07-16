"""V2-7 成交层的不可变合同。

订单意图是风控决定与成交证据之间唯一的桥梁。本模块不保存可变账户状态，
也不把策略的 ``entry_price`` 误解释成限价单价格。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN
from enum import Enum

from .account import as_decimal
from .enums import DecisionMode, Market
from .market_data import ContractViolation, InstrumentId, ensure_utc, stable_hash
from .risk import DecisionDisposition, ExecutionDecision, ExecutionLevel, RiskDecisionBundle, RiskProfile
from .strategy import ConditionEvaluation, ConditionExpression, PlanAction, QuantityIntent, TradePlan


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class OrderSide(_StringEnum): BUY = "buy"; SELL = "sell"
class OrderStyle(_StringEnum): MARKET_ON_ACTIVATION = "market_on_activation"
class IntentState(_StringEnum): READY = "ready"; STAGED = "staged"
class IntentBuildStatus(_StringEnum): CREATED = "created"; NO_ORDER = "no_order"
class ExecutionMode(_StringEnum): CURRENT_PREVIEW = "current_preview"; HISTORICAL_REPLAY = "historical_replay"
class EventGranularity(_StringEnum): QUOTE = "quote"; INTRADAY_BAR = "intraday_bar"; DAILY_BAR = "daily_bar"
class TradingStatus(_StringEnum): OPEN = "open"; SUSPENDED = "suspended"; UNKNOWN = "unknown"
class TriggerState(_StringEnum): READY = "ready"; TRIGGERED = "triggered"; NOT_TRIGGERED = "not_triggered"; INVALIDATED = "invalidated"; EXPIRED = "expired"; UNVERIFIABLE = "unverifiable"
class FillOutcome(_StringEnum): PREVIEW_ONLY = "preview_only"; FILLED = "filled"; PARTIAL = "partial"; REJECTED = "rejected"; NOT_TRIGGERED = "not_triggered"; INVALIDATED = "invalidated"; EXPIRED = "expired"; UNVERIFIABLE = "unverifiable"
class PathAssumption(_StringEnum): EXACT_SEQUENCE = "exact_sequence"; POINT_SNAPSHOT = "point_snapshot"; GAP_AT_OPEN = "gap_at_open"; CONSERVATIVE_STOP_FIRST = "conservative_stop_first"; STRICT_UNKNOWN = "strict_unknown"
class ExecutionEvidenceGrade(_StringEnum): HIGH = "high"; MEDIUM = "medium"; LOW = "low"; INSUFFICIENT = "insufficient"
class PreviewStatus(_StringEnum): READY = "ready"; STAGED = "staged"; RECHECK_REQUIRED = "recheck_required"; REJECTED = "rejected"; EXPIRED = "expired"


EXECUTION_REASON_CODES = frozenset("""
EXEC_INTENT_CREATED EXEC_NO_ORDER_LEVEL_C EXEC_NO_ORDER_LEVEL_D EXEC_NO_ORDER_ACTION
EXEC_NO_APPROVED_SHARES EXEC_REQUESTED_SHARES_REDUCED EXEC_DECISION_STALE EXEC_PLAN_EXPIRED
EXEC_TRIGGERED EXEC_NOT_TRIGGERED EXEC_INVALIDATED EXEC_SEQUENCE_REQUIRED EXEC_SEQUENCE_AMBIGUOUS
EXEC_DAILY_RANGE_ONLY EXEC_CURRENT_PREVIEW_ONLY EXEC_FRESH_QUOTE_REQUIRED EXEC_GAP_TRIGGERED
EXEC_GAP_STOP EXEC_STOP_TRIGGERED EXEC_TAKE_PROFIT_TRIGGERED EXEC_STOP_FIRST_CONSERVATIVE
EXEC_T1_BLOCKED EXEC_PARTIAL_SELLABLE EXEC_LIMIT_QUEUE_UNVERIFIABLE EXEC_SUSPENDED
EXEC_TRADING_STATUS_UNKNOWN EXEC_NO_TRADABLE_VOLUME EXEC_CASH_REDUCED EXEC_CASH_INSUFFICIENT
EXEC_POSITION_MISMATCH EXEC_LOT_ROUNDED EXEC_LIQUIDITY_CAPPED EXEC_LIQUIDITY_EVIDENCE_MISSING
EXEC_NO_LEVEL2_DEPTH EXEC_BASE_SLIPPAGE_APPLIED EXEC_VOLATILITY_SLIPPAGE_APPLIED
EXEC_LIQUIDITY_SLIPPAGE_APPLIED EXEC_COMMISSION_APPLIED EXEC_SELL_TAX_APPLIED EXEC_PARTIAL_FILL
EXEC_FULL_FILL EXEC_UNFILLED_REMAINDER EXEC_EVIDENCE_HIGH EXEC_EVIDENCE_MEDIUM EXEC_EVIDENCE_LOW
EXEC_EVIDENCE_INSUFFICIENT EXEC_HISTORICAL_ONLY EXEC_HARD_LIMIT_IMMUTABLE
EXEC_PORTFOLIO_NOT_ALLOCATED
""".split())


def _enum(kind, value, name):
    try:
        return value if isinstance(value, kind) else kind(str(value))
    except ValueError as exc:
        raise ContractViolation(f"unsupported {name}: {value}") from exc


def _decimal(value, name, *, positive=False, nonnegative=False) -> Decimal:
    value = as_decimal(value, name)
    if positive and value <= 0: raise ContractViolation(f"{name} must be positive")
    if nonnegative and value < 0: raise ContractViolation(f"{name} cannot be negative")
    return value


def _reasons(values: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(sorted(set(values)))
    if any(value not in EXECUTION_REASON_CODES for value in result):
        raise ContractViolation("unknown execution reason code")
    return result


def _hash(value: str | None, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional: return None
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ContractViolation(f"{name} must be a SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    policy_version: str = "execution_policy_v1"
    base_slippage: Decimal = Decimal("0.003")
    volatility_threshold: Decimal = Decimal("0.30")
    volatility_factor: Decimal = Decimal("0.01")
    max_volatility_extra: Decimal = Decimal("0.007")
    free_participation: Decimal = Decimal("0.01")
    max_participation: Decimal = Decimal("0.05")
    max_liquidity_extra: Decimal = Decimal("0.005")
    missing_liquidity_reserve: Decimal = Decimal("0.005")
    ambiguity_mode: str = "strict"
    us_fill_quantum: Decimal = Decimal("0.0001")
    a_fill_quantum: Decimal = Decimal("0.01")
    currency_quantum: Decimal = Decimal("0.01")
    hard_constraint_version: str = "execution_hard_v1"
    parameter_hash: str = ""

    def __post_init__(self):
        if not self.policy_version or self.ambiguity_mode != "strict" or self.hard_constraint_version != "execution_hard_v1":
            raise ContractViolation("execution hard constraints cannot be disabled")
        for name in ("base_slippage", "volatility_threshold", "volatility_factor", "max_volatility_extra", "free_participation", "max_participation", "max_liquidity_extra", "missing_liquidity_reserve", "us_fill_quantum", "a_fill_quantum", "currency_quantum"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True))
        if (self.base_slippage != Decimal("0.003") or self.max_participation != Decimal("0.05") or
                self.free_participation != Decimal("0.01") or self.max_liquidity_extra != Decimal("0.005") or
                self.missing_liquidity_reserve != Decimal("0.005") or self.a_fill_quantum != Decimal("0.01") or
                self.us_fill_quantum != Decimal("0.0001") or self.currency_quantum != Decimal("0.01")):
            raise ContractViolation("execution hard policy values are immutable")
        payload = {key: getattr(self, key) for key in self.__dataclass_fields__ if key != "parameter_hash"}
        expected = stable_hash(payload)
        if self.parameter_hash and self.parameter_hash != expected: raise ContractViolation("execution policy hash mismatch")
        object.__setattr__(self, "parameter_hash", expected)


@dataclass(frozen=True, slots=True)
class OrderIntentRequest:
    trade_plan: TradePlan
    execution_decision: ExecutionDecision
    risk_decision_bundle: RiskDecisionBundle
    requested_shares: Decimal | None
    requested_at: datetime
    execution_policy: ExecutionPolicy
    schema_version: int = 1

    def __post_init__(self):
        plan, decision, bundle = self.trade_plan, self.execution_decision, self.risk_decision_bundle
        requested = decision.approved_shares if self.requested_shares is None else _decimal(self.requested_shares, "requested shares", nonnegative=True)
        requested_at = ensure_utc(self.requested_at, "requested_at")
        if (self.schema_version != 1 or decision.decision_id not in {item.decision_id for item in bundle.decisions} or
                plan.plan_id != decision.plan_id or plan.instrument != decision.instrument or plan.scenario_id != decision.scenario_id or
                plan.action is not decision.action or plan.quantity_intent is not decision.quantity_intent or
                decision.bundle_id != bundle.strategy_bundle_id or decision.account_hash != bundle.account_hash or
                decision.valuation_id != bundle.valuation_id or decision.quality_hash != bundle.quality_hash or
                decision.market_rule_version != bundle.market_rule_version or decision.risk_policy_version != bundle.risk_policy_version):
            raise ContractViolation("order intent request identity mismatch")
        if requested > decision.approved_shares: raise ContractViolation("requested shares cannot exceed risk approval")
        if requested_at < max(plan.generated_at, decision.generated_at, bundle.generated_at): raise ContractViolation("order intent request cannot predate frozen inputs")
        object.__setattr__(self, "requested_shares", requested)
        object.__setattr__(self, "requested_at", requested_at)


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: str; event_key: str; instrument: InstrumentId; scenario_id: str; strategy_bundle_id: str; risk_bundle_id: str; plan_id: str; decision_id: str
    profile: RiskProfile; action: PlanAction; quantity_intent: QuantityIntent; side: OrderSide; order_style: OrderStyle; state: IntentState
    requested_shares: Decimal; risk_approved_shares: Decimal; trigger_condition: ConditionExpression; confirmation_condition: ConditionExpression | None; invalidation_condition: ConditionExpression
    condition_evaluations: tuple[ConditionEvaluation, ...]
    trigger_level: Decimal | None; stop: Decimal | None; take_profit: Decimal | None; valid_from: datetime; expires_at: datetime; earliest_execution_at: datetime
    account_hash: str | None; valuation_id: str | None; quality_hash: str; evidence_hash: str; market_rule_version: str; risk_policy_version: str; execution_policy_version: str; generated_at: datetime; schema_version: int = 1

    def __post_init__(self):
        action = _enum(PlanAction, self.action, "intent action"); side = _enum(OrderSide, self.side, "order side"); state = _enum(IntentState, self.state, "intent state"); style = _enum(OrderStyle, self.order_style, "order style")
        profile = _enum(RiskProfile, self.profile, "intent profile"); quantity = _enum(QuantityIntent, self.quantity_intent, "intent quantity")
        requested = _decimal(self.requested_shares, "requested shares", positive=True); approved = _decimal(self.risk_approved_shares, "approved shares", positive=True)
        valid, expires, earliest = ensure_utc(self.valid_from, "intent valid_from"), ensure_utc(self.expires_at, "intent expires_at"), ensure_utc(self.earliest_execution_at, "earliest_execution_at")
        expected_side = OrderSide.BUY if action in {PlanAction.BUY, PlanAction.ADD} else OrderSide.SELL
        if (self.schema_version != 1 or action not in {PlanAction.BUY, PlanAction.ADD, PlanAction.REDUCE, PlanAction.SELL} or side is not expected_side or
                style is not OrderStyle.MARKET_ON_ACTIVATION or requested > approved or valid >= expires or earliest < valid or
                not self.strategy_bundle_id or not self.risk_bundle_id or not self.execution_policy_version or
                quantity.value != {PlanAction.BUY: "open", PlanAction.ADD: "add", PlanAction.REDUCE: "partial_exit", PlanAction.SELL: "full_exit"}[action]):
            raise ContractViolation("invalid order intent")
        evaluations = tuple(sorted(self.condition_evaluations, key=lambda item: item.condition_id))
        required = {self.trigger_condition.condition_id, self.invalidation_condition.condition_id}
        if self.confirmation_condition: required.add(self.confirmation_condition.condition_id)
        if not required.issubset({item.condition_id for item in evaluations}): raise ContractViolation("intent is missing frozen condition evaluations")
        for name in ("trigger_level", "stop", "take_profit"):
            value = getattr(self, name); object.__setattr__(self, name, None if value is None else _decimal(value, name, positive=True))
        account_hash = _hash(self.account_hash, "intent account hash", optional=True); valuation_id = _hash(self.valuation_id, "intent valuation id", optional=True)
        quality_hash = _hash(self.quality_hash, "intent quality hash"); evidence_hash = _hash(self.evidence_hash, "intent evidence hash")
        payload = {"instrument": self.instrument, "scenario_id": self.scenario_id, "strategy_bundle_id": self.strategy_bundle_id, "risk_bundle_id": self.risk_bundle_id, "plan_id": self.plan_id, "decision_id": self.decision_id, "profile": profile, "action": action, "quantity_intent": quantity, "side": side, "order_style": style, "state": state, "requested_shares": requested, "risk_approved_shares": approved, "trigger_condition": self.trigger_condition, "confirmation_condition": self.confirmation_condition, "invalidation_condition": self.invalidation_condition, "condition_evaluations": evaluations, "trigger_level": self.trigger_level, "stop": self.stop, "take_profit": self.take_profit, "valid_from": valid, "expires_at": expires, "earliest_execution_at": earliest, "account_hash": account_hash, "valuation_id": valuation_id, "quality_hash": quality_hash, "evidence_hash": evidence_hash, "market_rule_version": self.market_rule_version, "risk_policy_version": self.risk_policy_version, "execution_policy_version": self.execution_policy_version}
        expected = stable_hash(payload)
        if self.intent_id != expected or not self.event_key.endswith(expected): raise ContractViolation("order intent identity mismatch")
        object.__setattr__(self, "profile", profile); object.__setattr__(self, "quantity_intent", quantity); object.__setattr__(self, "condition_evaluations", evaluations); object.__setattr__(self, "account_hash", account_hash); object.__setattr__(self, "valuation_id", valuation_id); object.__setattr__(self, "quality_hash", quality_hash); object.__setattr__(self, "evidence_hash", evidence_hash)
        object.__setattr__(self, "action", action); object.__setattr__(self, "side", side); object.__setattr__(self, "state", state); object.__setattr__(self, "order_style", style); object.__setattr__(self, "requested_shares", requested); object.__setattr__(self, "risk_approved_shares", approved); object.__setattr__(self, "valid_from", valid); object.__setattr__(self, "expires_at", expires); object.__setattr__(self, "earliest_execution_at", earliest); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "intent generated_at"))


@dataclass(frozen=True, slots=True)
class OrderIntentBuildRecord:
    build_id: str; decision_id: str; plan_id: str; status: IntentBuildStatus; intent_id: str | None; reasons: tuple[str, ...]; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        status = _enum(IntentBuildStatus, self.status, "build status"); reasons = _reasons(self.reasons)
        if self.schema_version != 1 or (status is IntentBuildStatus.CREATED) != (self.intent_id is not None): raise ContractViolation("invalid intent build record")
        expected = stable_hash({"decision_id": self.decision_id, "plan_id": self.plan_id, "status": status, "intent_id": self.intent_id, "reasons": reasons})
        if self.build_id != expected: raise ContractViolation("intent build record identity mismatch")
        object.__setattr__(self, "status", status); object.__setattr__(self, "reasons", reasons); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "build generated_at"))


@dataclass(frozen=True, slots=True)
class OrderIntentBundle:
    intent_bundle_id: str; risk_bundle_id: str; records: tuple[OrderIntentBuildRecord, ...]; intents: tuple[OrderIntent, ...]; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        records = tuple(sorted(self.records, key=lambda item: item.decision_id)); intents = tuple(sorted(self.intents, key=lambda item: item.intent_id))
        if self.schema_version != 1 or len({item.decision_id for item in records}) != len(records) or {item.intent_id for item in intents} != {item.intent_id for item in records if item.intent_id} or any(item.risk_bundle_id != self.risk_bundle_id for item in intents):
            raise ContractViolation("intent bundle references are inconsistent")
        expected = stable_hash({"risk_bundle_id": self.risk_bundle_id, "records": records, "intent_ids": tuple(item.intent_id for item in intents)})
        if self.intent_bundle_id != expected: raise ContractViolation("intent bundle identity mismatch")
        object.__setattr__(self, "records", records); object.__setattr__(self, "intents", intents); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "bundle generated_at"))


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    event_id: str; instrument: InstrumentId; session_date: date; interval_start: datetime; interval_end: datetime; granularity: EventGranularity
    open: Decimal; high: Decimal; low: Decimal; close: Decimal; volume: Decimal | None; previous_close: Decimal | None; bid: Decimal | None; ask: Decimal | None
    trading_status: TradingStatus; source: str; source_evidence_quality: str; available_at: datetime; generated_at: datetime
    def __post_init__(self):
        start, end = ensure_utc(self.interval_start, "event interval_start"), ensure_utc(self.interval_end, "event interval_end")
        granularity = _enum(EventGranularity, self.granularity, "event granularity"); status = _enum(TradingStatus, self.trading_status, "trading status")
        values = {name: _decimal(getattr(self, name), name, positive=True) for name in ("open", "high", "low", "close")}
        if values["low"] > min(values["open"], values["close"], values["high"]) or values["high"] < max(values["open"], values["close"], values["low"]) or end < start: raise ContractViolation("event OHLC or interval is invalid")
        if granularity is EventGranularity.QUOTE and (start != end or len(set(values.values())) != 1): raise ContractViolation("quote event must be a point price")
        volume = None if self.volume is None else _decimal(self.volume, "event volume", nonnegative=True)
        for name in ("previous_close", "bid", "ask"):
            value = getattr(self, name); object.__setattr__(self, name, None if value is None else _decimal(value, name, positive=True))
        if self.bid is not None and self.ask is not None and self.bid > self.ask: raise ContractViolation("event bid cannot exceed ask")
        if not self.event_id or not self.source or not self.source_evidence_quality: raise ContractViolation("event evidence is incomplete")
        available = ensure_utc(self.available_at, "event available_at")
        if granularity is not EventGranularity.QUOTE and available < end: raise ContractViolation("bar cannot be available before it is complete")
        object.__setattr__(self, "interval_start", start); object.__setattr__(self, "interval_end", end); object.__setattr__(self, "granularity", granularity); object.__setattr__(self, "trading_status", status); object.__setattr__(self, "volume", volume); object.__setattr__(self, "available_at", available); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "event generated_at"))
        for name, value in values.items(): object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class ExecutionState:
    market: Market; currency: str; cash: Decimal; position_shares: Decimal; sellable_shares: Decimal | None; average_cost: Decimal | None; acquired_session_date: date | None; active_stop: Decimal | None; active_take_profit: Decimal | None; captured_at: datetime; source: str; account_hash: str | None = None; source_hash: str | None = None
    def __post_init__(self):
        market = self.market if isinstance(self.market, Market) else Market(str(self.market).upper()); cash = _decimal(self.cash, "cash", nonnegative=True); shares = _decimal(self.position_shares, "position shares", nonnegative=True)
        sellable = None if self.sellable_shares is None else _decimal(self.sellable_shares, "sellable shares", nonnegative=True)
        if sellable is not None and sellable > shares: raise ContractViolation("sellable shares exceed position")
        for name in ("average_cost", "active_stop", "active_take_profit"):
            value = getattr(self, name); object.__setattr__(self, name, None if value is None else _decimal(value, name, positive=True))
        if self.currency.upper() != ("CNY" if market is Market.A else "USD") or not self.source: raise ContractViolation("execution state market or source is invalid")
        captured = ensure_utc(self.captured_at, "state captured_at"); account_hash = _hash(self.account_hash, "state account hash", optional=True)
        identity = {"market": market, "currency": self.currency.upper(), "cash": cash, "position_shares": shares, "sellable_shares": sellable, "average_cost": self.average_cost, "acquired_session_date": self.acquired_session_date, "active_stop": self.active_stop, "active_take_profit": self.active_take_profit, "captured_at": captured, "source": self.source, "account_hash": account_hash}
        source_hash = stable_hash(identity)
        if self.source_hash and self.source_hash != source_hash: raise ContractViolation("execution state source hash mismatch")
        object.__setattr__(self, "market", market); object.__setattr__(self, "currency", self.currency.upper()); object.__setattr__(self, "cash", cash); object.__setattr__(self, "position_shares", shares); object.__setattr__(self, "sellable_shares", sellable); object.__setattr__(self, "captured_at", captured); object.__setattr__(self, "account_hash", account_hash); object.__setattr__(self, "source_hash", source_hash)


@dataclass(frozen=True, slots=True)
class ExecutionStateDelta:
    cash_delta: Decimal; position_delta: Decimal; sellable_delta: Decimal | None; average_cost: Decimal | None; active_stop: Decimal | None; active_take_profit: Decimal | None; reason_codes: tuple[str, ...]; acquired_session_date: date | None = None
    def __post_init__(self):
        object.__setattr__(self, "cash_delta", _decimal(self.cash_delta, "cash delta")); object.__setattr__(self, "position_delta", _decimal(self.position_delta, "position delta")); object.__setattr__(self, "sellable_delta", None if self.sellable_delta is None else _decimal(self.sellable_delta, "sellable delta"))
        for name in ("average_cost", "active_stop", "active_take_profit"):
            value = getattr(self, name)
            object.__setattr__(self, name, None if value is None else _decimal(value, name, positive=True))
        object.__setattr__(self, "reason_codes", _reasons(self.reason_codes))


@dataclass(frozen=True, slots=True)
class TriggerEvaluation:
    trigger_evaluation_id: str; event_key: str; intent_id: str; state: TriggerState; evaluated_event_ids: tuple[str, ...]; event_batch_hash: str
    trigger_event_id: str | None; invalidation_event_id: str | None; evaluated_from: datetime | None; evaluated_to: datetime | None
    triggered_at: datetime | None; invalidated_at: datetime | None; source: str; granularity: EventGranularity | None; path_assumption: PathAssumption
    evidence_grade: ExecutionEvidenceGrade; execution_policy_version: str; reason_codes: tuple[str, ...]; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        state = _enum(TriggerState, self.state, "trigger state"); path = _enum(PathAssumption, self.path_assumption, "path assumption"); grade = _enum(ExecutionEvidenceGrade, self.evidence_grade, "evidence grade")
        granularity = None if self.granularity is None else _enum(EventGranularity, self.granularity, "event granularity")
        triggered = ensure_utc(self.triggered_at, "triggered_at") if self.triggered_at else None; invalidated = ensure_utc(self.invalidated_at, "invalidated_at") if self.invalidated_at else None
        if state is TriggerState.TRIGGERED:
            if not (triggered and self.trigger_event_id) or invalidated or self.invalidation_event_id: raise ContractViolation("triggered evaluation needs only trigger evidence")
        elif state is TriggerState.INVALIDATED:
            if not (invalidated and self.invalidation_event_id) or triggered or self.trigger_event_id: raise ContractViolation("invalidated evaluation needs only invalidation evidence")
        elif any((triggered, invalidated, self.trigger_event_id, self.invalidation_event_id)): raise ContractViolation("non-terminal evaluation cannot claim trigger evidence")
        ids = tuple(self.evaluated_event_ids)
        if len(ids) != len(set(ids)) or not self.event_batch_hash or not self.source or not self.execution_policy_version: raise ContractViolation("trigger evaluation evidence is incomplete")
        begin = ensure_utc(self.evaluated_from, "evaluated_from") if self.evaluated_from else None; end = ensure_utc(self.evaluated_to, "evaluated_to") if self.evaluated_to else None
        if begin and end and begin > end: raise ContractViolation("trigger evaluation range is inverted")
        reasons = _reasons(self.reason_codes)
        expected = stable_hash({"intent_id": self.intent_id, "event_batch_hash": self.event_batch_hash, "state": state, "evaluated_event_ids": ids, "trigger_event_id": self.trigger_event_id, "invalidation_event_id": self.invalidation_event_id, "path": path, "grade": grade, "policy": self.execution_policy_version})
        if self.trigger_evaluation_id != expected or not self.event_key.endswith(expected): raise ContractViolation("trigger evaluation identity mismatch")
        object.__setattr__(self, "state", state); object.__setattr__(self, "path_assumption", path); object.__setattr__(self, "evidence_grade", grade); object.__setattr__(self, "granularity", granularity); object.__setattr__(self, "evaluated_event_ids", ids); object.__setattr__(self, "triggered_at", triggered); object.__setattr__(self, "invalidated_at", invalidated); object.__setattr__(self, "evaluated_from", begin); object.__setattr__(self, "evaluated_to", end); object.__setattr__(self, "reason_codes", reasons); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "trigger evaluation generated_at"))


@dataclass(frozen=True, slots=True)
class CurrentOrderPreview:
    preview_id: str; intent_id: str; status: PreviewStatus; reference_price: Decimal | None; estimated_fill_low: Decimal | None; estimated_fill_high: Decimal | None; estimated_costs: Decimal | None; requested_shares: Decimal; max_preview_shares: Decimal; evidence_grade: ExecutionEvidenceGrade; reason_codes: tuple[str, ...]; observed_at: datetime; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        status = _enum(PreviewStatus, self.status, "preview status"); grade = _enum(ExecutionEvidenceGrade, self.evidence_grade, "preview evidence grade")
        requested = _decimal(self.requested_shares, "preview requested shares", positive=True); maximum = _decimal(self.max_preview_shares, "preview maximum shares", nonnegative=True)
        fields = ("reference_price", "estimated_fill_low", "estimated_fill_high", "estimated_costs")
        for name in fields:
            value = getattr(self, name); object.__setattr__(self, name, None if value is None else _decimal(value, name, nonnegative=True))
        if maximum > requested: raise ContractViolation("preview shares cannot exceed the frozen request")
        if self.estimated_fill_low is not None and self.estimated_fill_high is not None and self.estimated_fill_low > self.estimated_fill_high: raise ContractViolation("preview fill range is inverted")
        expected = stable_hash({"intent_id": self.intent_id, "status": status, "reference_price": self.reference_price, "estimated_fill_low": self.estimated_fill_low, "estimated_fill_high": self.estimated_fill_high, "estimated_costs": self.estimated_costs, "requested": requested, "maximum": maximum, "grade": grade, "reasons": _reasons(self.reason_codes), "observed_at": ensure_utc(self.observed_at, "preview observed_at")})
        if self.preview_id != expected: raise ContractViolation("preview identity mismatch")
        object.__setattr__(self, "status", status); object.__setattr__(self, "evidence_grade", grade); object.__setattr__(self, "requested_shares", requested); object.__setattr__(self, "max_preview_shares", maximum); object.__setattr__(self, "reason_codes", _reasons(self.reason_codes)); object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "preview observed_at")); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "preview generated_at"))


@dataclass(frozen=True, slots=True)
class LiquidityEvidence:
    median_daily_volume_20: Decimal | None; annualized_volatility_20: Decimal | None; cutoff_at: datetime; source: str; evidence_hash: str
    def __post_init__(self):
        for name in ("median_daily_volume_20", "annualized_volatility_20"):
            value = getattr(self, name); object.__setattr__(self, name, None if value is None else _decimal(value, name, nonnegative=True))
        if not self.source: raise ContractViolation("liquidity source cannot be empty")
        expected = stable_hash({"median_daily_volume_20": self.median_daily_volume_20, "annualized_volatility_20": self.annualized_volatility_20, "cutoff_at": ensure_utc(self.cutoff_at, "liquidity cutoff"), "source": self.source})
        if self.evidence_hash != expected: raise ContractViolation("liquidity evidence identity mismatch")
        object.__setattr__(self, "cutoff_at", ensure_utc(self.cutoff_at, "liquidity cutoff"))


@dataclass(frozen=True, slots=True)
class FillEvidence:
    fill_id: str; event_key: str; run_id: str; intent_id: str; decision_id: str; plan_id: str; instrument: InstrumentId; action: PlanAction; side: OrderSide; outcome: FillOutcome
    requested_shares: Decimal; filled_shares: Decimal; unfilled_shares: Decimal; raw_price: Decimal | None; slippage_rate: Decimal | None; fill_price: Decimal | None
    gross_value: Decimal | None; commission: Decimal | None; sell_tax: Decimal | None; total_fee: Decimal | None; cash_delta: Decimal | None
    triggered_at: datetime | None; filled_at: datetime | None; source: str; granularity: EventGranularity | None; path_assumption: PathAssumption; evidence_grade: ExecutionEvidenceGrade
    market_rule_version: str; execution_policy_version: str; reason_codes: tuple[str, ...]; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        action = _enum(PlanAction, self.action, "fill action"); side = _enum(OrderSide, self.side, "fill side"); outcome = _enum(FillOutcome, self.outcome, "fill outcome"); path = _enum(PathAssumption, self.path_assumption, "fill path"); grade = _enum(ExecutionEvidenceGrade, self.evidence_grade, "fill evidence grade")
        granularity = None if self.granularity is None else _enum(EventGranularity, self.granularity, "fill granularity")
        requested = _decimal(self.requested_shares, "fill requested shares", positive=True); filled = _decimal(self.filled_shares, "filled shares", nonnegative=True); unfilled = _decimal(self.unfilled_shares, "unfilled shares", nonnegative=True)
        monetary = ("raw_price", "slippage_rate", "fill_price", "gross_value", "commission", "sell_tax", "total_fee")
        for name in monetary:
            value = getattr(self, name); object.__setattr__(self, name, None if value is None else _decimal(value, name, nonnegative=True))
        cash = None if self.cash_delta is None else _decimal(self.cash_delta, "cash delta")
        completed = outcome in {FillOutcome.FILLED, FillOutcome.PARTIAL}
        if (completed and (filled <= 0 or filled + unfilled != requested or any(getattr(self, item) is None for item in ("raw_price", "slippage_rate", "fill_price", "gross_value", "commission", "sell_tax", "total_fee")))) or (not completed and (filled != 0 or unfilled != requested or any(getattr(self, item) is not None for item in ("raw_price", "slippage_rate", "fill_price", "gross_value", "commission", "sell_tax", "total_fee", "cash_delta")))) or (outcome is FillOutcome.FILLED and unfilled != 0) or (outcome is FillOutcome.PARTIAL and unfilled <= 0):
            raise ContractViolation("fill outcome and monetary fields are inconsistent")
        if completed and ((side is OrderSide.BUY and (cash is None or cash >= 0)) or (side is OrderSide.SELL and (cash is None or cash <= 0))): raise ContractViolation("fill cash direction is invalid")
        if completed:
            if self.gross_value != self.fill_price * filled or self.total_fee != self.commission + self.sell_tax:
                raise ContractViolation("fill monetary amounts cannot be reproduced")
            expected_cash = -(self.gross_value + self.total_fee) if side is OrderSide.BUY else self.gross_value - self.total_fee
            if cash != expected_cash: raise ContractViolation("fill cash delta cannot be reproduced")
            adverse = self.raw_price * (Decimal("1") + self.slippage_rate if side is OrderSide.BUY else Decimal("1") - self.slippage_rate)
            if (side is OrderSide.BUY and self.fill_price < adverse) or (side is OrderSide.SELL and self.fill_price > adverse):
                raise ContractViolation("fill price is not adverse after slippage")
            if self.triggered_at is None or self.filled_at is None: raise ContractViolation("completed fill needs trigger and fill timestamps")
        reasons = _reasons(self.reason_codes)
        expected = stable_hash({"run_id": self.run_id, "intent_id": self.intent_id, "decision_id": self.decision_id, "plan_id": self.plan_id, "instrument": self.instrument, "action": action, "side": side, "outcome": outcome, "requested": requested, "filled": filled, "unfilled": unfilled, "raw": self.raw_price, "slippage": self.slippage_rate, "fill": self.fill_price, "gross": self.gross_value, "commission": self.commission, "sell_tax": self.sell_tax, "fee": self.total_fee, "cash": cash, "triggered_at": self.triggered_at, "filled_at": self.filled_at, "source": self.source, "granularity": granularity, "path": path, "grade": grade, "rule": self.market_rule_version, "policy": self.execution_policy_version, "reasons": reasons})
        if self.fill_id != expected or not self.event_key.endswith(expected): raise ContractViolation("fill identity mismatch")
        object.__setattr__(self, "action", action); object.__setattr__(self, "side", side); object.__setattr__(self, "outcome", outcome); object.__setattr__(self, "path_assumption", path); object.__setattr__(self, "evidence_grade", grade); object.__setattr__(self, "granularity", granularity); object.__setattr__(self, "requested_shares", requested); object.__setattr__(self, "filled_shares", filled); object.__setattr__(self, "unfilled_shares", unfilled); object.__setattr__(self, "cash_delta", cash); object.__setattr__(self, "triggered_at", ensure_utc(self.triggered_at, "fill triggered_at") if self.triggered_at else None); object.__setattr__(self, "filled_at", ensure_utc(self.filled_at, "fill filled_at") if self.filled_at else None); object.__setattr__(self, "reason_codes", reasons); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "fill generated_at"))


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    run_id: str; intent_id: str; mode: ExecutionMode; initial_state_hash: str; event_batch_hash: str; replay_as_of: datetime; market_rule_version: str; execution_policy_version: str; trigger_evaluation_id: str; fill_ids: tuple[str, ...]; final_state_delta: ExecutionStateDelta; outcome: FillOutcome; evidence_grade: ExecutionEvidenceGrade; reason_codes: tuple[str, ...]; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        mode = _enum(ExecutionMode, self.mode, "execution mode"); outcome = _enum(FillOutcome, self.outcome, "run outcome"); grade = _enum(ExecutionEvidenceGrade, self.evidence_grade, "run evidence grade"); replay = ensure_utc(self.replay_as_of, "replay_as_of")
        if mode is not ExecutionMode.HISTORICAL_REPLAY: raise ContractViolation("execution run is only for historical replay")
        expected = stable_hash({"intent_id": self.intent_id, "mode": mode, "initial_state_hash": self.initial_state_hash, "event_batch_hash": self.event_batch_hash, "replay_as_of": replay, "market_rule_version": self.market_rule_version, "execution_policy_version": self.execution_policy_version})
        if self.run_id != expected: raise ContractViolation("execution run identity mismatch")
        object.__setattr__(self, "mode", mode); object.__setattr__(self, "outcome", outcome); object.__setattr__(self, "evidence_grade", grade); object.__setattr__(self, "replay_as_of", replay); object.__setattr__(self, "fill_ids", tuple(sorted(set(self.fill_ids)))); object.__setattr__(self, "reason_codes", _reasons(self.reason_codes)); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "run generated_at"))
