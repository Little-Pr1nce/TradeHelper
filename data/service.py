"""V2 数据路由服务。

本模块负责来源选择与缓存刷新，并刻意止步于事实层：不能在这里计算指标、
预测或交易计划。这样 Provider 失败会以状态传播，而不是被高层猜测掩盖。

This module owns source selection and cache refresh.  It deliberately stops at
facts: it never calculates indicators, forecasts, strategies, or trade plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Generic, TypeVar

from contracts.enums import DecisionMode, FreshnessStatus, Market, ProviderStatus, QualityStatus, TradingSession
from contracts.market_data import (
    CanonicalBar,
    FundamentalSnapshot,
    FundamentalValue,
    InstrumentId,
    NewsSnapshot,
    QuoteSnapshot,
    StockMetadata,
    ensure_utc,
)
from contracts.providers import ProviderResult
from contracts.providers import DailyBarsBatchResult, DailyBarsRequest, ProviderAttempt, QuoteBatch
from .cache import CacheEntry, CacheKey, DataCache
from .calendar import TradingCalendar, TradingCalendarUnavailable
from .drift import DailyBarDriftMonitor
from .quality import assess_fundamental_quality, assess_quote_freshness, effective_start_date
from .rate_limit import (
    DailyRateBudget,
    FinnhubRateBudget,
    InMemoryDailyRateBudget,
    InMemoryFinnhubRateBudget,
    InMemoryQuoteRateBudget,
    QuoteRateBudget,
)
from .repository import DailyBarDriftRecord, SQLiteRepository

T = TypeVar("T")
_RETRYABLE_PROVIDER_STATUSES = {
    ProviderStatus.RATE_LIMITED,
    ProviderStatus.TIMEOUT,
    ProviderStatus.UNAVAILABLE,
}
_MAX_QUEUE_ATTEMPTS = 5
Loader = Callable[[], ProviderResult[T]]
DailyLoader = Callable[[InstrumentId, date, date], ProviderResult[tuple[CanonicalBar, ...]]]
QuoteLoader = Callable[[InstrumentId, TradingSession], ProviderResult[QuoteSnapshot]]
QuoteBatchLoader = Callable[[tuple[InstrumentId, ...], TradingSession], QuoteBatch]
InstrumentLoader = Callable[[InstrumentId], ProviderResult[T]]


@dataclass(frozen=True, slots=True)
class DataProviders:
    """Injected source clients; production composition lives in ``data.composition``."""

    tickflow_daily: DailyLoader | None = None
    nasdaq_daily: DailyLoader | None = None
    yfinance_daily: DailyLoader | None = None
    tickflow_quote: QuoteLoader | None = None
    tickflow_quotes: QuoteBatchLoader | None = None
    nasdaq_extended_quote: QuoteLoader | None = None
    yfinance_extended_quote: QuoteLoader | None = None
    tickflow_metadata: InstrumentLoader[StockMetadata] | None = None
    baostock_metadata: InstrumentLoader[StockMetadata] | None = None
    finnhub_metadata: InstrumentLoader[StockMetadata] | None = None
    baostock_listing_date: InstrumentLoader[date | None] | None = None
    finnhub_listing_date: InstrumentLoader[date | None] | None = None
    baostock_fundamentals: InstrumentLoader[FundamentalSnapshot] | None = None
    akshare_fundamentals: InstrumentLoader[FundamentalSnapshot] | None = None
    finnhub_fundamentals: InstrumentLoader[FundamentalSnapshot] | None = None
    yfinance_fundamentals: InstrumentLoader[FundamentalSnapshot] | None = None
    baidu_fundamentals: InstrumentLoader[FundamentalSnapshot] | None = None
    eastmoney_news: InstrumentLoader[tuple[NewsSnapshot, ...]] | None = None
    akshare_news: InstrumentLoader[tuple[NewsSnapshot, ...]] | None = None
    finnhub_news: InstrumentLoader[tuple[NewsSnapshot, ...]] | None = None


def _unavailable(now: datetime) -> ProviderResult[T]:
    return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now)


def _merge_fallback(
    primary: ProviderResult[T], fallback: ProviderResult[T], reason: str
) -> ProviderResult[T]:
    attempts = primary.attempts + fallback.attempts
    if fallback.status is ProviderStatus.OK:
        return ProviderResult.success(
            fallback.value, fallback.selected_source or "fallback", fallback.fetched_at, attempts, reason
        )
    # A provider-declared retry window is more informative than an unrelated
    # fallback being unavailable.  Preserve it so callers can queue the fact
    # instead of incorrectly treating it as permanent absence.
    if primary.status is ProviderStatus.RATE_LIMITED:
        return ProviderResult.failure(
            ProviderStatus.RATE_LIMITED, fallback.fetched_at, attempts, reason, primary.retry_at
        )
    return ProviderResult.failure(fallback.status, fallback.fetched_at, attempts, reason)


class DataRefreshService:
    """Shared refresh boundary for Tab1 and Tab3, independent of page call order."""

    def __init__(
        self,
        providers: DataProviders,
        calendar: TradingCalendar,
        cache: DataCache,
        repository: SQLiteRepository | None = None,
        daily_rate_budget: DailyRateBudget | None = None,
        quote_rate_budget: QuoteRateBudget | None = None,
        finnhub_rate_budget: FinnhubRateBudget | None = None,
    ) -> None:
        self.providers = providers
        self.calendar = calendar
        self.cache = cache
        self.repository = repository
        self.daily_rate_budget = daily_rate_budget or InMemoryDailyRateBudget()
        self.quote_rate_budget = quote_rate_budget or InMemoryQuoteRateBudget()
        self.finnhub_rate_budget = finnhub_rate_budget or InMemoryFinnhubRateBudget()

    def get_or_refresh(
        self,
        key: CacheKey,
        ttl: timedelta,
        as_of: datetime,
        loader: Loader[T],
    ) -> ProviderResult[T]:
        now = ensure_utc(as_of, "as_of")
        cached = self.cache.get(key, now)
        if cached is not None:
            if cached.status is ProviderStatus.OK:
                return ProviderResult.success(cached.value, cached.source or key.provider, cached.cached_at)
            return ProviderResult.failure(cached.status, cached.cached_at, retry_at=cached.retry_at)
        result = loader()
        if result.status is ProviderStatus.RATE_LIMITED and result.retry_at is None:
            result = ProviderResult.failure(
                ProviderStatus.RATE_LIMITED, result.fetched_at, result.attempts,
                result.fallback_reason, now + timedelta(minutes=1),
            )
        cache_ttl = ttl if result.status is ProviderStatus.OK else timedelta(minutes=5)
        if result.retry_at is not None:
            cache_ttl = max(result.retry_at - now, timedelta(seconds=1))
        self.cache.put(
            key,
            CacheEntry(result.value, result.status, now, now + cache_ttl, result.selected_source, result.retry_at),
        )
        return result

    def refresh_daily_bars(
        self,
        instrument: InstrumentId,
        requested_start: date,
        requested_end: date,
        listing_date: date | None,
        as_of: datetime,
        *,
        skip_tickflow: bool = False,
    ) -> ProviderResult[tuple[CanonicalBar, ...]]:
        """Route daily data and enforce the listing and completed-session windows."""
        now = ensure_utc(as_of, "as_of")
        start = effective_start_date(requested_start, listing_date)
        if start > requested_end:
            return ProviderResult.failure(ProviderStatus.EMPTY, now)
        key = CacheKey(instrument, "daily_bars", None, "daily-route", (start.isoformat(), requested_end.isoformat()))
        cached_bars: tuple[CanonicalBar, ...] = ()
        fetch_start = start
        fetch_end = requested_end
        if self.repository is not None:
            cached_bars = self.repository.list_daily_bars(instrument, start, requested_end)
            if cached_bars:
                expected_first = next(
                    (
                        start + timedelta(days=offset)
                        for offset in range((requested_end - start).days + 1)
                        if self.calendar.is_session(instrument.market, start + timedelta(days=offset))
                    ),
                    None,
                )
                leading_history_missing = expected_first is not None and cached_bars[0].trading_date > expected_first
                if leading_history_missing:
                    # A shorter earlier analysis may have populated only the
                    # recent tail. Re-fetch the requested window once so a 1y
                    # request cannot be incorrectly satisfied by a 3m cache.
                    fetch_start = start
                else:
                    fetch_start = max(start, cached_bars[-1].trading_date + timedelta(days=1))
                if fetch_start > fetch_end:
                    return ProviderResult.success(cached_bars, "repository", now)

        def load() -> ProviderResult[tuple[CanonicalBar, ...]]:
            if instrument.market is Market.US:
                primary = self._call_daily(self.providers.nasdaq_daily, instrument, fetch_start, fetch_end, now)
                result = primary
                if primary.status is not ProviderStatus.OK:
                    yfinance = self._call_daily(self.providers.yfinance_daily, instrument, fetch_start, fetch_end, now)
                    result = _merge_fallback(primary, yfinance, "nasdaq historical unavailable; yfinance limited to completed sessions")
                if result.status is not ProviderStatus.OK:
                    tickflow = self._call_daily(self.providers.tickflow_daily, instrument, fetch_start, fetch_end, now)
                    result = _merge_fallback(result, tickflow, "nasdaq/yfinance historical unavailable; tickflow fallback")
            else:
                retry_at = self._daily_budget_exhausted_at(now) if skip_tickflow else self.daily_rate_budget.reserve(Market.A, now)
                result = (
                    self._daily_budget_exhausted(now, retry_at)
                    if retry_at is not None
                    else self._call_daily(self.providers.tickflow_daily, instrument, fetch_start, fetch_end, now)
                )
            if result.status is not ProviderStatus.OK or result.value is None:
                return result
            try:
                completed_end = self.calendar.latest_completed_session(instrument.market, now)
            except TradingCalendarUnavailable:
                return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now, result.attempts, "exchange calendar unavailable")
            response_bars = tuple(
                bar for bar in result.value if bar.trading_date <= min(fetch_end, completed_end)
            )
            before_listing = tuple(
                bar for bar in response_bars if listing_date is not None and bar.trading_date < listing_date
            )
            valid_listing = tuple(bar for bar in response_bars if bar not in before_listing)
            candidate_bars = tuple(bar for bar in valid_listing if bar.trading_date >= fetch_start)
            try:
                invalid_sessions = tuple(
                    bar for bar in candidate_bars if not self.calendar.is_session(instrument.market, bar.trading_date)
                )
            except TradingCalendarUnavailable:
                return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now, result.attempts, "exchange calendar unavailable")
            cached_keys = {(bar.trading_date, bar.adjustment_mode) for bar in cached_bars}
            new_bars = tuple(
                bar for bar in candidate_bars
                if bar not in invalid_sessions and (bar.trading_date, bar.adjustment_mode) not in cached_keys
            )
            bars = tuple(sorted((*cached_bars, *new_bars), key=lambda bar: bar.trading_date))
            if not bars:
                return ProviderResult.failure(ProviderStatus.EMPTY, result.fetched_at, result.attempts, result.fallback_reason)
            if self.repository is not None:
                self.repository.quarantine_daily_bars(instrument, start, "before_listing_date")
                self.repository.quarantine_received_daily_bars(before_listing, "before_listing_date")
                self.repository.quarantine_received_daily_bars(invalid_sessions, "trading_date_not_in_exchange_calendar")
                self.repository.upsert_daily_bars(bars)
            return ProviderResult.success(bars, result.selected_source or "tickflow", result.fetched_at, result.attempts, result.fallback_reason)

        return self.get_or_refresh(key, timedelta(minutes=1), now, load)

    def refresh_daily_bars_batch(
        self,
        requests: tuple[DailyBarsRequest, ...],
        as_of: datetime,
    ) -> DailyBarsBatchResult:
        """Refresh a portfolio without turning provider quota exhaustion into empty data.

        TickFlow exposes one-symbol A-share daily K requests.  At most ten A-share
        requests are issued per round.  US daily bars use Nasdaq historical data;
        excess A-share requests remain explicitly pending and expose a retry
        timestamp for the caller's background queue.
        """
        now = ensure_utc(as_of, "as_of")
        results: dict[InstrumentId, ProviderResult[tuple[CanonicalBar, ...]]] = {}
        pending: dict[InstrumentId, datetime] = {}
        seen: set[InstrumentId] = set()
        for request in requests:
            if request.instrument in seen:
                raise ValueError("daily refresh batch cannot contain duplicate instruments")
            seen.add(request.instrument)
            results[request.instrument] = self.refresh_daily_bars(
                request.instrument,
                request.requested_start,
                request.requested_end,
                request.listing_date,
                now,
                skip_tickflow=False,
            )
            if results[request.instrument].status is ProviderStatus.RATE_LIMITED:
                retry_at = results[request.instrument].retry_at or now + timedelta(minutes=1)
                pending[request.instrument] = retry_at
                if self.repository is not None:
                    self.repository.enqueue_daily_refresh(request, retry_at)
        return DailyBarsBatchResult(results, pending, now)

    def refresh_due_daily_bars(self, as_of: datetime, *, limit: int = 10) -> DailyBarsBatchResult:
        """Resume durable A-share daily-bar work after its recorded retry time."""
        if self.repository is None:
            raise RuntimeError("refresh_due_daily_bars requires a V2 repository")
        now = ensure_utc(as_of, "as_of")
        due = self.repository.due_daily_refreshes(now, limit=limit)
        results: dict[InstrumentId, ProviderResult[tuple[CanonicalBar, ...]]] = {}
        pending: dict[InstrumentId, datetime] = {}
        for item in due:
            result = self.refresh_daily_bars(
                item.request.instrument, item.request.requested_start, item.request.requested_end,
                item.request.listing_date, now,
            )
            results[item.request.instrument] = result
            attempts = item.attempts + 1
            if result.status in _RETRYABLE_PROVIDER_STATUSES and attempts < _MAX_QUEUE_ATTEMPTS:
                retry_at = self._next_retry_at(result, now, attempts)
                self.repository.reschedule_daily_refresh(item.queue_id, retry_at, attempts)
                if result.status is ProviderStatus.RATE_LIMITED:
                    pending[item.request.instrument] = retry_at
            elif result.status in _RETRYABLE_PROVIDER_STATUSES or result.status is ProviderStatus.INVALID_PAYLOAD:
                self.repository.mark_daily_refresh_failed(item.queue_id, attempts)
            else:
                self.repository.mark_daily_refresh_complete(item.queue_id)
        return DailyBarsBatchResult(results, pending, now)

    def audit_us_daily_source_drift(
        self, instrument: InstrumentId, start: date, end: date, as_of: datetime
    ) -> tuple[DailyBarDriftRecord, ...]:
        """Persist an explicit cross-source audit without changing the daily-bar route.

        This is intentionally an opt-in maintenance action.  Normal analyses use
        the configured primary/fallback route and must not spend extra provider
        quota merely to compare sources.
        """
        if instrument.market is not Market.US:
            raise ValueError("daily source drift audit is currently defined for US daily bars")
        if self.repository is None:
            raise RuntimeError("daily source drift audit requires a V2 repository")
        now = ensure_utc(as_of, "as_of")
        primary = self._call_daily(self.providers.nasdaq_daily, instrument, start, end, now)
        if primary.status is not ProviderStatus.OK or primary.value is None:
            return ()
        monitor = DailyBarDriftMonitor(self.repository)
        records: list[DailyBarDriftRecord] = []
        for loader in (self.providers.yfinance_daily, self.providers.tickflow_daily):
            comparison = self._call_daily(loader, instrument, start, end, now)
            if comparison.status is ProviderStatus.OK and comparison.value is not None:
                records.extend(monitor.compare(primary.value, comparison.value, now))
        return tuple(records)

    def refresh_quote(
        self, instrument: InstrumentId, mode: DecisionMode, as_of: datetime
    ) -> ProviderResult[QuoteSnapshot]:
        """Use TickFlow only in regular hours; extended US routes Nasdaq -> yfinance."""
        now = ensure_utc(as_of, "as_of")
        if mode is DecisionMode.EOD:
            return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now)
        if mode is DecisionMode.PRE and instrument.market is Market.A:
            return ProviderResult.failure(ProviderStatus.UNAVAILABLE, now, fallback_reason="A股盘前没有连续实时价；使用T-1条件计划")
        session = TradingSession.REGULAR if mode is DecisionMode.INTRADAY else TradingSession.PRE
        provider = "tickflow" if mode is DecisionMode.INTRADAY else "extended-route"
        key = CacheKey(instrument, "quote", mode, provider, (session.value,))

        def load() -> ProviderResult[QuoteSnapshot]:
            if mode is DecisionMode.INTRADAY:
                retry_at = self.quote_rate_budget.reserve(instrument.market, 1, now)
                result = (
                    self._quote_budget_exhausted(now, retry_at)
                    if retry_at is not None
                    else self._call_quote(self.providers.tickflow_quote, instrument, session, now)
                )
            else:
                primary = self._call_quote(self.providers.nasdaq_extended_quote, instrument, session, now)
                if primary.status is ProviderStatus.OK and primary.value is not None:
                    primary_quote = assess_quote_freshness(primary.value, mode, now)
                    if primary_quote.freshness_status is FreshnessStatus.FRESH:
                        result = ProviderResult.success(
                            primary_quote, primary.selected_source or "nasdaq", primary.fetched_at,
                            primary.attempts, primary.fallback_reason,
                        )
                    else:
                        fallback = self._call_quote(self.providers.yfinance_extended_quote, instrument, session, now)
                        if fallback.status is ProviderStatus.OK and fallback.value is not None:
                            fallback_quote = assess_quote_freshness(fallback.value, mode, now)
                            result = (
                                ProviderResult.success(
                                    fallback_quote, fallback.selected_source or "yfinance", fallback.fetched_at,
                                    primary.attempts + fallback.attempts,
                                    "nasdaq extended quote timestamp is missing or stale; yfinance fallback",
                                )
                                if fallback_quote.freshness_status is FreshnessStatus.FRESH
                                else primary
                            )
                        else:
                            result = primary
                else:
                    fallback = self._call_quote(self.providers.yfinance_extended_quote, instrument, session, now)
                    result = _merge_fallback(
                        primary, fallback, "nasdaq extended quote unavailable; yfinance fallback"
                    )
            if result.status is not ProviderStatus.OK or result.value is None:
                return result
            quote = assess_quote_freshness(result.value, mode, now)
            if self.repository is not None:
                self.repository.save_quote_snapshot(quote)
            return ProviderResult.success(quote, result.selected_source or "unknown", result.fetched_at, result.attempts, result.fallback_reason)

        return self.get_or_refresh(key, timedelta(minutes=1), now, load)

    def refresh_intraday_quotes(
        self, instruments: tuple[InstrumentId, ...], as_of: datetime
    ) -> QuoteBatch:
        """Fetch up to 50 same-market regular-session quotes without exceeding TickFlow's request quota."""
        now = ensure_utc(as_of, "as_of")
        unique = tuple(dict.fromkeys(instruments))
        if not unique:
            return QuoteBatch({}, {})
        market = unique[0].market
        if any(item.market is not market for item in unique):
            raise ValueError("a TickFlow batch quote request cannot mix A-share and US instruments")
        quotes: dict[InstrumentId, QuoteSnapshot] = {}
        missing: list[InstrumentId] = []
        for instrument in unique:
            key = CacheKey(instrument, "quote", DecisionMode.INTRADAY, "tickflow", (TradingSession.REGULAR.value,))
            cached = self.cache.get(key, now)
            cached_quote = cached.value if cached is not None and cached.status is ProviderStatus.OK else None
            if not isinstance(cached_quote, QuoteSnapshot) and self.repository is not None:
                stored = self.repository.get_latest_quote(instrument, TradingSession.REGULAR)
                if stored is not None:
                    refreshed_stored = assess_quote_freshness(stored, DecisionMode.INTRADAY, now)
                    if refreshed_stored.freshness_status is FreshnessStatus.FRESH:
                        cached_quote = refreshed_stored
            if isinstance(cached_quote, QuoteSnapshot):
                refreshed_cached = assess_quote_freshness(cached_quote, DecisionMode.INTRADAY, now)
                if refreshed_cached.freshness_status is FreshnessStatus.FRESH:
                    quotes[instrument] = refreshed_cached
                    continue
            missing.append(instrument)
        accepted = tuple(missing[:50])
        failures: dict[InstrumentId, ProviderStatus] = {item: ProviderStatus.RATE_LIMITED for item in missing[50:]}
        if not accepted:
            return QuoteBatch(quotes, failures)
        request_count = (len(accepted) + 4) // 5
        retry_at = self.quote_rate_budget.reserve(market, request_count, now)
        if retry_at is not None:
            failures.update({item: ProviderStatus.RATE_LIMITED for item in accepted})
            return QuoteBatch(quotes, failures)
        if self.providers.tickflow_quotes is None:
            failures.update({item: ProviderStatus.UNAVAILABLE for item in accepted})
            return QuoteBatch(quotes, failures)
        raw_batch = self.providers.tickflow_quotes(accepted, TradingSession.REGULAR)
        for instrument, quote in raw_batch.quotes.items():
            refreshed = assess_quote_freshness(quote, DecisionMode.INTRADAY, now)
            quotes[instrument] = refreshed
            key = CacheKey(instrument, "quote", DecisionMode.INTRADAY, "tickflow", (TradingSession.REGULAR.value,))
            self.cache.put(
                key, CacheEntry(refreshed, ProviderStatus.OK, now, now + timedelta(minutes=1), refreshed.source)
            )
            if self.repository is not None:
                self.repository.save_quote_snapshot(refreshed)
        failures.update(raw_batch.failures)
        return QuoteBatch(quotes, failures)

    def refresh_metadata(self, instrument: InstrumentId, as_of: datetime) -> ProviderResult[StockMetadata]:
        now = ensure_utc(as_of, "as_of")
        key = CacheKey(instrument, "metadata", None, "metadata-route")

        def load() -> ProviderResult[StockMetadata]:
            # TickFlow has no lightweight metadata endpoint in this subscription:
            # the legacy SDK gets a one-row daily K request for a name.  Keep that
            # expensive fallback away from the A-share daily-bar quota whenever
            # baostock/Finnhub can supply the same factual identifier.
            primary_loader = self.providers.baostock_metadata if instrument.market is Market.A else self.providers.finnhub_metadata
            primary = (
                self._call_instrument(primary_loader, instrument, now)
                if instrument.market is Market.A
                else self._call_finnhub_instrument(primary_loader, instrument, now)
            )
            if primary.status is ProviderStatus.OK:
                result = primary
            else:
                retry_at = self.daily_rate_budget.reserve(instrument.market, now)
                tickflow = (
                    ProviderResult.failure(ProviderStatus.RATE_LIMITED, now, retry_at=retry_at)
                    if retry_at is not None
                    else self._call_instrument(self.providers.tickflow_metadata, instrument, now)
                )
                result = _merge_fallback(
                    primary, tickflow, "authoritative metadata unavailable; TickFlow daily-K metadata fallback"
                )
            if result.status is ProviderStatus.OK and result.value is not None and self.repository is not None:
                self.repository.upsert_stock_metadata(result.value)
            return result

        return self._queue_provider_refresh_if_limited(
            "metadata", instrument, self.get_or_refresh(key, timedelta(days=7), now, load), now
        )

    def refresh_listing_date(self, instrument: InstrumentId, as_of: datetime) -> ProviderResult[date | None]:
        now = ensure_utc(as_of, "as_of")
        cached_metadata = self.cache.get(CacheKey(instrument, "metadata", None, "metadata-route"), now)
        if cached_metadata is not None and cached_metadata.status is ProviderStatus.OK and cached_metadata.value is not None:
            metadata = cached_metadata.value
            if isinstance(metadata, StockMetadata) and metadata.listing_date is not None:
                return ProviderResult.success(metadata.listing_date, metadata.source, cached_metadata.cached_at)
        key = CacheKey(instrument, "listing_date", None, "listing-route")
        loader = self.providers.baostock_listing_date if instrument.market is Market.A else self.providers.finnhub_listing_date
        result = self.get_or_refresh(
            key,
            timedelta(days=3650),
            now,
            lambda: self._call_instrument(loader, instrument, now)
            if instrument.market is Market.A
            else self._call_finnhub_instrument(loader, instrument, now),
        )
        return self._queue_provider_refresh_if_limited("listing_date", instrument, result, now)

    def refresh_news(
        self, instrument: InstrumentId, mode: DecisionMode, as_of: datetime
    ) -> ProviderResult[tuple[NewsSnapshot, ...]]:
        now = ensure_utc(as_of, "as_of")
        ttl = {DecisionMode.INTRADAY: timedelta(minutes=30), DecisionMode.PRE: timedelta(minutes=60), DecisionMode.EOD: timedelta(hours=6)}[mode]
        key = CacheKey(instrument, "news", mode, "news-route")

        def load() -> ProviderResult[tuple[NewsSnapshot, ...]]:
            if instrument.market is Market.A:
                primary_loader, fallback_loaders = self.providers.eastmoney_news, (self.providers.akshare_news,)
            else:
                primary_loader, fallback_loaders = self.providers.finnhub_news, ()
            primary = (
                self._call_instrument(primary_loader, instrument, now)
                if instrument.market is Market.A
                else self._call_finnhub_instrument(primary_loader, instrument, now)
            )
            result = primary
            if primary.status is not ProviderStatus.OK:
                for fallback_loader in fallback_loaders:
                    fallback = self._call_instrument(fallback_loader, instrument, now)
                    result = _merge_fallback(result, fallback, "primary news unavailable")
                    if result.status is ProviderStatus.OK:
                        break
            if result.status is ProviderStatus.OK and result.value is not None and self.repository is not None:
                self.repository.upsert_news(result.value)
            return result

        return self._queue_provider_refresh_if_limited(
            "news", instrument, self.get_or_refresh(key, ttl, now, load), now, mode=mode
        )

    def refresh_fundamentals(self, instrument: InstrumentId, as_of: datetime) -> ProviderResult[FundamentalSnapshot]:
        now = ensure_utc(as_of, "as_of")
        key = CacheKey(instrument, "fundamentals", None, "fundamental-route")

        def load() -> ProviderResult[FundamentalSnapshot]:
            if instrument.market is Market.A:
                primary_loader = self.providers.baostock_fundamentals
                fallback_loaders = (self.providers.akshare_fundamentals,)
            else:
                primary_loader = self.providers.finnhub_fundamentals
                fallback_loaders = (
                    self.providers.yfinance_fundamentals,
                    self.providers.akshare_fundamentals,
                    self.providers.baidu_fundamentals,
                )
            primary = (
                self._call_instrument(primary_loader, instrument, now)
                if instrument.market is Market.A
                else self._call_finnhub_instrument(primary_loader, instrument, now)
            )
            snapshots: list[FundamentalSnapshot] = []
            attempts = list(primary.attempts)
            if primary.status is ProviderStatus.OK and primary.value is not None and primary.value.instrument == instrument:
                snapshots.append(primary.value)
            elif primary.status is ProviderStatus.OK:
                primary = ProviderResult.failure(
                    ProviderStatus.INVALID_PAYLOAD, primary.fetched_at, primary.attempts,
                    "fundamental provider returned the wrong instrument",
                )
            last_failure: ProviderResult[FundamentalSnapshot] = primary
            current_quality = (
                assess_fundamental_quality(snapshots[0]).quality_status
                if snapshots else QualityStatus.BLOCKED
            )
            a_share_semantic_supplement = (
                instrument.market is Market.A
                and bool(snapshots)
                and not {"weighted_roe_annual", "revenue_yoy_annual"}.issubset(snapshots[0].fields)
            )
            us_growth_supplement = (
                instrument.market is Market.US
                and bool(snapshots)
                and not {
                    "netIncomeGrowthTTMYoy", "netIncomeGrowthQuarterlyYoy", "net_profit_yoy",
                }.intersection(snapshots[0].fields)
            )
            if current_quality is not QualityStatus.OK or a_share_semantic_supplement or us_growth_supplement:
                for fallback_loader in fallback_loaders:
                    fallback = self._call_instrument(fallback_loader, instrument, now)
                    attempts.extend(fallback.attempts)
                    if fallback.status is ProviderStatus.OK and fallback.value is not None and fallback.value.instrument == instrument:
                        snapshots.append(fallback.value)
                        combined = self._combine_fundamentals(instrument, snapshots)
                        if combined.quality_status is QualityStatus.OK:
                            break
                    else:
                        if fallback.status is ProviderStatus.OK:
                            fallback = ProviderResult.failure(
                                ProviderStatus.INVALID_PAYLOAD, fallback.fetched_at, fallback.attempts,
                                "fundamental provider returned the wrong instrument",
                            )
                        last_failure = fallback
            if snapshots:
                combined = self._combine_fundamentals(instrument, snapshots)
                result = ProviderResult.success(
                    combined, combined.provider, combined.fetched_at, tuple(attempts),
                    "fundamental fields supplemented by fallback" if len(snapshots) > 1 else None,
                )
            else:
                final_status = (
                    ProviderStatus.RATE_LIMITED
                    if primary.status is ProviderStatus.RATE_LIMITED
                    else last_failure.status
                )
                result = ProviderResult.failure(
                    final_status,
                    last_failure.fetched_at,
                    tuple(attempts),
                    "all fundamental providers unavailable",
                    primary.retry_at if final_status is ProviderStatus.RATE_LIMITED else last_failure.retry_at,
                )
            if result.status is ProviderStatus.OK and result.value is not None and self.repository is not None:
                self.repository.upsert_fundamental_snapshot(result.value)
            return result

        return self._queue_provider_refresh_if_limited(
            "fundamentals", instrument, self.get_or_refresh(key, timedelta(hours=24), now, load), now
        )

    @staticmethod
    def _combine_fundamentals(
        instrument: InstrumentId, snapshots: list[FundamentalSnapshot]
    ) -> FundamentalSnapshot:
        fields: dict[str, FundamentalValue] = {}
        providers: list[str] = []
        for snapshot in snapshots:
            providers.append(snapshot.provider)
            for name, value in snapshot.fields.items():
                fields.setdefault(name, value)
        combined = FundamentalSnapshot(
            instrument=instrument,
            fields=fields,
            available_at=max(snapshot.available_at for snapshot in snapshots),
            fetched_at=max(snapshot.fetched_at for snapshot in snapshots),
            provider="+".join(dict.fromkeys(providers)),
            quality_status=QualityStatus.BLOCKED,
        )
        return assess_fundamental_quality(combined)

    def refresh_due_provider_facts(self, as_of: datetime, *, limit: int = 60) -> dict[int, ProviderStatus]:
        """Resume metadata, listing, fundamental and news tasks deferred by provider quotas."""
        if self.repository is None:
            raise RuntimeError("refresh_due_provider_facts requires a V2 repository")
        now = ensure_utc(as_of, "as_of")
        outcomes: dict[int, ProviderStatus] = {}
        for item in self.repository.due_provider_refreshes(now, limit=limit):
            if item.task_type == "metadata":
                result = self.refresh_metadata(item.instrument, now)
            elif item.task_type == "listing_date":
                result = self.refresh_listing_date(item.instrument, now)
            elif item.task_type == "fundamentals":
                result = self.refresh_fundamentals(item.instrument, now)
            else:
                result = self.refresh_news(item.instrument, item.mode or DecisionMode.EOD, now)
            outcomes[item.queue_id] = result.status
            attempts = item.attempts + 1
            if result.status in _RETRYABLE_PROVIDER_STATUSES and attempts < _MAX_QUEUE_ATTEMPTS:
                self.repository.reschedule_provider_refresh(
                    item.queue_id, self._next_retry_at(result, now, attempts), attempts
                )
            elif result.status in _RETRYABLE_PROVIDER_STATUSES or result.status is ProviderStatus.INVALID_PAYLOAD:
                self.repository.mark_provider_refresh_failed(item.queue_id, attempts)
            else:
                self.repository.mark_provider_refresh_complete(item.queue_id)
        return outcomes

    @staticmethod
    def _call_daily(loader: DailyLoader | None, instrument: InstrumentId, start: date, end: date, now: datetime) -> ProviderResult[tuple[CanonicalBar, ...]]:
        return loader(instrument, start, end) if loader is not None else _unavailable(now)

    @staticmethod
    def _daily_budget_exhausted_at(now: datetime) -> datetime:
        return now + timedelta(minutes=1)

    @staticmethod
    def _daily_budget_exhausted(now: datetime, retry_at: datetime | None = None) -> ProviderResult[tuple[CanonicalBar, ...]]:
        retry_at = retry_at or now + timedelta(minutes=1)
        attempt = ProviderAttempt(
            "tickflow", ProviderStatus.RATE_LIMITED, now, now,
            "daily_request_budget_exhausted", "TickFlow日K本轮最多10次；等待下一配额窗口",
        )
        return ProviderResult.failure(
            ProviderStatus.RATE_LIMITED,
            now,
            (attempt,),
            "TickFlow日K配额已用尽",
            retry_at,
        )

    @staticmethod
    def _quote_budget_exhausted(now: datetime, retry_at: datetime | None = None) -> ProviderResult[QuoteSnapshot]:
        retry_at = retry_at or now + timedelta(minutes=1)
        attempt = ProviderAttempt(
            "tickflow", ProviderStatus.RATE_LIMITED, now, now,
            "quote_request_budget_exhausted", "TickFlow实时报价本轮配额已用尽；等待下一配额窗口",
        )
        return ProviderResult.failure(
            ProviderStatus.RATE_LIMITED, now, (attempt,), "TickFlow实时报价配额已用尽", retry_at,
        )

    @staticmethod
    def _call_quote(loader: QuoteLoader | None, instrument: InstrumentId, session: TradingSession, now: datetime) -> ProviderResult[QuoteSnapshot]:
        return loader(instrument, session) if loader is not None else _unavailable(now)

    @staticmethod
    def _call_instrument(loader: InstrumentLoader[T] | None, instrument: InstrumentId, now: datetime) -> ProviderResult[T]:
        return loader(instrument) if loader is not None else _unavailable(now)

    def _call_finnhub_instrument(
        self, loader: InstrumentLoader[T] | None, instrument: InstrumentId, now: datetime
    ) -> ProviderResult[T]:
        retry_at = self.finnhub_rate_budget.reserve(now)
        if retry_at is None:
            return self._call_instrument(loader, instrument, now)
        attempt = ProviderAttempt(
            "finnhub", ProviderStatus.RATE_LIMITED, now, now,
            "aggregate_request_budget_exhausted", "Finnhub所有端点共享的每分钟请求额度已用尽",
        )
        return ProviderResult.failure(
            ProviderStatus.RATE_LIMITED, now, (attempt,), "Finnhub配额已用尽", retry_at
        )

    def _queue_provider_refresh_if_limited(
        self,
        task_type: str,
        instrument: InstrumentId,
        result: ProviderResult[T],
        now: datetime,
        *,
        mode: DecisionMode | None = None,
    ) -> ProviderResult[T]:
        if result.status in _RETRYABLE_PROVIDER_STATUSES and self.repository is not None:
            self.repository.enqueue_provider_refresh(
                task_type, instrument, self._next_retry_at(result, now, 0), mode=mode
            )
        return result

    @staticmethod
    def _next_retry_at(result: ProviderResult[object], now: datetime, attempts: int) -> datetime:
        if result.retry_at is not None and result.retry_at > now:
            return result.retry_at
        delay_minutes = min(5 * (2 ** max(attempts - 1, 0)), 60)
        return now + timedelta(minutes=delay_minutes)
