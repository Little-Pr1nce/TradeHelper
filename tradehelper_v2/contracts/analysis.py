"""Point-in-time feature contracts for the V2 analysis chain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping

from .enums import DecisionMode, ProviderStatus
from .market_data import (
    CanonicalBar,
    ContractViolation,
    FundamentalSnapshot,
    InstrumentId,
    NewsSnapshot,
    QuoteSnapshot,
    ensure_utc,
)
from .quality import DataQualityReport


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class FeatureStatus(_StringEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    INSUFFICIENT_HISTORY = "insufficient_history"
    STALE = "stale"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class FeatureGroup(_StringEnum):
    CLOSED_TECHNICAL = "closed_technical"
    CURRENT_MARKET = "current_market"
    NEWS = "news"
    FUNDAMENTALS = "fundamentals"
    MARKET_CONTEXT = "market_context"


class FeatureEvidenceMode(_StringEnum):
    OBSERVED_SNAPSHOT = "observed_snapshot"
    RECONSTRUCTED_HISTORY = "reconstructed_history"


@dataclass(frozen=True, slots=True)
class FeatureValue:
    name: str
    value: float | int | bool | str | None
    status: FeatureStatus
    unit: str | None
    lookback: int | None
    available_at: datetime
    sources: tuple[str, ...]
    model_eligible: bool
    reason: str | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not str(self.name or "").strip() or self.name != self.name.lower():
            raise ContractViolation("feature name must be a non-empty lowercase name")
        try:
            status = self.status if isinstance(self.status, FeatureStatus) else FeatureStatus(str(self.status))
        except ValueError as exc:
            raise ContractViolation("unsupported feature status") from exc
        if status is FeatureStatus.AVAILABLE:
            if self.value is None:
                raise ContractViolation("available feature must have a value")
            if isinstance(self.value, float) and not math.isfinite(self.value):
                raise ContractViolation("available feature value must be finite")
        elif self.value is not None:
            raise ContractViolation("unavailable feature value must be None")
        if self.lookback is not None and (isinstance(self.lookback, bool) or self.lookback <= 0):
            raise ContractViolation("feature lookback must be positive or None")
        if isinstance(self.value, (str, bool)) and self.model_eligible:
            raise ContractViolation("non-numeric feature cannot be model eligible")
        if self.name.startswith("current.") and self.model_eligible:
            raise ContractViolation("current features cannot be model eligible")
        normalized_sources = tuple(sorted({str(source) for source in self.sources if str(source)}))
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "available_at", ensure_utc(self.available_at, "feature available_at"))
        object.__setattr__(self, "sources", normalized_sources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "value": self.value, "status": self.status.value, "unit": self.unit,
            "lookback": self.lookback, "available_at": self.available_at,
            "sources": self.sources, "model_eligible": self.model_eligible, "reason": self.reason,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class FeatureInputs:
    instrument: InstrumentId
    mode: DecisionMode
    cutoff_at: datetime
    bars: tuple[CanonicalBar, ...]
    quote: QuoteSnapshot | None
    news: tuple[NewsSnapshot, ...]
    news_status: ProviderStatus
    fundamentals: FundamentalSnapshot | None
    fundamentals_status: ProviderStatus
    data_quality: DataQualityReport
    evidence_mode: FeatureEvidenceMode
    context: Mapping[str, FeatureValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mode = self.mode if isinstance(self.mode, DecisionMode) else DecisionMode(str(self.mode))
        evidence_mode = self.evidence_mode if isinstance(self.evidence_mode, FeatureEvidenceMode) else FeatureEvidenceMode(str(self.evidence_mode))
        news_status = self.news_status if isinstance(self.news_status, ProviderStatus) else ProviderStatus(str(self.news_status))
        fundamentals_status = self.fundamentals_status if isinstance(self.fundamentals_status, ProviderStatus) else ProviderStatus(str(self.fundamentals_status))
        if any(bar.instrument != self.instrument for bar in self.bars):
            raise ContractViolation("feature bars must match their instrument")
        trading_dates = tuple(bar.trading_date for bar in self.bars)
        if len(set(trading_dates)) != len(trading_dates):
            raise ContractViolation("feature bars cannot contain duplicate trading dates")
        if self.quote is not None and self.quote.instrument != self.instrument:
            raise ContractViolation("feature quote must match its instrument")
        if any(item.instrument != self.instrument for item in self.news):
            raise ContractViolation("feature news must match its instrument")
        if self.fundamentals is not None and self.fundamentals.instrument != self.instrument:
            raise ContractViolation("feature fundamentals must match its instrument")
        context = {str(name): value for name, value in self.context.items()}
        if any(not name.startswith("context.") or not isinstance(value, FeatureValue) for name, value in context.items()):
            raise ContractViolation("context values must be FeatureValue objects in the context namespace")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "cutoff_at", ensure_utc(self.cutoff_at, "feature cutoff_at"))
        object.__setattr__(self, "bars", tuple(self.bars))
        object.__setattr__(self, "news", tuple(self.news))
        object.__setattr__(self, "news_status", news_status)
        object.__setattr__(self, "fundamentals_status", fundamentals_status)
        object.__setattr__(self, "evidence_mode", evidence_mode)
        object.__setattr__(self, "context", MappingProxyType(dict(sorted(context.items()))))


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    instrument: InstrumentId
    mode: DecisionMode
    cutoff_at: datetime
    latest_bar_date: date | None
    quote_observed_at: datetime | None
    feature_set_version: str
    evidence_mode: FeatureEvidenceMode
    values: tuple[FeatureValue, ...]
    input_hash: str
    feature_hash: str
    generated_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        mode = self.mode if isinstance(self.mode, DecisionMode) else DecisionMode(str(self.mode))
        evidence_mode = self.evidence_mode if isinstance(self.evidence_mode, FeatureEvidenceMode) else FeatureEvidenceMode(str(self.evidence_mode))
        ordered = tuple(sorted(self.values, key=lambda value: value.name))
        if len({value.name for value in ordered}) != len(ordered):
            raise ContractViolation("feature names must be unique")
        if not str(self.feature_set_version or "").strip():
            raise ContractViolation("feature set version cannot be empty")
        hex_digits = frozenset("0123456789abcdef")
        if (
            len(self.input_hash) != 64
            or len(self.feature_hash) != 64
            or not set(self.input_hash).issubset(hex_digits)
            or not set(self.feature_hash).issubset(hex_digits)
        ):
            raise ContractViolation("feature hashes must be SHA-256 hex digests")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "cutoff_at", ensure_utc(self.cutoff_at, "feature cutoff_at"))
        object.__setattr__(self, "quote_observed_at", ensure_utc(self.quote_observed_at, "quote_observed_at") if self.quote_observed_at else None)
        object.__setattr__(self, "evidence_mode", evidence_mode)
        object.__setattr__(self, "values", ordered)
        object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "feature generated_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument.to_dict(), "mode": self.mode.value, "cutoff_at": self.cutoff_at,
            "latest_bar_date": self.latest_bar_date, "quote_observed_at": self.quote_observed_at,
            "feature_set_version": self.feature_set_version, "evidence_mode": self.evidence_mode.value,
            "values": tuple(value.to_dict() for value in self.values), "input_hash": self.input_hash,
            "feature_hash": self.feature_hash, "generated_at": self.generated_at,
            "schema_version": self.schema_version,
        }
