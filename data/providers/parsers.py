"""Pure parsers for de-identified provider payloads.

No parser calls a network service.  This keeps source-shape regressions testable
without credentials and prevents raw dictionaries from leaking past the adapter.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from contracts.enums import AdjustmentMode, FreshnessStatus, QualityStatus, TradingSession
from contracts.market_data import (
    CanonicalBar,
    ContractViolation,
    FundamentalSnapshot,
    FundamentalValue,
    InstrumentId,
    NewsSnapshot,
    QuoteSnapshot,
    StockMetadata,
    ensure_utc,
)


def _value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, "", "--", "N/A", "null"):
            return value
    return None


def _required_float(row: Mapping[str, Any], *names: str) -> float:
    value = _value(row, *names)
    if value is None:
        raise ContractViolation(f"MISSING_REQUIRED_COLUMN:{names[0]}")
    return _as_float(value)


def _optional_float(row: Mapping[str, Any], *names: str) -> float | None:
    value = _value(row, *names)
    return None if value is None else _as_float(value)


def _optional_int(row: Mapping[str, Any], *names: str) -> int | None:
    value = _value(row, *names)
    return None if value is None else int(_as_float(value))


def _as_float(value: Any) -> float:
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip()
    return float(value)


def _date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if "/" in text:
        return datetime.strptime(text, "%m/%d/%Y").date()
    return date.fromisoformat(text[:10])


def _try_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / (1000 if float(value) > 10_000_000_000 else 1), tz=timezone.utc)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        cleaned = text.replace("Closed at ", "").replace(" ET", "").strip()
        try:
            parsed = datetime.strptime(cleaned, "%b %d, %Y %I:%M %p").replace(
                tzinfo=ZoneInfo("America/New_York")
            )
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _datetime(value: Any, default: datetime) -> datetime:
    return _try_datetime(value) or ensure_utc(default, "default timestamp")


def parse_tickflow_bars(
    payload: Iterable[Mapping[str, Any]], instrument: InstrumentId, fetched_at: datetime
) -> tuple[CanonicalBar, ...]:
    """TickFlow daily bars, converting A-share lots from hands to shares."""
    bars: list[CanonicalBar] = []
    for row in payload:
        raw_volume = _required_float(row, "volume", "vol")
        volume = int(raw_volume * 100) if instrument.market.value == "A" else int(raw_volume)
        bars.append(
            CanonicalBar(
                instrument=instrument,
                # TickFlow's dataframe can include a display-only ``date`` column
                # containing the day-of-month (for example ``"29"``).  The
                # provider's full exchange date is ``trade_date`` and must win.
                trading_date=_date(_value(row, "trade_date", "datetime", "time", "date")),
                open=_required_float(row, "open", "o"),
                high=_required_float(row, "high", "h"),
                low=_required_float(row, "low", "l"),
                close=_required_float(row, "close", "c"),
                volume=volume,
                adjustment_mode=AdjustmentMode.FRONT_ADJUSTED,
                source="tickflow",
                fetched_at=fetched_at,
                corporate_action_version=_value(row, "corporate_action_version", "adjustment_version"),
            )
        )
    return tuple(sorted(bars, key=lambda bar: bar.trading_date))


def parse_yfinance_bars(
    payload: Iterable[Mapping[str, Any]], instrument: InstrumentId, fetched_at: datetime
) -> tuple[CanonicalBar, ...]:
    return tuple(
        CanonicalBar(
            instrument=instrument,
            trading_date=_date(_value(row, "date", "Date", "Datetime")),
            open=_required_float(row, "open", "Open"),
            high=_required_float(row, "high", "High"),
            low=_required_float(row, "low", "Low"),
            close=_required_float(row, "close", "Close"),
            volume=int(_required_float(row, "volume", "Volume")),
            adjustment_mode=AdjustmentMode.FRONT_ADJUSTED,
            source="yfinance",
            fetched_at=fetched_at,
        )
        for row in payload
    )


def parse_tickflow_quote(
    payload: Mapping[str, Any], instrument: InstrumentId, session: TradingSession, fetched_at: datetime
) -> QuoteSnapshot:
    raw_observed = _value(payload, "timestamp", "observed_at", "time")
    parsed_observed = _try_datetime(raw_observed)
    return QuoteSnapshot(
        instrument=instrument,
        session=session,
        price=_required_float(payload, "last_price", "latest", "price", "last"),
        prev_close=_optional_float(payload, "prev_close", "previousClose"),
        open=_optional_float(payload, "open"), high=_optional_float(payload, "high"), low=_optional_float(payload, "low"),
        volume=_optional_int(payload, "volume"), bid=_optional_float(payload, "bid"), ask=_optional_float(payload, "ask"),
        observed_at=parsed_observed or fetched_at, fetched_at=fetched_at, source="tickflow",
        freshness_status=FreshnessStatus.NOT_REQUIRED if parsed_observed is not None else FreshnessStatus.MISSING_TIMESTAMP,
    )


def parse_nasdaq_quote(payload: Mapping[str, Any], instrument: InstrumentId, session: TradingSession, fetched_at: datetime) -> QuoteSnapshot:
    root = payload.get("data", payload)
    if not isinstance(root, Mapping):
        raise ContractViolation("Nasdaq payload data must be a mapping")
    primary = root.get("primaryData") if isinstance(root.get("primaryData"), Mapping) else root
    secondary = root.get("secondaryData") if isinstance(root.get("secondaryData"), Mapping) else {}
    raw_price = _value(primary, "lastSalePrice", "price", "last")
    if isinstance(raw_price, str):
        raw_price = raw_price.replace("$", "").replace(",", "").strip()
    observed = _try_datetime(_value(primary, "lastTradeTimestamp", "timestamp", "observed_at"))
    previous_close = _optional_float(primary, "previousClose", "prev_close")
    if previous_close is None:
        previous_close = _optional_float(secondary, "lastSalePrice", "previousClose", "prev_close")
    return QuoteSnapshot(
        instrument=instrument, session=session, price=float(raw_price), prev_close=previous_close,
        open=None, high=None, low=None, volume=_optional_int(primary, "volume"),
        bid=_optional_float(primary, "bidPrice", "bid"), ask=_optional_float(primary, "askPrice", "ask"),
        observed_at=observed or fetched_at, fetched_at=fetched_at, source="nasdaq",
        freshness_status=FreshnessStatus.NOT_REQUIRED if observed is not None else FreshnessStatus.MISSING_TIMESTAMP,
    )


def parse_nasdaq_bars(
    payload: Mapping[str, Any], instrument: InstrumentId, fetched_at: datetime
) -> tuple[CanonicalBar, ...]:
    """Parse Nasdaq's broker-aligned historical OHLCV response.

    Nasdaq exposes no adjustment argument on this endpoint.  V2 accepts it as
    the validated US canonical series and still records the source on every bar;
    source reconciliation remains responsible for detecting later revisions.
    """
    data = payload.get("data")
    table = data.get("tradesTable") if isinstance(data, Mapping) else None
    rows = table.get("rows") if isinstance(table, Mapping) else None
    if not isinstance(rows, list):
        raise ContractViolation("Nasdaq historical payload has no tradesTable rows")
    bars = tuple(
        CanonicalBar(
            instrument=instrument,
            trading_date=_date(_value(row, "date")),
            open=_required_float(row, "open"),
            high=_required_float(row, "high"),
            low=_required_float(row, "low"),
            close=_required_float(row, "close"),
            volume=int(_required_float(row, "volume")),
            adjustment_mode=AdjustmentMode.FRONT_ADJUSTED,
            source="nasdaq",
            fetched_at=fetched_at,
            corporate_action_version="nasdaq_historical",
        )
        for row in rows
    )
    return tuple(sorted(bars, key=lambda bar: bar.trading_date))


def parse_yfinance_quote(payload: Mapping[str, Any], instrument: InstrumentId, session: TradingSession, fetched_at: datetime) -> QuoteSnapshot:
    observed = _try_datetime(_value(payload, "regularMarketTime", "postMarketTime", "preMarketTime", "timestamp"))
    return QuoteSnapshot(
        instrument=instrument, session=session,
        price=_required_float(payload, "regularMarketPrice", "postMarketPrice", "preMarketPrice", "price"),
        prev_close=_optional_float(payload, "regularMarketPreviousClose", "previousClose", "prev_close"),
        open=None, high=None, low=None, volume=None, bid=None, ask=None,
        observed_at=observed or fetched_at,
        fetched_at=fetched_at, source="yfinance",
        freshness_status=FreshnessStatus.NOT_REQUIRED if observed is not None else FreshnessStatus.MISSING_TIMESTAMP,
    )


def parse_tickflow_metadata(payload: Mapping[str, Any], instrument: InstrumentId, fetched_at: datetime) -> StockMetadata:
    return StockMetadata(
        instrument=instrument, name=str(_value(payload, "name", "shortName", "display_name") or instrument.code),
        industry=_value(payload, "industry"), description=_value(payload, "description"), listing_date=None,
        source="tickflow", fetched_at=fetched_at,
    )


def parse_finnhub_metadata(payload: Mapping[str, Any], instrument: InstrumentId, fetched_at: datetime) -> StockMetadata:
    raw_listing = _value(payload, "ipo", "listing_date")
    return StockMetadata(
        instrument=instrument, name=str(_value(payload, "name", "ticker") or instrument.code),
        industry=_value(payload, "finnhubIndustry", "industry"), description=_value(payload, "description"),
        listing_date=_date(raw_listing) if raw_listing else None, source="finnhub", fetched_at=fetched_at,
    )


def parse_baostock_listing_date(payload: Mapping[str, Any], instrument: InstrumentId) -> date | None:
    raw = _value(payload, "ipoDate", "ipo_date", "listDate", "listing_date")
    return _date(raw) if raw else None


def parse_finnhub_fundamentals(
    payload: Mapping[str, Any], instrument: InstrumentId, fetched_at: datetime, *, provider: str = "finnhub"
) -> FundamentalSnapshot:
    # Finnhub /stock/metric returns the numeric facts under ``metric``;
    # a few normalizers flatten that map before reaching this adapter.
    structured_fields = payload.get("fields") if isinstance(payload.get("fields"), Mapping) else None
    raw_fields = payload.get("metric") if isinstance(payload.get("metric"), Mapping) else payload
    values: dict[str, FundamentalValue] = {}
    for name, raw in (structured_fields or raw_fields).items():
        if raw is None:
            continue
        if isinstance(raw, Mapping):
            value = raw.get("value")
            if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
                continue
            period_end = _date(raw["period_end"]) if raw.get("period_end") else None
            published_at = _try_datetime(raw.get("published_at"))
            values[str(name)] = FundamentalValue(
                float(value) if isinstance(value, (int, float)) else value,
                str(raw["unit"]) if raw.get("unit") else None,
                period_end,
                published_at,
                str(raw.get("source") or provider),
            )
        elif isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
            values[str(name)] = FundamentalValue(
                float(raw) if isinstance(raw, (int, float)) else raw, None, None, None, provider
            )
    if not values:
        raise ContractViolation("Finnhub fundamentals payload has no supported metric fields")
    return FundamentalSnapshot(instrument, values, fetched_at, fetched_at, provider, QualityStatus.OK)


def parse_finnhub_news(payload: Iterable[Mapping[str, Any]], instrument: InstrumentId, fetched_at: datetime) -> tuple[NewsSnapshot, ...]:
    news: list[NewsSnapshot] = []
    for row in payload:
        title = _value(row, "headline", "title")
        if title is None or not str(title).strip():
            continue
        published = _datetime(_value(row, "datetime", "published_at"), fetched_at)
        news.append(
            NewsSnapshot(
                instrument=instrument, title=str(title).strip(), source=str(_value(row, "source") or "finnhub"),
                published_at=published, available_at=max(published, ensure_utc(fetched_at, "fetched_at")), fetched_at=fetched_at,
                content=_value(row, "summary", "content"), is_macro=False, finbert_label=None, finbert_score=None, relevance=None,
            )
        )
    return tuple(news)
