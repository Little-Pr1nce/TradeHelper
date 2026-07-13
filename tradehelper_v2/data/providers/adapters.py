"""Provider-specific typed adapters.

The callers are injected transports so adapters can be exercised with captured,
de-identified payloads.  Network credential composition is intentionally kept
out of this module and will be supplied by the application composition root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable, Mapping

from tradehelper_v2.contracts.enums import ProviderStatus, TradingSession
from tradehelper_v2.contracts.market_data import (
    CanonicalBar,
    FundamentalSnapshot,
    InstrumentId,
    NewsSnapshot,
    QuoteSnapshot,
    StockMetadata,
)
from tradehelper_v2.contracts.providers import ProviderResult, QuoteBatch
from .base import RetryingClient
from .parsers import (
    parse_baostock_listing_date,
    parse_finnhub_fundamentals,
    parse_finnhub_metadata,
    parse_finnhub_news,
    parse_nasdaq_bars,
    parse_nasdaq_quote,
    parse_tickflow_bars,
    parse_tickflow_metadata,
    parse_tickflow_quote,
    parse_yfinance_bars,
    parse_yfinance_quote,
)


RawCall = Callable[..., Any]


def _status(error: Exception) -> ProviderStatus:
    message = str(error).lower()
    if "429" in message or "rate" in message or "limit" in message:
        return ProviderStatus.RATE_LIMITED
    if "timeout" in message or "timed out" in message:
        return ProviderStatus.TIMEOUT
    return ProviderStatus.UNAVAILABLE


def _result(name: str, now: Callable[[], datetime], invoke: Callable[[], Any], parse: Callable[[Any], Any]) -> ProviderResult[Any]:
    """Retry transport failures only; a bad payload is never retried as a network fault."""
    raw = RetryingClient(name, invoke, now, _status).fetch()
    if raw.status is not ProviderStatus.OK:
        return raw
    if raw.value is None or raw.value == [] or raw.value == {}:
        return ProviderResult.failure(ProviderStatus.EMPTY, raw.fetched_at, raw.attempts)
    try:
        value = parse(raw.value)
    except (TypeError, ValueError, KeyError) as exc:
        return ProviderResult.failure(ProviderStatus.INVALID_PAYLOAD, raw.fetched_at, raw.attempts)
    if value == ():
        return ProviderResult.failure(ProviderStatus.EMPTY, raw.fetched_at, raw.attempts)
    return ProviderResult.success(value, name, raw.fetched_at, raw.attempts)


@dataclass(frozen=True, slots=True)
class TickFlowAdapter:
    daily_call: RawCall
    quote_call: RawCall
    metadata_call: RawCall
    now: Callable[[], datetime]
    name: str = "tickflow"

    @staticmethod
    def symbol(instrument: InstrumentId) -> str:
        if instrument.market.value == "US":
            return f"{instrument.code}.US"
        suffix = "SH" if instrument.exchange.value == "XSHG" else "BJ" if instrument.exchange.value == "XBSE" else "SZ"
        return f"{instrument.code}.{suffix}"

    def daily(self, instrument: InstrumentId, start: date, end: date) -> ProviderResult[tuple[CanonicalBar, ...]]:
        return _result(self.name, self.now, lambda: self.daily_call(self.symbol(instrument), start, end), lambda raw: parse_tickflow_bars(raw, instrument, self.now()))

    def metadata(self, instrument: InstrumentId) -> ProviderResult[StockMetadata]:
        return _result(self.name, self.now, lambda: self.metadata_call(self.symbol(instrument)), lambda raw: parse_tickflow_metadata(raw, instrument, self.now()))

    def quotes(self, instruments: tuple[InstrumentId, ...], session: TradingSession) -> QuoteBatch:
        """Honor TickFlow's 5-symbol request and 10-request round limits."""
        quotes: dict[InstrumentId, QuoteSnapshot] = {}
        failures: dict[InstrumentId, ProviderStatus] = {}
        for start in range(0, min(len(instruments), 50), 5):
            batch = instruments[start:start + 5]
            raw_result = _result(self.name, self.now, lambda batch=batch: self.quote_call([self.symbol(item) for item in batch]), lambda raw: raw)
            if raw_result.status is not ProviderStatus.OK or raw_result.value is None:
                failures.update({item: raw_result.status for item in batch})
                continue
            raw_quotes = tuple(raw_result.value)
            for index, instrument in enumerate(batch):
                if index >= len(raw_quotes):
                    failures[instrument] = ProviderStatus.EMPTY
                    continue
                try:
                    quotes[instrument] = parse_tickflow_quote(raw_quotes[index], instrument, session, self.now())
                except Exception:
                    failures[instrument] = ProviderStatus.INVALID_PAYLOAD
        for instrument in instruments[50:]:
            failures[instrument] = ProviderStatus.RATE_LIMITED
        return QuoteBatch(quotes, failures)

    def quote(self, instrument: InstrumentId, session: TradingSession) -> ProviderResult[QuoteSnapshot]:
        batch = self.quotes((instrument,), session)
        if instrument in batch.quotes:
            return ProviderResult.success(batch.quotes[instrument], self.name, self.now())
        return ProviderResult.failure(batch.failures[instrument], self.now())


@dataclass(frozen=True, slots=True)
class NasdaqAdapter:
    quote_call: RawCall
    now: Callable[[], datetime]
    daily_call: RawCall | None = None
    name: str = "nasdaq"

    def quote(self, instrument: InstrumentId, session: TradingSession) -> ProviderResult[QuoteSnapshot]:
        return _result(self.name, self.now, lambda: self.quote_call(instrument.code), lambda raw: parse_nasdaq_quote(raw, instrument, session, self.now()))

    def daily(self, instrument: InstrumentId, start: date, end: date) -> ProviderResult[tuple[CanonicalBar, ...]]:
        if self.daily_call is None:
            return ProviderResult.failure(ProviderStatus.UNAVAILABLE, self.now())
        return _result(
            self.name,
            self.now,
            lambda: self.daily_call(instrument.code, start, end),
            lambda raw: parse_nasdaq_bars(raw, instrument, self.now()),
        )


@dataclass(frozen=True, slots=True)
class YFinanceAdapter:
    daily_call: RawCall
    quote_call: RawCall
    now: Callable[[], datetime]
    name: str = "yfinance"
    fundamentals_call: RawCall | None = None

    def daily(self, instrument: InstrumentId, start: date, end: date) -> ProviderResult[tuple[CanonicalBar, ...]]:
        return _result(self.name, self.now, lambda: self.daily_call(instrument.code, start, end), lambda raw: parse_yfinance_bars(raw, instrument, self.now()))

    def quote(self, instrument: InstrumentId, session: TradingSession) -> ProviderResult[QuoteSnapshot]:
        return _result(self.name, self.now, lambda: self.quote_call(instrument.code), lambda raw: parse_yfinance_quote(raw, instrument, session, self.now()))

    def fundamentals(self, instrument: InstrumentId) -> ProviderResult[FundamentalSnapshot]:
        if self.fundamentals_call is None:
            return ProviderResult.failure(ProviderStatus.UNAVAILABLE, self.now())
        return _result(
            self.name, self.now, lambda: self.fundamentals_call(instrument.code),
            lambda raw: parse_finnhub_fundamentals(raw, instrument, self.now(), provider=self.name),
        )


@dataclass(frozen=True, slots=True)
class FinnhubAdapter:
    profile_call: RawCall
    fundamentals_call: RawCall
    news_call: RawCall
    now: Callable[[], datetime]
    name: str = "finnhub"

    def metadata(self, instrument: InstrumentId) -> ProviderResult[StockMetadata]:
        return _result(self.name, self.now, lambda: self.profile_call(instrument.code), lambda raw: parse_finnhub_metadata(raw, instrument, self.now()))

    def listing_date(self, instrument: InstrumentId) -> ProviderResult[date | None]:
        return _result(self.name, self.now, lambda: self.profile_call(instrument.code), lambda raw: parse_finnhub_metadata(raw, instrument, self.now()).listing_date)

    def fundamentals(self, instrument: InstrumentId) -> ProviderResult[FundamentalSnapshot]:
        return _result(self.name, self.now, lambda: self.fundamentals_call(instrument.code), lambda raw: parse_finnhub_fundamentals(raw, instrument, self.now()))

    def news(self, instrument: InstrumentId) -> ProviderResult[tuple[NewsSnapshot, ...]]:
        return _result(self.name, self.now, lambda: self.news_call(instrument.code), lambda raw: parse_finnhub_news(raw, instrument, self.now()))


@dataclass(frozen=True, slots=True)
class BaostockAdapter:
    metadata_call: RawCall
    listing_call: RawCall
    fundamentals_call: RawCall
    now: Callable[[], datetime]
    name: str = "baostock"

    def metadata(self, instrument: InstrumentId) -> ProviderResult[StockMetadata]:
        return _result(self.name, self.now, lambda: self.metadata_call(instrument.code), lambda raw: StockMetadata(
            instrument=instrument,
            name=str(raw.get("code_name") or raw.get("name") or instrument.code),
            industry=raw.get("industry"), description=None,
            listing_date=parse_baostock_listing_date(raw, instrument),
            source=self.name, fetched_at=self.now(),
        ))

    def listing_date(self, instrument: InstrumentId) -> ProviderResult[date | None]:
        return _result(self.name, self.now, lambda: self.listing_call(instrument.code), lambda raw: parse_baostock_listing_date(raw, instrument))

    def fundamentals(self, instrument: InstrumentId) -> ProviderResult[FundamentalSnapshot]:
        # baostock returns a field mapping after the transport normalizes rows.
        return _result(
            self.name, self.now, lambda: self.fundamentals_call(instrument.code),
            lambda raw: parse_finnhub_fundamentals(raw, instrument, self.now(), provider=self.name),
        )


@dataclass(frozen=True, slots=True)
class AkshareAdapter:
    fundamentals_call: RawCall
    news_call: RawCall
    now: Callable[[], datetime]
    name: str = "akshare"

    def fundamentals(self, instrument: InstrumentId) -> ProviderResult[FundamentalSnapshot]:
        return _result(
            self.name, self.now, lambda: self.fundamentals_call(instrument.code),
            lambda raw: parse_finnhub_fundamentals(raw, instrument, self.now(), provider=self.name),
        )

    def news(self, instrument: InstrumentId) -> ProviderResult[tuple[NewsSnapshot, ...]]:
        return _result(self.name, self.now, lambda: self.news_call(instrument.code), lambda raw: parse_finnhub_news(raw, instrument, self.now()))


@dataclass(frozen=True, slots=True)
class FundamentalAdapter:
    fundamentals_call: RawCall
    now: Callable[[], datetime]
    name: str

    def fundamentals(self, instrument: InstrumentId) -> ProviderResult[FundamentalSnapshot]:
        return _result(
            self.name, self.now, lambda: self.fundamentals_call(instrument.code),
            lambda raw: parse_finnhub_fundamentals(raw, instrument, self.now(), provider=self.name),
        )
