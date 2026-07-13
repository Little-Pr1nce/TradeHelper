"""In-memory cache with explicit freshness boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from tradehelper_v2.contracts.enums import DecisionMode, ProviderStatus
from tradehelper_v2.contracts.market_data import InstrumentId, ensure_utc

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CacheKey:
    instrument: InstrumentId | None
    data_type: str
    mode: DecisionMode | None
    provider: str
    query_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.data_type or not self.provider:
            raise ValueError("cache key data_type and provider are required")
        mode = self.mode
        if mode is not None and not isinstance(mode, DecisionMode):
            mode = DecisionMode(str(mode))
            object.__setattr__(self, "mode", mode)


@dataclass(frozen=True, slots=True)
class CacheEntry(Generic[T]):
    value: T | None
    status: ProviderStatus
    cached_at: datetime
    expires_at: datetime
    source: str | None
    retry_at: datetime | None = None

    def __post_init__(self) -> None:
        status = self.status if isinstance(self.status, ProviderStatus) else ProviderStatus(str(self.status))
        cached_at = ensure_utc(self.cached_at, "cached_at")
        expires_at = ensure_utc(self.expires_at, "expires_at")
        if expires_at < cached_at:
            raise ValueError("cache expiry cannot precede cached_at")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "cached_at", cached_at)
        object.__setattr__(self, "expires_at", expires_at)
        if self.retry_at is not None:
            object.__setattr__(self, "retry_at", ensure_utc(self.retry_at, "retry_at"))

    def is_fresh(self, as_of: datetime) -> bool:
        return ensure_utc(as_of, "as_of") < self.expires_at


class DataCache:
    def __init__(self) -> None:
        self._entries: dict[CacheKey, CacheEntry[object]] = {}

    def get(self, key: CacheKey, as_of: datetime) -> CacheEntry[object] | None:
        entry = self._entries.get(key)
        return entry if entry is not None and entry.is_fresh(as_of) else None

    def put(self, key: CacheKey, entry: CacheEntry[T]) -> None:
        self._entries[key] = entry

    def clear(self) -> None:
        self._entries.clear()
