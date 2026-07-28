"""Shared, deterministic TickFlow A-share daily-K request budgets."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Protocol

from contracts.enums import Market
from contracts.market_data import ensure_utc
from .repository import SQLiteRepository


class DailyRateBudget(Protocol):
    def reserve(self, market: Market, as_of: datetime) -> datetime | None: ...


class InMemoryDailyRateBudget:
    """Shared per-service fallback used by deterministic tests."""

    def __init__(self, *, limit: int = 10, window: timedelta = timedelta(minutes=1)) -> None:
        self.limit = limit
        self.window = window
        self._events: dict[Market, deque[datetime]] = {Market.A: deque(), Market.US: deque()}

    def reserve(self, market: Market, as_of: datetime) -> datetime | None:
        now = ensure_utc(as_of, "as_of")
        events = self._events[market]
        while events and events[0] <= now - self.window:
            events.popleft()
        if len(events) >= self.limit:
            return events[0] + self.window
        events.append(now)
        return None


class SQLiteDailyRateBudget:
    """A process-safe V2 database budget shared by Tab1 and Tab3."""

    def __init__(self, repository: SQLiteRepository, *, limit: int = 10, window: timedelta = timedelta(minutes=1)) -> None:
        self.repository = repository
        self.limit = limit
        self.window = window

    def reserve(self, market: Market, as_of: datetime) -> datetime | None:
        return self.repository.reserve_provider_slot(
            "tickflow", market, "daily_bars", as_of, limit=self.limit, window=self.window
        )


class QuoteRateBudget(Protocol):
    def reserve(self, market: Market, request_count: int, as_of: datetime) -> datetime | None: ...


class InMemoryQuoteRateBudget:
    """One TickFlow quote subscription permits ten requests per rolling minute."""

    def __init__(self, *, limit: int = 10, window: timedelta = timedelta(minutes=1)) -> None:
        self.limit = limit
        self.window = window
        self._events: dict[Market, deque[datetime]] = {Market.A: deque(), Market.US: deque()}

    def reserve(self, market: Market, request_count: int, as_of: datetime) -> datetime | None:
        if request_count <= 0 or request_count > self.limit:
            raise ValueError("quote request count must be between 1 and the provider limit")
        now = ensure_utc(as_of, "as_of")
        events = self._events[market]
        while events and events[0] <= now - self.window:
            events.popleft()
        if len(events) + request_count > self.limit:
            return events[0] + self.window
        events.extend(now for _ in range(request_count))
        return None


class SQLiteQuoteRateBudget:
    """Persistent TickFlow quote budget shared by individual and batched requests."""

    def __init__(self, repository: SQLiteRepository, *, limit: int = 10, window: timedelta = timedelta(minutes=1)) -> None:
        self.repository = repository
        self.limit = limit
        self.window = window

    def reserve(self, market: Market, request_count: int, as_of: datetime) -> datetime | None:
        return self.repository.reserve_provider_slots(
            "tickflow", market, "quotes", as_of,
            count=request_count, limit=self.limit, window=self.window,
        )


class FinnhubRateBudget(Protocol):
    def reserve(self, as_of: datetime) -> datetime | None: ...


class InMemoryFinnhubRateBudget:
    """Finnhub free access is treated as one shared sixty-call rolling window."""

    def __init__(self, *, limit: int = 60, window: timedelta = timedelta(minutes=1)) -> None:
        self.limit = limit
        self.window = window
        self._events: deque[datetime] = deque()

    def reserve(self, as_of: datetime) -> datetime | None:
        now = ensure_utc(as_of, "as_of")
        while self._events and self._events[0] <= now - self.window:
            self._events.popleft()
        if len(self._events) >= self.limit:
            return self._events[0] + self.window
        self._events.append(now)
        return None


class SQLiteFinnhubRateBudget:
    """Persistent aggregate Finnhub budget across profile, metric and news endpoints."""

    def __init__(self, repository: SQLiteRepository, *, limit: int = 60, window: timedelta = timedelta(minutes=1)) -> None:
        self.repository = repository
        self.limit = limit
        self.window = window

    def reserve(self, as_of: datetime) -> datetime | None:
        return self.repository.reserve_provider_slot(
            "finnhub", Market.US, "all_api_calls", as_of, limit=self.limit, window=self.window
        )
