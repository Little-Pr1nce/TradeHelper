"""Provider 结果合同：适配器不能把未经验证的原始 payload 泄漏到上层。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Generic, Mapping, TypeVar

from .enums import ProviderStatus
from .market_data import CanonicalBar, ContractViolation, InstrumentId, QuoteSnapshot, ensure_date, ensure_utc

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider: str
    status: ProviderStatus
    started_at: datetime
    finished_at: datetime
    error_code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if not str(self.provider or "").strip():
            raise ContractViolation("provider attempt provider cannot be empty")
        status = self.status if isinstance(self.status, ProviderStatus) else ProviderStatus(str(self.status))
        started_at = ensure_utc(self.started_at, "started_at")
        finished_at = ensure_utc(self.finished_at, "finished_at")
        if finished_at < started_at:
            raise ContractViolation("provider attempt cannot finish before it starts")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)


@dataclass(frozen=True, slots=True)
class ProviderResult(Generic[T]):
    value: T | None
    status: ProviderStatus
    selected_source: str | None
    attempts: tuple[ProviderAttempt, ...]
    fallback_reason: str | None
    fetched_at: datetime
    retry_at: datetime | None = None

    def __post_init__(self) -> None:
        status = self.status if isinstance(self.status, ProviderStatus) else ProviderStatus(str(self.status))
        if status is ProviderStatus.OK and self.value is None:
            raise ContractViolation("ok provider result requires a value")
        if status is not ProviderStatus.OK and self.value is not None:
            raise ContractViolation("non-ok provider result cannot contain a value")
        if status is ProviderStatus.OK and not str(self.selected_source or "").strip():
            raise ContractViolation("ok provider result requires selected_source")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "attempts", tuple(self.attempts))
        fetched_at = ensure_utc(self.fetched_at, "fetched_at")
        retry_at = ensure_utc(self.retry_at, "retry_at") if self.retry_at is not None else None
        if retry_at is not None and retry_at <= fetched_at:
            raise ContractViolation("retry_at must be later than fetched_at")
        object.__setattr__(self, "fetched_at", fetched_at)
        object.__setattr__(self, "retry_at", retry_at)

    @classmethod
    def success(
        cls,
        value: T,
        source: str,
        fetched_at: datetime,
        attempts: tuple[ProviderAttempt, ...] = (),
        fallback_reason: str | None = None,
        retry_at: datetime | None = None,
    ) -> "ProviderResult[T]":
        if retry_at is not None:
            raise ContractViolation("successful provider result cannot include retry_at")
        return cls(value, ProviderStatus.OK, source, attempts, fallback_reason, fetched_at)

    @classmethod
    def failure(
        cls,
        status: ProviderStatus,
        fetched_at: datetime,
        attempts: tuple[ProviderAttempt, ...] = (),
        fallback_reason: str | None = None,
        retry_at: datetime | None = None,
    ) -> "ProviderResult[T]":
        return cls(None, status, None, attempts, fallback_reason, fetched_at, retry_at)


@dataclass(frozen=True, slots=True)
class QuoteBatch:
    quotes: Mapping[InstrumentId, QuoteSnapshot]
    failures: Mapping[InstrumentId, ProviderStatus]

    def __post_init__(self) -> None:
        quotes = dict(self.quotes)
        failures = {
            instrument: status if isinstance(status, ProviderStatus) else ProviderStatus(str(status))
            for instrument, status in self.failures.items()
        }
        overlap = set(quotes).intersection(failures)
        if overlap:
            raise ContractViolation("quote batch cannot contain both quote and failure for one instrument")
        object.__setattr__(self, "quotes", MappingProxyType(dict(sorted(quotes.items(), key=lambda item: item[0].stable_key))))
        object.__setattr__(self, "failures", MappingProxyType(dict(sorted(failures.items(), key=lambda item: item[0].stable_key))))


@dataclass(frozen=True, slots=True)
class DailyBarsRequest:
    instrument: InstrumentId
    requested_start: date
    requested_end: date
    listing_date: date | None = None

    def __post_init__(self) -> None:
        start = ensure_date(self.requested_start, "requested_start")
        end = ensure_date(self.requested_end, "requested_end")
        if start > end:
            raise ContractViolation("requested_start cannot be after requested_end")
        if self.listing_date is not None:
            ensure_date(self.listing_date, "listing_date")


@dataclass(frozen=True, slots=True)
class DailyBarsBatchResult:
    results: Mapping[InstrumentId, ProviderResult[tuple[CanonicalBar, ...]]]
    pending_retry_at: Mapping[InstrumentId, datetime]
    completed_at: datetime

    def __post_init__(self) -> None:
        results = dict(self.results)
        pending = {instrument: ensure_utc(value, "pending retry_at") for instrument, value in self.pending_retry_at.items()}
        if not set(pending).issubset(results):
            raise ContractViolation("pending daily requests must have a provider result")
        if any(results[instrument].status is not ProviderStatus.RATE_LIMITED for instrument in pending):
            raise ContractViolation("only rate-limited daily requests may be pending")
        object.__setattr__(self, "results", MappingProxyType(dict(sorted(results.items(), key=lambda item: item[0].stable_key))))
        object.__setattr__(self, "pending_retry_at", MappingProxyType(dict(sorted(pending.items(), key=lambda item: item[0].stable_key))))
        object.__setattr__(self, "completed_at", ensure_utc(self.completed_at, "completed_at"))


@dataclass(frozen=True, slots=True)
class MigrationPreflight:
    source_path: str
    source_exists: bool
    source_schema_detected: bool
    table_counts: Mapping[str, int]
    migratable_counts: Mapping[str, int]
    conflict_counts: Mapping[str, int]
    warnings: tuple[str, ...]
    read_only: bool
    evaluated_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "table_counts", MappingProxyType(dict(sorted(self.table_counts.items()))))
        object.__setattr__(self, "migratable_counts", MappingProxyType(dict(sorted(self.migratable_counts.items()))))
        object.__setattr__(self, "conflict_counts", MappingProxyType(dict(sorted(self.conflict_counts.items()))))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "evaluated_at", ensure_utc(self.evaluated_at, "evaluated_at"))
