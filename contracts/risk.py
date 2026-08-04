"""V2-6 风控层的不可变合同。

风控只对既有 TradePlan 作分级和容量约束；本文件没有订单、成交或组合排序字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from .account import AccountSnapshot, as_decimal
from .enums import DecisionMode, Exchange, FreshnessStatus, Market, TradingSession
from .market_data import ContractViolation, InstrumentId, ensure_finite, ensure_utc, stable_hash
from .quality import DataQualityReport
from .scenario import TradingScenario
from .strategy import PlanAction, PositionState, QuantityIntent, StrategyBundle


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ExecutionLevel(_StringEnum): A = "A"; B = "B"; C = "C"; D = "D"
class DecisionDisposition(_StringEnum): APPROVED_NOW = "approved_now"; CONDITIONALLY_APPROVED = "conditionally_approved"; NO_ORDER_REQUIRED = "no_order_required"; OBSERVE = "observe"; REJECTED = "rejected"
class RiskProfile(_StringEnum): CONSERVATIVE = "conservative"; AGGRESSIVE = "aggressive"
class EvidenceStatus(_StringEnum): RELIABLE_POSITIVE = "reliable_positive"; POSITIVE_UNCERTAIN = "positive_uncertain"; INSUFFICIENT_SAMPLE = "insufficient_sample"; UNAVAILABLE = "unavailable"; NEGATIVE = "negative"; CONFLICTING = "conflicting"
class ValuationStatus(_StringEnum): COMPLETE = "complete"; INCOMPLETE = "incomplete"; UNAVAILABLE = "unavailable"
class MarketEligibility(_StringEnum): ELIGIBLE = "eligible"; RECHECK_REQUIRED = "recheck_required"; PARTIALLY_ELIGIBLE = "partially_eligible"; BLOCKED = "blocked"
class AvailabilitySource(_StringEnum): BROKER = "broker"; USER = "user"; INTERNAL_LEDGER = "internal_ledger"; ASSUMED_PRIOR_DAY = "assumed_prior_day"; UNAVAILABLE = "unavailable"
class RiskConstraintKind(_StringEnum): HARD = "hard"; SOFT = "soft"
class ValuationPriceKind(_StringEnum): REFERENCE_CLOSE = "reference_close"; FRESH_QUOTE = "fresh_quote"
class InstrumentClassification(_StringEnum): ORDINARY = "ordinary"; ST = "st"; GROWTH = "growth"; STAR = "star"; BSE = "bse"; UNKNOWN = "unknown"


RISK_REASON_CODES = frozenset("""
RISK_APPROVED RISK_CONDITIONALLY_APPROVED RISK_SMALL_SAMPLE RISK_FORECAST_NONINFERIOR_CAP RISK_NEGATIVE_EXPECTANCY
RISK_EVIDENCE_CONFLICT RISK_COUNTERTREND_CAP RISK_PLAN_OBSERVATION_ONLY RISK_PLAN_NOT_TRIGGERED
RISK_PLAN_EXPIRED RISK_ENTRY_STOP_MISSING RISK_ENTRY_STOP_INVALID RISK_ACCOUNT_MISSING
RISK_ACCOUNT_MARKET_MISMATCH RISK_ACCOUNT_POSITION_MISMATCH RISK_EQUITY_ZERO
RISK_VALUATION_INCOMPLETE RISK_CASH_INSUFFICIENT RISK_BUDGET_EXHAUSTED
RISK_MIN_LOT_EXCEEDS_CAPACITY RISK_SINGLE_POSITION_CAP RISK_TOTAL_STOCK_CAP
RISK_CONCENTRATION_WARNING RISK_CONCENTRATION_REDLINE RISK_ADD_BLOCKED_BY_EXISTING_RISK
RISK_DATA_BLOCKED RISK_DATA_DEGRADED RISK_QUALITY_MULTIPLIER_APPLIED RISK_EXIT_PRESERVED
RISK_PROTECTIVE_EXIT_PRIORITY RISK_POSITION_AVAILABILITY_UNKNOWN RISK_T1_BLOCKED
RISK_PARTIAL_SELLABLE RISK_PRICE_LIMIT_BLOCKED RISK_A_CLASSIFICATION_UNKNOWN
RISK_MARKET_RECHECK_REQUIRED RISK_EXTENDED_TOP_OF_BOOK_ONLY RISK_EXTENDED_VOLUME_PROXY
RISK_EXTENDED_PRICE_ONLY RISK_NO_LEVEL2_DEPTH RISK_FRICTION_RESERVE_INCLUDED
RISK_GAP_LOSS_CAN_EXCEED_PLAN RISK_PORTFOLIO_ALLOCATION_PENDING RISK_HARD_CONSTRAINT_IMMUTABLE
""".split())

_DECISION_QUANTITY = {
    PlanAction.BUY: QuantityIntent.OPEN, PlanAction.ADD: QuantityIntent.ADD,
    PlanAction.REDUCE: QuantityIntent.PARTIAL_EXIT, PlanAction.SELL: QuantityIntent.FULL_EXIT,
    PlanAction.HOLD: QuantityIntent.KEEP, PlanAction.WATCH: QuantityIntent.NONE,
}


def _enum(kind, value, name):
    try: return value if isinstance(value, kind) else kind(str(value))
    except ValueError as exc: raise ContractViolation(f"unsupported {name}: {value}") from exc


def _hash(value: str | None, name: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none: return None
    if not isinstance(value, str) or len(value) != 64 or not set(value).issubset(set("0123456789abcdef")):
        raise ContractViolation(f"{name} must be a SHA-256 digest")
    return value


def _reasons(values: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(sorted(set(values)))
    if any(value not in RISK_REASON_CODES for value in values): raise ContractViolation("unknown risk reason code")
    return values


def _decimal(value, name: str, *, nonnegative: bool = False, positive: bool = False) -> Decimal:
    result = as_decimal(value, name)
    if positive and result <= 0: raise ContractViolation(f"{name} must be positive")
    if nonnegative and result < 0: raise ContractViolation(f"{name} cannot be negative")
    return result


@dataclass(frozen=True, slots=True)
class ValuationPrice:
    instrument: InstrumentId; price: Decimal; observed_at: datetime; source: str; price_kind: ValuationPriceKind; freshness_status: FreshnessStatus
    def __post_init__(self):
        price = _decimal(self.price, "valuation price", positive=True); kind = _enum(ValuationPriceKind, self.price_kind, "price kind"); freshness = _enum(FreshnessStatus, self.freshness_status, "price freshness")
        if not self.source or (kind is ValuationPriceKind.FRESH_QUOTE and freshness is not FreshnessStatus.FRESH): raise ContractViolation("invalid valuation price evidence")
        object.__setattr__(self, "price", price); object.__setattr__(self, "price_kind", kind); object.__setattr__(self, "freshness_status", freshness); object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "valuation price observed_at"))


@dataclass(frozen=True, slots=True)
class PositionValuation:
    instrument: InstrumentId; shares: Decimal; price: Decimal; market_value: Decimal; position_pct: float | None; unrealized_pnl_amount: Decimal; unrealized_pnl_pct: float | None
    def __post_init__(self):
        shares = _decimal(self.shares, "valuation shares", positive=True); price = _decimal(self.price, "valuation price", positive=True); value = _decimal(self.market_value, "market value", positive=True); pnl = _decimal(self.unrealized_pnl_amount, "unrealized pnl")
        if value != shares * price: raise ContractViolation("position market value must equal shares times price")
        if self.position_pct is not None and not 0 <= ensure_finite(self.position_pct, "position pct") <= 1: raise ContractViolation("position pct out of range")
        if self.unrealized_pnl_pct is not None: ensure_finite(self.unrealized_pnl_pct, "unrealized pnl pct")
        object.__setattr__(self, "shares", shares); object.__setattr__(self, "price", price); object.__setattr__(self, "market_value", value); object.__setattr__(self, "unrealized_pnl_amount", pnl)


@dataclass(frozen=True, slots=True)
class FrozenAccountValuation:
    valuation_id: str; event_key: str; market: Market; currency: str; account_hash: str; price_batch_hash: str; valuation_at: datetime; status: ValuationStatus; equity: Decimal | None; cash: Decimal; invested_value: Decimal | None; invested_pct: float | None; position_values: tuple[PositionValuation, ...]; missing_price_instruments: tuple[InstrumentId, ...]; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        market = _enum(Market, self.market, "valuation market"); status = _enum(ValuationStatus, self.status, "valuation status"); cash = _decimal(self.cash, "valuation cash", nonnegative=True); account_hash = _hash(self.account_hash, "account hash"); price_hash = _hash(self.price_batch_hash, "price batch hash"); at = ensure_utc(self.valuation_at, "valuation_at")
        currency = str(self.currency).upper(); expected_currency = "CNY" if market is Market.A else "USD"
        positions = tuple(sorted(self.position_values, key=lambda item: item.instrument.stable_key)); missing = tuple(sorted(set(self.missing_price_instruments), key=lambda item: item.stable_key))
        position_keys = [item.instrument.stable_key for item in positions]
        if currency != expected_currency or len(position_keys) != len(set(position_keys)) or set(position_keys) & {item.stable_key for item in missing}: raise ContractViolation("valuation currency or instruments are inconsistent")
        if any(item.instrument.market is not market for item in positions) or any(item.market is not market for item in missing): raise ContractViolation("valuation market mismatch")
        if status is ValuationStatus.COMPLETE:
            equity = _decimal(self.equity, "valuation equity", nonnegative=True); invested = _decimal(self.invested_value, "invested value", nonnegative=True)
            position_total = sum((item.market_value for item in positions), Decimal("0"))
            expected_invested_pct = float(invested / equity) if equity else 0.0
            if (missing or invested != position_total or equity != cash + invested or self.invested_pct is None or
                    abs(ensure_finite(self.invested_pct, "invested pct") - expected_invested_pct) > 1e-12 or
                    any(item.position_pct is None or abs(item.position_pct - (float(item.market_value / equity) if equity else 0.0)) > 1e-12 for item in positions)):
                raise ContractViolation("complete valuation totals are inconsistent")
        elif status is ValuationStatus.INCOMPLETE:
            if not missing or any(value is not None for value in (self.equity, self.invested_value, self.invested_pct)):
                raise ContractViolation("incomplete valuation cannot fill missing prices")
        elif positions or missing or any(value is not None for value in (self.equity, self.invested_value, self.invested_pct)):
            raise ContractViolation("unavailable valuation cannot claim positions or totals")
        identity = {"market": market, "currency": currency, "account_hash": account_hash, "price_batch_hash": price_hash, "valuation_at": at}
        expected = stable_hash(identity)
        if self.valuation_id != expected or not self.event_key.endswith(expected): raise ContractViolation("valuation identity mismatch")
        object.__setattr__(self, "market", market); object.__setattr__(self, "currency", currency); object.__setattr__(self, "cash", cash); object.__setattr__(self, "valuation_at", at); object.__setattr__(self, "status", status); object.__setattr__(self, "position_values", positions); object.__setattr__(self, "missing_price_instruments", missing); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "valuation generated_at"))


@dataclass(frozen=True, slots=True)
class PositionAvailability:
    instrument: InstrumentId; total_shares: Decimal; sellable_shares: Decimal | None; as_of: datetime; source: AvailabilitySource; reason_codes: tuple[str, ...]
    def __post_init__(self):
        total = _decimal(self.total_shares, "total shares", positive=True); sellable = None if self.sellable_shares is None else _decimal(self.sellable_shares, "sellable shares", nonnegative=True); source = _enum(AvailabilitySource, self.source, "availability source")
        if sellable is not None and sellable > total: raise ContractViolation("sellable shares exceed total")
        if source is AvailabilitySource.UNAVAILABLE and sellable is not None: raise ContractViolation("unavailable source cannot provide sellable shares")
        object.__setattr__(self, "total_shares", total); object.__setattr__(self, "sellable_shares", sellable); object.__setattr__(self, "source", source); object.__setattr__(self, "as_of", ensure_utc(self.as_of, "availability as_of")); object.__setattr__(self, "reason_codes", _reasons(self.reason_codes))


@dataclass(frozen=True, slots=True)
class PlanEvidenceSnapshot:
    evidence_id: str; instrument: InstrumentId; strategy_id: str; strategy_version: str; parameter_hash: str; profile: RiskProfile | None; sample_count: int; oof_sample_count: int; expected_net_return: float | None; confidence_low: float | None; confidence_high: float | None; win_rate: float | None; max_adverse_excursion: float | None; status: EvidenceStatus; source_ledger_version: str; data_cutoff_at: datetime; evaluated_at: datetime; generated_at: datetime
    action: str | None = None
    def __post_init__(self):
        status = _enum(EvidenceStatus, self.status, "evidence status"); profile = None if self.profile is None else _enum(RiskProfile, self.profile, "evidence profile")
        _hash(self.parameter_hash, "evidence parameter hash")
        if self.sample_count < 0 or self.oof_sample_count < 0 or self.oof_sample_count > self.sample_count or not self.strategy_id or not self.strategy_version or not self.source_ledger_version: raise ContractViolation("invalid evidence samples")
        if self.action is not None and self.action not in {"buy", "add", "sell", "reduce", "hold", "watch"}: raise ContractViolation("invalid evidence action")
        values = (self.expected_net_return, self.confidence_low, self.confidence_high, self.win_rate, self.max_adverse_excursion)
        if status is EvidenceStatus.CONFLICTING:
            pass
        elif status is EvidenceStatus.UNAVAILABLE:
            if self.oof_sample_count == 0 and any(item is not None for item in values): raise ContractViolation("unavailable evidence without OOF samples cannot claim metrics")
            if 1 <= self.oof_sample_count < 10 and any(item is None for item in values): raise ContractViolation("observed but unavailable evidence needs complete metrics")
            if self.oof_sample_count >= 10: raise ContractViolation("ten or more OOF samples require an evaluated status")
        elif any(item is None for item in values): raise ContractViolation("available evidence needs complete metrics")
        for name, value in zip(("expected return", "confidence low", "confidence high", "win rate", "max adverse excursion"), values):
            if value is not None: ensure_finite(value, name)
        if self.win_rate is not None and not 0 <= ensure_finite(self.win_rate, "win rate") <= 1: raise ContractViolation("win rate out of range")
        if self.confidence_low is not None and self.confidence_high is not None and self.confidence_low > self.confidence_high: raise ContractViolation("evidence interval inverted")
        data_at, evaluated, generated = ensure_utc(self.data_cutoff_at, "evidence cutoff"), ensure_utc(self.evaluated_at, "evidence evaluated"), ensure_utc(self.generated_at, "evidence generated")
        if data_at > evaluated or evaluated > generated: raise ContractViolation("evidence timestamps are inverted")
        computed = self._status()
        if computed is not status: raise ContractViolation("evidence status does not match metrics")
        identity = {"instrument": self.instrument, "strategy_id": self.strategy_id, "strategy_version": self.strategy_version, "parameter_hash": self.parameter_hash, "profile": profile, "sample_count": self.sample_count, "oof_sample_count": self.oof_sample_count, "metrics": values, "status": status, "source_ledger_version": self.source_ledger_version, "data_cutoff_at": data_at, "evaluated_at": evaluated}
        # Legacy snapshots predate action-specific evidence and keep their old identity.
        if self.action is not None: identity["action"] = self.action
        expected = stable_hash(identity)
        if self.evidence_id != expected: raise ContractViolation("evidence identity mismatch")
        object.__setattr__(self, "status", status); object.__setattr__(self, "profile", profile); object.__setattr__(self, "data_cutoff_at", data_at); object.__setattr__(self, "evaluated_at", evaluated); object.__setattr__(self, "generated_at", generated)
    def _status(self):
        # 账本身份冲突由 V2-9 的调用方显式标注；风控层必须优先拒绝它，
        # 不能用一组貌似正常的汇总指标把冲突降级为普通样本不足。
        if self.status is EvidenceStatus.CONFLICTING: return EvidenceStatus.CONFLICTING
        if self.expected_net_return is None: return EvidenceStatus.UNAVAILABLE
        if 1 <= self.oof_sample_count < 10: return EvidenceStatus.UNAVAILABLE
        if self.confidence_high < 0 or (self.oof_sample_count >= 10 and self.expected_net_return < 0) or (self.oof_sample_count >= 30 and self.expected_net_return <= 0): return EvidenceStatus.NEGATIVE
        if self.oof_sample_count >= 30 and self.expected_net_return > 0 and self.confidence_low >= 0: return EvidenceStatus.RELIABLE_POSITIVE
        if 10 <= self.oof_sample_count <= 29: return EvidenceStatus.INSUFFICIENT_SAMPLE
        if self.oof_sample_count >= 30 and self.expected_net_return > 0: return EvidenceStatus.POSITIVE_UNCERTAIN
        return EvidenceStatus.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class MarketRuleSet:
    rule_version: str; market: Market; exchange: Exchange; lot_size: Decimal; same_day_sell_restricted: bool; commission_rate: Decimal; minimum_commission: Decimal; sell_tax_rate: Decimal; base_slippage_reserve: Decimal; price_limit_pct: float | None; instrument_classification: InstrumentClassification; source: str; effective_from: datetime; effective_to: datetime | None
    def __post_init__(self):
        market = _enum(Market, self.market, "rule market"); exchange = _enum(Exchange, self.exchange, "rule exchange"); classification = _enum(InstrumentClassification, self.instrument_classification, "classification")
        lot = _decimal(self.lot_size, "lot size", positive=True); fields = ("commission_rate", "minimum_commission", "sell_tax_rate", "base_slippage_reserve")
        for name in fields:
            value = _decimal(getattr(self, name), name, nonnegative=True)
            object.__setattr__(self, name, value)
        a_exchanges = {Exchange.XSHG, Exchange.XSHE, Exchange.XBSE}
        if ((market is Market.A) != (exchange in a_exchanges) or not self.rule_version or not self.source or
                lot != lot.to_integral_value() or any(getattr(self, name) > 1 for name in ("commission_rate", "sell_tax_rate", "base_slippage_reserve")) or
                (self.price_limit_pct is not None and not 0 < ensure_finite(self.price_limit_pct, "price limit") < 1)):
            raise ContractViolation("invalid market rule")
        start = ensure_utc(self.effective_from, "rule effective_from"); end = ensure_utc(self.effective_to, "rule effective_to") if self.effective_to else None
        if end and start >= end: raise ContractViolation("rule effective window is inverted")
        object.__setattr__(self, "market", market); object.__setattr__(self, "exchange", exchange); object.__setattr__(self, "instrument_classification", classification); object.__setattr__(self, "lot_size", lot); object.__setattr__(self, "effective_from", start); object.__setattr__(self, "effective_to", end)


@dataclass(frozen=True, slots=True)
class MarketState:
    instrument: InstrumentId; mode: DecisionMode; session: TradingSession; current_price: Decimal | None; previous_close: Decimal | None; bid: Decimal | None; ask: Decimal | None; volume: Decimal | None; observed_at: datetime; source: str; freshness_status: FreshnessStatus
    def __post_init__(self):
        mode = _enum(DecisionMode, self.mode, "market mode"); session = _enum(TradingSession, self.session, "market session"); freshness = _enum(FreshnessStatus, self.freshness_status, "market freshness")
        for name in ("current_price", "previous_close", "bid", "ask"):
            value = getattr(self, name)
            if value is not None: object.__setattr__(self, name, _decimal(value, name, positive=True))
        if self.volume is not None: object.__setattr__(self, "volume", _decimal(self.volume, "volume", nonnegative=True))
        if self.bid is not None and self.ask is not None and self.bid > self.ask: raise ContractViolation("bid cannot exceed ask")
        valid_sessions = {DecisionMode.PRE: {TradingSession.PRE}, DecisionMode.INTRADAY: {TradingSession.REGULAR}, DecisionMode.EOD: {TradingSession.POST, TradingSession.CLOSED}}
        if session not in valid_sessions[mode]: raise ContractViolation("market state mode and session are inconsistent")
        if not self.source: raise ContractViolation("market state source cannot be empty")
        object.__setattr__(self, "mode", mode); object.__setattr__(self, "session", session); object.__setattr__(self, "freshness_status", freshness); object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "market observed_at"))


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    policy_version: str = "risk_policy_v1"; conservative_risk_pct: Decimal = Decimal("0.01"); aggressive_risk_pct: Decimal = Decimal("0.02"); conservative_target_cap: Decimal = Decimal("0.20"); aggressive_target_cap: Decimal = Decimal("0.25"); single_position_hard_cap: Decimal = Decimal("0.25"); total_stock_hard_cap: Decimal = Decimal("0.90"); concentration_warning: Decimal = Decimal("0.20"); concentration_redline: Decimal = Decimal("0.30"); b_level_multiplier: Decimal = Decimal("0.50"); countertrend_multiplier: Decimal = Decimal("0.50"); conservative_reduce_fraction: Decimal = Decimal("0.50"); aggressive_reduce_fraction: Decimal = Decimal("0.25"); hard_constraint_version: str = "risk_hard_constraints_v1"; parameter_hash: str = ""
    def __post_init__(self):
        if not self.policy_version or not self.hard_constraint_version: raise ContractViolation("risk policy versions cannot be empty")
        for name in ("conservative_risk_pct", "aggressive_risk_pct", "conservative_target_cap", "aggressive_target_cap", "single_position_hard_cap", "total_stock_hard_cap", "concentration_warning", "concentration_redline", "b_level_multiplier", "countertrend_multiplier", "conservative_reduce_fraction", "aggressive_reduce_fraction"):
            value = _decimal(getattr(self, name), name, positive=True)
            if value > 1: raise ContractViolation("risk policy multiplier cannot exceed one")
            object.__setattr__(self, name, value)
        if (self.hard_constraint_version != "risk_hard_constraints_v1" or self.conservative_risk_pct > self.aggressive_risk_pct or
                self.aggressive_risk_pct > Decimal("0.02") or self.conservative_target_cap > self.aggressive_target_cap or
                self.aggressive_target_cap > self.single_position_hard_cap or self.single_position_hard_cap != Decimal("0.25") or
                self.total_stock_hard_cap != Decimal("0.90") or self.concentration_warning >= self.concentration_redline or
                self.conservative_reduce_fraction < self.aggressive_reduce_fraction):
            raise ContractViolation("risk hard constraints or profile ordering are invalid")
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "parameter_hash"}
        expected = stable_hash(payload)
        if self.parameter_hash and self.parameter_hash != expected: raise ContractViolation("risk policy hash mismatch")
        object.__setattr__(self, "parameter_hash", expected)


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    code: str; kind: RiskConstraintKind; passed: bool; limit: Decimal | None = None; observed: Decimal | None = None
    def __post_init__(self):
        if self.code not in RISK_REASON_CODES: raise ContractViolation("constraint needs a registered reason code")
        object.__setattr__(self, "kind", _enum(RiskConstraintKind, self.kind, "constraint kind"))
        if self.limit is not None: object.__setattr__(self, "limit", _decimal(self.limit, "constraint limit", nonnegative=True))
        if self.observed is not None: object.__setattr__(self, "observed", _decimal(self.observed, "constraint observed", nonnegative=True))


@dataclass(frozen=True, slots=True)
class RiskAdjustment:
    code: str; multiplier: Decimal
    def __post_init__(self):
        if self.code not in RISK_REASON_CODES: raise ContractViolation("adjustment needs a registered reason code")
        multiplier = _decimal(self.multiplier, "risk multiplier", positive=True)
        if multiplier > 1: raise ContractViolation("risk adjustment cannot increase capacity")
        object.__setattr__(self, "multiplier", multiplier)


@dataclass(frozen=True, slots=True)
class RiskRequest:
    instrument: InstrumentId; strategy_bundle: StrategyBundle; trading_scenario: TradingScenario; data_quality: DataQualityReport; account_snapshot: AccountSnapshot | None; valuation: FrozenAccountValuation | None; position_availability: PositionAvailability | None; evidence: tuple[PlanEvidenceSnapshot, ...]; market_rules: MarketRuleSet; market_state: MarketState | None; policy: RiskPolicy; as_of: datetime; schema_version: int = 1
    def __post_init__(self):
        at = ensure_utc(self.as_of, "risk as_of")
        if (self.schema_version != 1 or self.strategy_bundle.instrument != self.instrument or self.trading_scenario.instrument != self.instrument or
                self.strategy_bundle.scenario_id != self.trading_scenario.scenario_id or self.market_rules.market is not self.instrument.market or
                self.market_rules.exchange is not self.instrument.exchange):
            raise ContractViolation("risk request identity mismatch")
        if stable_hash(self.data_quality) != self.trading_scenario.quality_hash: raise ContractViolation("risk request quality hash does not match scenario")
        if self.data_quality.evaluated_at > at: raise ContractViolation("risk quality is from the future")
        if self.account_snapshot and (self.account_snapshot.market is not self.instrument.market or self.account_snapshot.captured_at > at): raise ContractViolation("risk account mismatch")
        target_position = None if self.account_snapshot is None else next((item for item in self.account_snapshot.positions if item.instrument == self.instrument), None)
        expected_position_hash = stable_hash(target_position) if target_position else stable_hash("flat")
        plan_hashes = {plan.position_hash for branch in (self.strategy_bundle.entry_or_add, self.strategy_bundle.reduce_or_exit, self.strategy_bundle.hold, self.strategy_bundle.invalidation) for plan in branch.plans}
        if len(plan_hashes) != 1 or plan_hashes != {expected_position_hash}:
            raise ContractViolation("risk account position does not match strategy bundle")
        if self.valuation:
            expected_currency = self.account_snapshot.currency if self.account_snapshot else None
            account_positions = {item.instrument: item.shares for item in self.account_snapshot.positions} if self.account_snapshot else {}
            valued_positions = {item.instrument: item.shares for item in self.valuation.position_values}
            represented = set(valued_positions) | set(self.valuation.missing_price_instruments)
            if (self.valuation.market is not self.instrument.market or self.valuation.currency != expected_currency or self.valuation.valuation_at > at or
                    self.account_snapshot is None or self.valuation.account_hash != stable_hash(self.account_snapshot) or
                    represented != set(account_positions) or any(valued_positions[key] != account_positions[key] for key in valued_positions)):
                raise ContractViolation("risk valuation mismatch")
        if self.position_availability and (self.position_availability.instrument != self.instrument or self.position_availability.as_of > at or target_position is None or self.position_availability.total_shares != target_position.shares): raise ContractViolation("risk availability mismatch")
        if self.market_state and (self.market_state.instrument != self.instrument or self.market_state.mode is not self.trading_scenario.mode or self.market_state.observed_at > at): raise ContractViolation("risk market state mismatch")
        if not (self.market_rules.effective_from <= at and (self.market_rules.effective_to is None or at < self.market_rules.effective_to)): raise ContractViolation("market rules are not effective at risk time")
        if any(item.data_cutoff_at > at or item.evaluated_at > at or item.generated_at > at for item in self.evidence): raise ContractViolation("risk evidence is from the future")
        object.__setattr__(self, "as_of", at); object.__setattr__(self, "evidence", tuple(sorted(self.evidence, key=lambda item: item.evidence_id)))


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    decision_id: str; event_key: str; instrument: InstrumentId; scenario_id: str; bundle_id: str; plan_id: str; profile: RiskProfile; action: PlanAction; quantity_intent: QuantityIntent; level: ExecutionLevel; disposition: DecisionDisposition; executable_now: bool; recheck_at_trigger: bool; approved_shares: Decimal; blocked_shares: Decimal; entry_price: Decimal | None; stop_price: Decimal | None; current_position_value: Decimal | None; current_position_pct: float | None; planned_position_value: Decimal | None; post_trade_position_pct: float | None; risk_budget_amount: Decimal | None; incremental_planned_loss: Decimal | None; total_position_planned_loss: Decimal | None; max_loss_amount: Decimal | None; friction_reserve: Decimal | None; market_eligibility: MarketEligibility; evidence_status: EvidenceStatus; hard_constraints: tuple[ConstraintResult, ...]; soft_adjustments: tuple[RiskAdjustment, ...]; reason_codes: tuple[str, ...]; valid_from: datetime | None; expires_at: datetime | None; account_hash: str | None; valuation_id: str | None; quality_hash: str; evidence_hash: str; market_rule_version: str; risk_policy_version: str; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        profile = _enum(RiskProfile, self.profile, "risk profile"); level = _enum(ExecutionLevel, self.level, "execution level"); disposition = _enum(DecisionDisposition, self.disposition, "decision disposition"); action = _enum(PlanAction, self.action, "decision action"); quantity = _enum(QuantityIntent, self.quantity_intent, "decision quantity"); eligibility = _enum(MarketEligibility, self.market_eligibility, "market eligibility"); evidence = _enum(EvidenceStatus, self.evidence_status, "evidence status")
        approved, blocked = _decimal(self.approved_shares, "approved shares", nonnegative=True), _decimal(self.blocked_shares, "blocked shares", nonnegative=True)
        if self.schema_version != 1 or quantity is not _DECISION_QUANTITY[action]: raise ContractViolation("invalid decision schema or action quantity")
        for name in ("entry_price", "stop_price", "current_position_value", "planned_position_value", "risk_budget_amount", "incremental_planned_loss", "total_position_planned_loss", "max_loss_amount", "friction_reserve"):
            value = getattr(self, name)
            if value is not None: object.__setattr__(self, name, _decimal(value, name, nonnegative=True))
        current_pct = None if self.current_position_pct is None else ensure_finite(self.current_position_pct, "current position pct")
        post_pct = None if self.post_trade_position_pct is None else ensure_finite(self.post_trade_position_pct, "post trade position pct")
        if any(value is not None and not 0 <= value <= 1 for value in (current_pct, post_pct)): raise ContractViolation("decision position pct out of range")
        expected_disposition = {ExecutionLevel.C: DecisionDisposition.OBSERVE, ExecutionLevel.D: DecisionDisposition.REJECTED}.get(level)
        if level in {ExecutionLevel.C, ExecutionLevel.D} and (approved != 0 or disposition is not expected_disposition): raise ContractViolation("C/D decision cannot approve shares")
        if disposition is DecisionDisposition.APPROVED_NOW and (not self.executable_now or approved <= 0 or self.recheck_at_trigger or level not in {ExecutionLevel.A, ExecutionLevel.B}): raise ContractViolation("approved-now decision is inconsistent")
        if disposition is DecisionDisposition.CONDITIONALLY_APPROVED and (self.executable_now or not self.recheck_at_trigger or level not in {ExecutionLevel.A, ExecutionLevel.B}): raise ContractViolation("conditional decision is inconsistent")
        if disposition in {DecisionDisposition.NO_ORDER_REQUIRED, DecisionDisposition.OBSERVE, DecisionDisposition.REJECTED} and (self.executable_now or approved != 0): raise ContractViolation("non-order decision cannot execute shares")
        valid_from = ensure_utc(self.valid_from, "decision valid_from") if self.valid_from else None; expires_at = ensure_utc(self.expires_at, "decision expires_at") if self.expires_at else None
        if (valid_from is None) != (expires_at is None) or (valid_from and expires_at and valid_from >= expires_at): raise ContractViolation("decision validity window is invalid")
        account_hash = _hash(self.account_hash, "decision account hash", allow_none=True); valuation_id = _hash(self.valuation_id, "decision valuation id", allow_none=True)
        quality_hash = _hash(self.quality_hash, "decision quality hash"); evidence_hash = _hash(self.evidence_hash, "decision evidence hash")
        hard = tuple(sorted(self.hard_constraints, key=lambda item: (item.kind.value, item.code))); soft = tuple(sorted(self.soft_adjustments, key=lambda item: item.code)); reasons = _reasons(self.reason_codes)
        identity = {"plan_id": self.plan_id, "bundle_id": self.bundle_id, "profile": profile, "level": level, "disposition": disposition, "approved_shares": approved, "blocked_shares": blocked, "entry": self.entry_price, "stop": self.stop_price, "position": (self.current_position_value, current_pct, self.planned_position_value, post_pct), "risk": (self.risk_budget_amount, self.incremental_planned_loss, self.total_position_planned_loss, self.max_loss_amount, self.friction_reserve), "eligibility": eligibility, "evidence_status": evidence, "account_hash": account_hash, "valuation_id": valuation_id, "quality_hash": quality_hash, "evidence_hash": evidence_hash, "market_rule_version": self.market_rule_version, "risk_policy_version": self.risk_policy_version, "hard": hard, "soft": soft, "reasons": reasons, "valid_from": valid_from, "expires_at": expires_at}
        expected = stable_hash(identity)
        if self.decision_id != expected or not self.event_key.endswith(expected): raise ContractViolation("decision identity mismatch")
        object.__setattr__(self, "profile", profile); object.__setattr__(self, "level", level); object.__setattr__(self, "disposition", disposition); object.__setattr__(self, "action", action); object.__setattr__(self, "quantity_intent", quantity); object.__setattr__(self, "market_eligibility", eligibility); object.__setattr__(self, "evidence_status", evidence); object.__setattr__(self, "approved_shares", approved); object.__setattr__(self, "blocked_shares", blocked); object.__setattr__(self, "current_position_pct", current_pct); object.__setattr__(self, "post_trade_position_pct", post_pct); object.__setattr__(self, "account_hash", account_hash); object.__setattr__(self, "valuation_id", valuation_id); object.__setattr__(self, "quality_hash", quality_hash); object.__setattr__(self, "evidence_hash", evidence_hash); object.__setattr__(self, "hard_constraints", hard); object.__setattr__(self, "soft_adjustments", soft); object.__setattr__(self, "reason_codes", reasons); object.__setattr__(self, "valid_from", valid_from); object.__setattr__(self, "expires_at", expires_at); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "decision generated_at"))


@dataclass(frozen=True, slots=True)
class RiskDecisionBundle:
    risk_bundle_id: str; event_key: str; instrument: InstrumentId; scenario_id: str; strategy_bundle_id: str; position_state: PositionState; decisions: tuple[ExecutionDecision, ...]; conservative_decision_ids: tuple[str, ...]; aggressive_decision_ids: tuple[str, ...]; protective_decision_ids: tuple[str, ...]; account_hash: str | None; valuation_id: str | None; quality_hash: str; market_rule_version: str; risk_policy_version: str; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        state = _enum(PositionState, self.position_state, "risk bundle position state"); decisions = tuple(sorted(self.decisions, key=lambda item: item.decision_id))
        if self.schema_version != 1 or not decisions or any(item.instrument != self.instrument or item.scenario_id != self.scenario_id or item.bundle_id != self.strategy_bundle_id for item in decisions): raise ContractViolation("risk bundle contains foreign decisions")
        if len({(item.plan_id, item.profile) for item in decisions}) != len(decisions): raise ContractViolation("risk bundle duplicates plan/profile decisions")
        for item in decisions:
            if (item.account_hash != self.account_hash or item.valuation_id != self.valuation_id or item.quality_hash != self.quality_hash or item.market_rule_version != self.market_rule_version or item.risk_policy_version != self.risk_policy_version): raise ContractViolation("risk bundle decision metadata mismatch")
        expected_conservative = tuple(sorted(item.decision_id for item in decisions if item.profile is RiskProfile.CONSERVATIVE)); expected_aggressive = tuple(sorted(item.decision_id for item in decisions if item.profile is RiskProfile.AGGRESSIVE))
        if tuple(sorted(set(self.conservative_decision_ids))) != expected_conservative or tuple(sorted(set(self.aggressive_decision_ids))) != expected_aggressive: raise ContractViolation("risk bundle profile ids mismatch")
        protective = tuple(sorted(set(self.protective_decision_ids)))
        if not set(protective).issubset({item.decision_id for item in decisions}): raise ContractViolation("protective decision ids must reference bundle decisions")
        identity = {"scenario_id": self.scenario_id, "strategy_bundle_id": self.strategy_bundle_id, "decision_ids": tuple(item.decision_id for item in decisions), "account_hash": self.account_hash, "valuation_id": self.valuation_id, "quality_hash": self.quality_hash, "market_rule_version": self.market_rule_version, "risk_policy_version": self.risk_policy_version}
        expected = stable_hash(identity)
        if self.risk_bundle_id != expected or not self.event_key.endswith(expected): raise ContractViolation("risk bundle identity mismatch")
        object.__setattr__(self, "position_state", state); object.__setattr__(self, "decisions", decisions); object.__setattr__(self, "conservative_decision_ids", expected_conservative); object.__setattr__(self, "aggressive_decision_ids", expected_aggressive); object.__setattr__(self, "protective_decision_ids", protective); object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "risk bundle generated_at"))
