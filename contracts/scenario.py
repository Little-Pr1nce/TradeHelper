"""V2-4 immutable contracts for translating forecasts into scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from .analysis import FeatureSnapshot
from .enums import DecisionMode, Exchange, Market
from .forecast import DirectionProbabilities, ForecastResult, ReturnDistribution
from .market_data import (
    ContractViolation,
    InstrumentId,
    QuoteSnapshot,
    ensure_finite,
    ensure_utc,
    stable_hash,
)
from .quality import DataQualityReport


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ScenarioBias(_StringEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGE = "range"
    UNCERTAIN = "uncertain"


class HorizonSignal(_StringEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGE = "range"
    WEAK = "weak"
    UNAVAILABLE = "unavailable"


class BandSignal(_StringEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGE = "range"
    UNCERTAIN = "uncertain"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class HorizonAlignment(_StringEnum):
    ALIGNED = "aligned"
    MIXED = "mixed"
    PARTIAL = "partial"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class ScenarioState(_StringEnum):
    BULLISH_CONTINUATION = "bullish_continuation"
    BULLISH_PULLBACK = "bullish_pullback"
    BEARISH_CONTINUATION = "bearish_continuation"
    BEARISH_REBOUND = "bearish_rebound"
    RANGE_BOUND = "range_bound"
    MIXED = "mixed"
    FORECAST_CONFLICT = "forecast_conflict"
    UNCERTAIN = "uncertain"


class ForecastEvidenceGrade(_StringEnum):
    STOCK_CONFIRMED = "stock_confirmed"
    STOCK_OBSERVATION = "stock_observation"
    CROSS_STOCK_OBSERVATION = "cross_stock_observation"
    BASELINE_OBSERVATION = "baseline_observation"
    UNAVAILABLE = "unavailable"


class ForecastSupportLevel(_StringEnum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    OBSERVATIONAL = "observational"
    UNAVAILABLE = "unavailable"


class ScenarioStatus(_StringEnum):
    READY = "ready"
    DEGRADED = "degraded"
    OBSERVATION_ONLY = "observation_only"
    BLOCKED = "blocked"


class PriceLocation(_StringEnum):
    BELOW_P10 = "below_p10"
    INSIDE_INTERVAL = "inside_interval"
    ABOVE_P90 = "above_p90"
    UNAVAILABLE = "unavailable"


class CurrentPriceState(_StringEnum):
    REFERENCE_CLOSE = "reference_close"
    FRESH_QUOTE = "fresh_quote"
    EXPECTED_MISSING = "expected_missing"
    STALE_OR_MISSING = "stale_or_missing"


class NewsDeltaState(_StringEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"


class EntryPosture(_StringEnum):
    FOLLOW_TREND = "follow_trend"
    WAIT_PULLBACK = "wait_pullback"
    WAIT_CONFIRMATION = "wait_confirmation"
    RANGE_EXTREMES_ONLY = "range_extremes_only"
    COUNTERTREND_CONFIRMATION = "countertrend_confirmation"
    OBSERVATION_ONLY = "observation_only"
    BLOCKED = "blocked"


class ExitPosture(_StringEnum):
    STANDARD = "standard"
    TIGHTEN_PROTECTION = "tighten_protection"
    PRIORITIZE_PROTECTION = "prioritize_protection"


class StrategyFamily(_StringEnum):
    TREND_CONTINUATION = "trend_continuation"
    BREAKOUT_CONFIRMATION = "breakout_confirmation"
    PULLBACK_ENTRY = "pullback_entry"
    SUPPORT_REBOUND = "support_rebound"
    RANGE_MEAN_REVERSION = "range_mean_reversion"
    PROTECTIVE_EXIT = "protective_exit"
    PROFIT_LOCK = "profit_lock"
    FAILED_REBOUND_EXIT = "failed_rebound_exit"
    OBSERVATION = "observation"


class ScenarioFactKind(_StringEnum):
    NEWS = "news"
    FUNDAMENTAL = "fundamental"


class VolatilityShock(_StringEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NONE = "none"
    UNAVAILABLE = "unavailable"


SCENARIO_REASON_CODES = frozenset(
    """FORECAST_STOCK_CONFIRMED FORECAST_STOCK_NONINFERIOR FORECAST_STOCK_OBSERVATION
    FORECAST_CROSS_STOCK_OBSERVATION FORECAST_BASELINE_OBSERVATION
    FORECAST_INSUFFICIENT_SAMPLE FORECAST_DATA_BLOCKED
    FORECAST_CALENDAR_UNAVAILABLE FORECAST_NO_ELIGIBLE_MODEL
    FORECAST_MARGIN_WEAK PROBABILITY_DISTRIBUTION_NOT_ALIGNED
    TACTICAL_SWING_ALIGNED TACTICAL_SWING_MIXED HORIZON_COVERAGE_PARTIAL
    HORIZON_CONFLICT CURRENT_PRICE_REFERENCE_CLOSE CURRENT_PRICE_FRESH_QUOTE
    CURRENT_PRICE_EXPECTED_MISSING CURRENT_PRICE_STALE_OR_MISSING
    PRICE_ABOVE_P90 PRICE_BELOW_P10 BULLISH_VOLATILITY_SHOCK
    BEARISH_VOLATILITY_SHOCK NEWS_DELTA_POSITIVE NEWS_DELTA_NEGATIVE
    NEWS_DELTA_UNAVAILABLE NEWS_UPDATE_PRESENT FUNDAMENTAL_UPDATE_PRESENT
    UNMODELED_FACT_UPDATE DATA_QUALITY_DEGRADED DATA_QUALITY_BLOCKED
    ENTRY_WAIT_PULLBACK ENTRY_WAIT_CONFIRMATION ENTRY_OBSERVATION_ONLY
    ENTRY_BLOCKED PROTECTIVE_EXIT_PRESERVED""".split()
)

REGISTERED_NEWS_FEATURES = frozenset(
    {
        "news.count_1d",
        "news.count_7d",
        "news.count_30d",
        "news.source_count_30d",
        "news.sentiment_weighted_1d",
        "news.sentiment_weighted_7d",
        "news.sentiment_change",
        "news.latest_age_hours",
        "news.scored_ratio_30d",
    }
)
REGISTERED_FUNDAMENTAL_FEATURES = frozenset(
    {
        "fund.pe_ttm",
        "fund.pb_mrq",
        "fund.ps_ttm",
        "fund.roe",
        "fund.gross_margin",
        "fund.revenue_growth_yoy",
        "fund.net_profit_growth_yoy",
        "fund.debt_ratio",
    }
)
PROTECTIVE_STRATEGY_FAMILIES = frozenset(
    {
        StrategyFamily.PROTECTIVE_EXIT,
        StrategyFamily.PROFIT_LOCK,
        StrategyFamily.FAILED_REBOUND_EXIT,
        StrategyFamily.OBSERVATION,
    }
)


def _enum(kind, value, field):
    try:
        return value if isinstance(value, kind) else kind(str(value))
    except ValueError as exc:
        raise ContractViolation(f"unsupported {field}: {value}") from exc


def _sha256(value: str, field: str) -> str:
    hex_digits = frozenset("0123456789abcdef")
    if len(value) != 64 or not set(value).issubset(hex_digits):
        raise ContractViolation(f"{field} must be a SHA-256 hex digest")
    return value


def _reason_codes(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    ordered = tuple(sorted(set(values)))
    if ordered != values or any(value not in SCENARIO_REASON_CODES for value in ordered):
        raise ContractViolation(f"{field} must contain sorted registered reason codes")
    return ordered


@dataclass(frozen=True, slots=True)
class DecisionSession:
    market: Market
    exchange: Exchange
    session_date: date
    regular_open: datetime
    regular_close: datetime
    breaks: tuple[tuple[datetime, datetime], ...]
    source: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        market = _enum(Market, self.market, "session market")
        exchange = _enum(Exchange, self.exchange, "session exchange")
        opened = ensure_utc(self.regular_open, "regular_open")
        closed = ensure_utc(self.regular_close, "regular_close")
        intervals = tuple(
            (ensure_utc(left, "break open"), ensure_utc(right, "break close"))
            for left, right in self.breaks
        )
        if opened >= closed or not str(self.source or "").strip() or self.schema_version < 1:
            raise ContractViolation("invalid decision session")
        if (
            any(left >= right or left <= opened or right >= closed for left, right in intervals)
            or tuple(sorted(intervals)) != intervals
            or any(intervals[index][1] > intervals[index + 1][0] for index in range(len(intervals) - 1))
        ):
            raise ContractViolation("invalid session breaks")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "regular_open", opened)
        object.__setattr__(self, "regular_close", closed)
        object.__setattr__(self, "breaks", intervals)


@dataclass(frozen=True, slots=True)
class ScenarioFactUpdate:
    kind: ScenarioFactKind
    stable_key: str
    available_at: datetime
    source: str
    payload_hash: str
    affected_features: tuple[str, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        kind = _enum(ScenarioFactKind, self.kind, "fact kind")
        features = tuple(self.affected_features)
        registered = REGISTERED_NEWS_FEATURES if kind is ScenarioFactKind.NEWS else REGISTERED_FUNDAMENTAL_FEATURES
        if (
            not str(self.stable_key or "").strip()
            or not str(self.source or "").strip()
            or not features
            or features != tuple(sorted(set(features)))
            or any(name not in registered for name in features)
            or self.schema_version < 1
        ):
            raise ContractViolation("invalid scenario fact update")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "available_at", ensure_utc(self.available_at, "fact available_at"))
        object.__setattr__(self, "payload_hash", _sha256(self.payload_hash, "fact payload_hash"))


@dataclass(frozen=True, slots=True)
class ScenarioRequest:
    instrument: InstrumentId
    mode: DecisionMode
    as_of: datetime
    origin_snapshot: FeatureSnapshot
    current_snapshot: FeatureSnapshot
    current_quote: QuoteSnapshot | None
    fact_updates: tuple[ScenarioFactUpdate, ...]
    forecasts: tuple[ForecastResult, ...]
    data_quality: DataQualityReport
    decision_session: DecisionSession | None
    policy_version: str = "scenario_policy_v1"
    schema_version: int = 1

    def __post_init__(self) -> None:
        mode = _enum(DecisionMode, self.mode, "scenario mode")
        as_of = ensure_utc(self.as_of, "scenario as_of")
        if not str(self.policy_version or "").strip() or self.schema_version < 1:
            raise ContractViolation("scenario policy and schema versions are required")
        if (
            self.origin_snapshot.instrument != self.instrument
            or self.current_snapshot.instrument != self.instrument
            or self.origin_snapshot.mode is not DecisionMode.EOD
            or self.current_snapshot.mode is not mode
            or self.origin_snapshot.feature_set_version != self.current_snapshot.feature_set_version
        ):
            raise ContractViolation("scenario snapshots do not match request")
        if self.current_snapshot.cutoff_at > as_of or self.current_snapshot.cutoff_at < self.origin_snapshot.cutoff_at:
            raise ContractViolation("invalid current snapshot cutoff")
        if self.data_quality.evaluated_at > as_of:
            raise ContractViolation("scenario data quality cannot be evaluated in the future")
        if mode is DecisionMode.EOD and (
            self.current_snapshot.feature_hash != self.origin_snapshot.feature_hash
            or self.current_snapshot.quote_observed_at is not None
            or self.current_quote is not None
        ):
            raise ContractViolation("eod scenario must use the new origin close without quote overlay")
        if mode in {DecisionMode.PRE, DecisionMode.INTRADAY}:
            def closed(snapshot: FeatureSnapshot) -> tuple:
                return tuple(
                    (
                        item.name,
                        item.value,
                        item.status.value,
                        item.unit,
                        item.lookback,
                        item.sources,
                        item.model_eligible,
                        item.reason,
                    )
                    for item in snapshot.values
                    if item.name.startswith("closed.")
                )

            if closed(self.current_snapshot) != closed(self.origin_snapshot):
                raise ContractViolation("pre/intraday closed facts must equal forecast origin")
        ordered = tuple(sorted(self.forecasts, key=lambda item: item.horizon))
        if tuple(item.horizon for item in ordered) != (1, 3, 5, 10) or any(
            item.instrument != self.instrument or item.origin_session_date != self.origin_snapshot.latest_bar_date
            for item in ordered
        ):
            raise ContractViolation("scenario requires exactly four matching forecasts")
        if (
            len({item.reference_price for item in ordered}) != 1
            or len({item.feature_set_version for item in ordered}) != 1
        ):
            raise ContractViolation("forecast bundle is inconsistent")
        from forecast.feature_sets import model_input_hash

        if any(
            item.model_input_hash
            != model_input_hash(self.origin_snapshot, item.origin_session_date, item.feature_set_id)
            for item in ordered
        ):
            raise ContractViolation("forecast model input hash does not match origin snapshot")
        if self.current_snapshot.quote_observed_at is not None and self.current_quote is None:
            raise ContractViolation("current snapshot quote requires the matching quote payload")
        if self.current_quote and (
            self.current_quote.instrument != self.instrument
            or self.current_snapshot.quote_observed_at != self.current_quote.observed_at
        ):
            raise ContractViolation("current quote does not match snapshot")
        current_price = next(
            (item for item in self.current_snapshot.values if item.name == "current.price"),
            None,
        )
        if (
            self.current_quote
            and current_price is not None
            and current_price.status.value == "available"
            and current_price.value != self.current_quote.price
        ):
            raise ContractViolation("current feature price does not match quote")
        updates = tuple(sorted(self.fact_updates, key=lambda item: (item.available_at, item.kind.value, item.stable_key)))
        if (
            updates != self.fact_updates
            or len({(item.kind, item.stable_key) for item in updates}) != len(updates)
            or any(not (self.origin_snapshot.cutoff_at < item.available_at <= as_of) for item in updates)
        ):
            raise ContractViolation("scenario fact updates are invalid")
        all_calendar_unavailable = all(
            item.availability.value == "calendar_unavailable" for item in ordered
        )
        if self.decision_session is None and not all_calendar_unavailable:
            raise ContractViolation("decision session required when h1 target exists")
        if self.decision_session and (
            self.decision_session.market != self.instrument.market
            or self.decision_session.exchange != self.instrument.exchange
            or ordered[0].target_session_date != self.decision_session.session_date
        ):
            raise ContractViolation("decision session does not match h1 forecast")
        if self.decision_session and mode is DecisionMode.PRE and as_of >= self.decision_session.regular_open:
            raise ContractViolation("pre scenario must precede the decision session open")
        if self.decision_session and mode is DecisionMode.INTRADAY and not (
            self.decision_session.regular_open <= as_of < self.decision_session.regular_close
        ):
            raise ContractViolation("intraday scenario must be inside the decision session")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "forecasts", ordered)


@dataclass(frozen=True, slots=True)
class HorizonAssessment:
    horizon: int
    target_session_date: date | None
    forecast_event_key: str
    evidence_grade: ForecastEvidenceGrade
    signal: HorizonSignal
    probabilities: DirectionProbabilities | None
    original_distribution: ReturnDistribution | None
    remaining_distribution: ReturnDistribution | None
    confidence_margin: float | None
    price_location: PriceLocation
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        grade = _enum(ForecastEvidenceGrade, self.evidence_grade, "forecast evidence grade")
        signal = _enum(HorizonSignal, self.signal, "horizon signal")
        location = _enum(PriceLocation, self.price_location, "price location")
        if self.horizon not in {1, 3, 5, 10} or not self.forecast_event_key:
            raise ContractViolation("invalid horizon assessment identity")
        if grade is ForecastEvidenceGrade.UNAVAILABLE:
            if signal is not HorizonSignal.UNAVAILABLE or any(
                value is not None
                for value in (
                    self.probabilities,
                    self.original_distribution,
                    self.remaining_distribution,
                    self.confidence_margin,
                )
            ):
                raise ContractViolation("unavailable horizon assessment contains forecast values")
        elif (
            signal is HorizonSignal.UNAVAILABLE
            or self.probabilities is None
            or self.original_distribution is None
            or self.confidence_margin is None
        ):
            raise ContractViolation("available horizon assessment is incomplete")
        margin = None
        if self.confidence_margin is not None:
            margin = ensure_finite(self.confidence_margin, "assessment confidence margin")
            if not 0.0 <= margin <= 1.0:
                raise ContractViolation("assessment confidence margin is out of range")
        object.__setattr__(self, "evidence_grade", grade)
        object.__setattr__(self, "signal", signal)
        object.__setattr__(self, "price_location", location)
        object.__setattr__(self, "confidence_margin", margin)
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes, "assessment reasons"))


@dataclass(frozen=True, slots=True)
class CurrentOverlay:
    price_state: CurrentPriceState
    current_price: float | None
    price_source: str | None
    observed_at: datetime | None
    realized_return_from_origin: float | None
    tactical_price_location: PriceLocation
    volatility_shock: VolatilityShock
    news_delta: NewsDeltaState
    news_update_present: bool
    fundamental_update_present: bool
    fact_update_count: int
    unmodeled_fact_update: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        state = _enum(CurrentPriceState, self.price_state, "current price state")
        location = _enum(PriceLocation, self.tactical_price_location, "tactical price location")
        shock = _enum(VolatilityShock, self.volatility_shock, "volatility shock")
        news = _enum(NewsDeltaState, self.news_delta, "news delta")
        observed = ensure_utc(self.observed_at, "overlay observed_at") if self.observed_at else None
        price = None if self.current_price is None else ensure_finite(self.current_price, "overlay current price", positive=True)
        realized = (
            None
            if self.realized_return_from_origin is None
            else ensure_finite(self.realized_return_from_origin, "overlay realized return")
        )
        if state in {CurrentPriceState.REFERENCE_CLOSE, CurrentPriceState.FRESH_QUOTE} and price is None:
            raise ContractViolation("available current price state requires a price")
        if state in {CurrentPriceState.EXPECTED_MISSING, CurrentPriceState.STALE_OR_MISSING} and any(
            value is not None for value in (price, self.price_source, observed, realized)
        ):
            raise ContractViolation("missing current price state cannot contain price evidence")
        if state is CurrentPriceState.REFERENCE_CLOSE and (self.price_source != "reference_close" or observed is not None):
            raise ContractViolation("reference close overlay has invalid source metadata")
        if state is CurrentPriceState.FRESH_QUOTE and (not self.price_source or observed is None):
            raise ContractViolation("fresh quote overlay requires source and observed_at")
        if self.fact_update_count < 0:
            raise ContractViolation("fact update count cannot be negative")
        expected_unmodeled = self.news_update_present or self.fundamental_update_present
        if self.unmodeled_fact_update != expected_unmodeled:
            raise ContractViolation("unmodeled fact update invariant failed")
        if (self.fact_update_count > 0) != expected_unmodeled:
            raise ContractViolation("fact update count does not match update flags")
        object.__setattr__(self, "price_state", state)
        object.__setattr__(self, "tactical_price_location", location)
        object.__setattr__(self, "volatility_shock", shock)
        object.__setattr__(self, "news_delta", news)
        object.__setattr__(self, "current_price", price)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "realized_return_from_origin", realized)
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes, "overlay reasons"))


@dataclass(frozen=True, slots=True)
class TradingScenario:
    scenario_id: str
    event_key: str
    instrument: InstrumentId
    mode: DecisionMode
    as_of: datetime
    origin_session_date: date
    decision_session: DecisionSession | None
    valid_from: datetime | None
    expires_at: datetime | None
    bias: ScenarioBias
    tactical_signal: BandSignal
    swing_signal: BandSignal
    alignment: HorizonAlignment
    state: ScenarioState
    forecast_support: ForecastSupportLevel
    status: ScenarioStatus
    horizon_assessments: tuple[HorizonAssessment, ...]
    current_overlay: CurrentOverlay
    allowed_strategy_families: tuple[StrategyFamily, ...]
    blocked_strategy_families: tuple[StrategyFamily, ...]
    entry_posture: EntryPosture
    exit_posture: ExitPosture
    reason_codes: tuple[str, ...]
    forecast_bundle_hash: str
    current_feature_hash: str
    fact_update_hash: str
    quality_hash: str
    policy_version: str
    generated_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        mode = _enum(DecisionMode, self.mode, "scenario mode")
        bias = _enum(ScenarioBias, self.bias, "scenario bias")
        tactical = _enum(BandSignal, self.tactical_signal, "tactical signal")
        swing = _enum(BandSignal, self.swing_signal, "swing signal")
        alignment = _enum(HorizonAlignment, self.alignment, "horizon alignment")
        state = _enum(ScenarioState, self.state, "scenario state")
        support = _enum(ForecastSupportLevel, self.forecast_support, "forecast support")
        status = _enum(ScenarioStatus, self.status, "scenario status")
        entry = _enum(EntryPosture, self.entry_posture, "entry posture")
        exit_posture = _enum(ExitPosture, self.exit_posture, "exit posture")
        as_of = ensure_utc(self.as_of, "scenario as_of")
        generated_at = ensure_utc(self.generated_at, "scenario generated_at")
        if self.schema_version < 1 or not str(self.policy_version or "").strip():
            raise ContractViolation("invalid trading scenario version")
        assessments = tuple(self.horizon_assessments)
        if tuple(item.horizon for item in assessments) != (1, 3, 5, 10):
            raise ContractViolation("invalid trading scenario horizons")
        if self.decision_session and (
            self.decision_session.market != self.instrument.market
            or self.decision_session.exchange != self.instrument.exchange
            or assessments[0].target_session_date != self.decision_session.session_date
        ):
            raise ContractViolation("scenario decision session does not match instrument or h1 target")
        allowed = tuple(_enum(StrategyFamily, item, "allowed family") for item in self.allowed_strategy_families)
        blocked = tuple(_enum(StrategyFamily, item, "blocked family") for item in self.blocked_strategy_families)
        expected_allowed = tuple(item for item in StrategyFamily if item in set(allowed))
        expected_blocked = tuple(item for item in StrategyFamily if item in set(blocked))
        if (
            allowed != expected_allowed
            or blocked != expected_blocked
            or set(allowed) & set(blocked)
            or set(allowed) | set(blocked) != set(StrategyFamily)
            or not PROTECTIVE_STRATEGY_FAMILIES.issubset(allowed)
        ):
            raise ContractViolation("scenario strategy families violate protection boundary")
        valid_from = ensure_utc(self.valid_from, "valid_from") if self.valid_from else None
        expires_at = ensure_utc(self.expires_at, "expires_at") if self.expires_at else None
        if self.decision_session is None:
            if valid_from is not None or expires_at is not None:
                raise ContractViolation("calendar-unavailable scenario has no validity window")
        elif valid_from is None or expires_at is None or not (
            self.decision_session.regular_open
            <= valid_from
            < expires_at
            <= self.decision_session.regular_close
        ):
            raise ContractViolation("scenario validity window is outside session")
        if status is ScenarioStatus.BLOCKED and entry is not EntryPosture.BLOCKED:
            raise ContractViolation("blocked scenario requires blocked entry posture")
        if status is ScenarioStatus.OBSERVATION_ONLY and entry is not EntryPosture.OBSERVATION_ONLY:
            raise ContractViolation("observation-only scenario requires observation-only entry posture")
        hashes = (
            _sha256(self.forecast_bundle_hash, "forecast bundle hash"),
            _sha256(self.current_feature_hash, "current feature hash"),
            _sha256(self.fact_update_hash, "fact update hash"),
            _sha256(self.quality_hash, "quality hash"),
        )
        identity = {
            "instrument": self.instrument,
            "mode": mode.value,
            "as_of": as_of,
            "origin_session_date": self.origin_session_date,
            "decision_session": self.decision_session,
            "valid_from": valid_from,
            "expires_at": expires_at,
            "forecast_bundle_hash": hashes[0],
            "current_feature_hash": hashes[1],
            "fact_update_hash": hashes[2],
            "quality_hash": hashes[3],
            "policy_version": self.policy_version,
        }
        expected_id = stable_hash(identity)
        session_key = self.decision_session.session_date.isoformat() if self.decision_session else "calendar-unavailable"
        expected_event = "|".join((self.instrument.stable_key, mode.value, session_key, expected_id))
        if self.scenario_id != expected_id or self.event_key != expected_event:
            raise ContractViolation("trading scenario identity does not match its business payload")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "bias", bias)
        object.__setattr__(self, "tactical_signal", tactical)
        object.__setattr__(self, "swing_signal", swing)
        object.__setattr__(self, "alignment", alignment)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "forecast_support", support)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "entry_posture", entry)
        object.__setattr__(self, "exit_posture", exit_posture)
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes, "scenario reasons"))
