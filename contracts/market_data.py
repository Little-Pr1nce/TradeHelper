"""V2 不可变市场数据合同：统一双市场身份、时间、复权和哈希语义。"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping

from .enums import (
    AdjustmentMode,
    Exchange,
    FreshnessStatus,
    Market,
    QualityStatus,
    TradingSession,
)


class ContractViolation(ValueError):
    """Raised when a V2 contract invariant is violated."""


_US_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.\-^]{0,15}$")
_A_CODE = re.compile(r"^\d{6}$")
_UTC = timezone.utc


def ensure_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ContractViolation(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(_UTC)


def ensure_date(value: date, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ContractViolation(f"{field_name} must be a date")
    return value


def ensure_finite(value: float, field_name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractViolation(f"{field_name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise ContractViolation(f"{field_name} must be finite")
    if positive and number <= 0:
        raise ContractViolation(f"{field_name} must be positive")
    return number


def ensure_optional_positive(value: float | None, field_name: str) -> float | None:
    if value is None:
        return None
    return ensure_finite(value, field_name, positive=True)


def ensure_optional_probability(value: float | None, field_name: str) -> float | None:
    if value is None:
        return None
    number = ensure_finite(value, field_name)
    if not 0.0 <= number <= 1.0:
        raise ContractViolation(f"{field_name} must be between 0 and 1")
    return number


def utc_iso(value: datetime) -> str:
    normalized = ensure_utc(value, "datetime")
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [_canonical_value(item) for item in value]
    if is_dataclass(value):
        return {field.name: _canonical_value(getattr(value, field.name)) for field in fields(value)}
    return value


def canonical_json(value: Any) -> str:
    """Serialize contracts deterministically for persistence and hashing."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _coerce_market(value: Market | str) -> Market:
    try:
        return value if isinstance(value, Market) else Market(str(value).upper())
    except ValueError as exc:
        raise ContractViolation(f"unsupported market: {value}") from exc


def _coerce_exchange(value: Exchange | str) -> Exchange:
    try:
        return value if isinstance(value, Exchange) else Exchange(str(value).upper())
    except ValueError as exc:
        raise ContractViolation(f"unsupported exchange: {value}") from exc


@dataclass(frozen=True, slots=True)
class InstrumentId:
    code: str
    market: Market
    exchange: Exchange

    def __post_init__(self) -> None:
        market = _coerce_market(self.market)
        exchange = _coerce_exchange(self.exchange)
        code = str(self.code or "").strip().upper()
        if market is Market.A:
            if not _A_CODE.fullmatch(code):
                raise ContractViolation("A-share code must contain exactly six digits")
            expected = self._a_exchange_for(code)
            if exchange is not expected:
                raise ContractViolation(f"A-share {code} must use {expected.value}")
        else:
            if _A_CODE.fullmatch(code) or not _US_TICKER.fullmatch(code):
                raise ContractViolation("US ticker format is invalid")
            if exchange in {Exchange.XSHG, Exchange.XSHE, Exchange.XBSE}:
                raise ContractViolation("US ticker cannot use an A-share exchange")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "code", code)

    @staticmethod
    def _a_exchange_for(code: str) -> Exchange:
        if code.startswith(("6", "5", "9")):
            return Exchange.XSHG
        if code.startswith(("4", "8")):
            return Exchange.XBSE
        return Exchange.XSHE

    @classmethod
    def from_code(
        cls,
        code: str,
        market: Market | str,
        exchange: Exchange | str | None = None,
    ) -> "InstrumentId":
        normalized_market = _coerce_market(market)
        normalized_code = str(code or "").strip().upper()
        if normalized_market is Market.A:
            expected = cls._a_exchange_for(normalized_code)
            return cls(normalized_code, normalized_market, expected)
        selected = Exchange.UNKNOWN if exchange is None else _coerce_exchange(exchange)
        return cls(normalized_code, normalized_market, selected)

    @property
    def stable_key(self) -> str:
        return f"{self.market.value}:{self.exchange.value}:{self.code}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "market": self.market.value, "exchange": self.exchange.value}


@dataclass(frozen=True, slots=True)
class StockMetadata:
    instrument: InstrumentId
    name: str
    industry: str | None
    description: str | None
    listing_date: date | None
    source: str
    fetched_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ContractViolation("stock name cannot be empty")
        if not str(self.source or "").strip():
            raise ContractViolation("metadata source cannot be empty")
        if self.listing_date is not None:
            ensure_date(self.listing_date, "listing_date")
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "fetched_at", ensure_utc(self.fetched_at, "fetched_at"))


@dataclass(frozen=True, slots=True)
class CanonicalBar:
    instrument: InstrumentId
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    adjustment_mode: AdjustmentMode
    source: str
    fetched_at: datetime
    corporate_action_version: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        open_price = ensure_finite(self.open, "open", positive=True)
        high = ensure_finite(self.high, "high", positive=True)
        low = ensure_finite(self.low, "low", positive=True)
        close = ensure_finite(self.close, "close", positive=True)
        epsilon = max(open_price, high, low, close) * 1e-6
        if high + epsilon < max(open_price, close) or low - epsilon > min(open_price, close):
            raise ContractViolation("INVALID_OHLC")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ContractViolation("volume must be a non-negative integer in shares")
        try:
            adjustment_mode = (
                self.adjustment_mode
                if isinstance(self.adjustment_mode, AdjustmentMode)
                else AdjustmentMode(str(self.adjustment_mode))
            )
        except ValueError as exc:
            raise ContractViolation("UNSUPPORTED_ADJUSTMENT_MODE") from exc
        if adjustment_mode is not AdjustmentMode.FRONT_ADJUSTED:
            raise ContractViolation("UNSUPPORTED_ADJUSTMENT_MODE")
        if not str(self.source or "").strip():
            raise ContractViolation("bar source cannot be empty")
        object.__setattr__(self, "trading_date", ensure_date(self.trading_date, "trading_date"))
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "adjustment_mode", adjustment_mode)
        object.__setattr__(self, "fetched_at", ensure_utc(self.fetched_at, "fetched_at"))

    @property
    def stable_key(self) -> str:
        return f"{self.instrument.stable_key}:{self.trading_date.isoformat()}:{self.adjustment_mode.value}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument.to_dict(),
            "trading_date": self.trading_date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "adjustment_mode": self.adjustment_mode.value,
            "source": self.source,
            "fetched_at": utc_iso(self.fetched_at),
            "corporate_action_version": self.corporate_action_version,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    instrument: InstrumentId
    session: TradingSession
    price: float
    prev_close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: int | None
    bid: float | None
    ask: float | None
    observed_at: datetime
    fetched_at: datetime
    source: str
    freshness_status: FreshnessStatus
    schema_version: int = 1

    def __post_init__(self) -> None:
        price = ensure_finite(self.price, "price", positive=True)
        prev_close = ensure_optional_positive(self.prev_close, "prev_close")
        open_price = ensure_optional_positive(self.open, "open")
        high = ensure_optional_positive(self.high, "high")
        low = ensure_optional_positive(self.low, "low")
        bid = ensure_optional_positive(self.bid, "bid")
        ask = ensure_optional_positive(self.ask, "ask")
        if self.volume is not None and (
            isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0
        ):
            raise ContractViolation("quote volume must be a non-negative integer or None")
        if bid is not None and ask is not None and bid > ask:
            raise ContractViolation("bid cannot exceed ask")
        ohlc = (open_price, high, low)
        if all(value is not None for value in ohlc):
            epsilon = max(price, *(value for value in ohlc if value is not None)) * 1e-6
            if high + epsilon < max(open_price, price) or low - epsilon > min(open_price, price):
                raise ContractViolation("INVALID_OHLC")
        try:
            session = self.session if isinstance(self.session, TradingSession) else TradingSession(str(self.session))
            freshness = (
                self.freshness_status
                if isinstance(self.freshness_status, FreshnessStatus)
                else FreshnessStatus(str(self.freshness_status))
            )
        except ValueError as exc:
            raise ContractViolation("unsupported quote session or freshness status") from exc
        if not str(self.source or "").strip():
            raise ContractViolation("quote source cannot be empty")
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "freshness_status", freshness)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "prev_close", prev_close)
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "fetched_at", ensure_utc(self.fetched_at, "fetched_at"))

    @property
    def available_fields(self) -> frozenset[str]:
        values = {
            "price": self.price,
            "prev_close": self.prev_close,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "volume": self.volume,
            "bid": self.bid,
            "ask": self.ask,
        }
        return frozenset(name for name, value in values.items() if value is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument.to_dict(),
            "session": self.session.value,
            "price": self.price,
            "prev_close": self.prev_close,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "volume": self.volume,
            "bid": self.bid,
            "ask": self.ask,
            "observed_at": utc_iso(self.observed_at),
            "fetched_at": utc_iso(self.fetched_at),
            "source": self.source,
            "freshness_status": self.freshness_status.value,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class IntradayBar:
    instrument: InstrumentId
    observed_at: datetime
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int | None
    source: str
    evidence_quality: str
    fetched_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        open_price = ensure_finite(self.open, "open", positive=True)
        high = ensure_finite(self.high, "high", positive=True)
        low = ensure_finite(self.low, "low", positive=True)
        close = ensure_finite(self.close, "close", positive=True)
        epsilon = max(open_price, high, low, close) * 1e-6
        if high + epsilon < max(open_price, close) or low - epsilon > min(open_price, close):
            raise ContractViolation("INVALID_OHLC")
        if self.volume is not None and (
            isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0
        ):
            raise ContractViolation("intraday volume must be a non-negative integer or None")
        if self.evidence_quality not in {"provider", "supplemental", "unknown"}:
            raise ContractViolation("unsupported intraday evidence quality")
        object.__setattr__(self, "session_date", ensure_date(self.session_date, "session_date"))
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "fetched_at", ensure_utc(self.fetched_at, "fetched_at"))
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)


@dataclass(frozen=True, slots=True)
class NewsSnapshot:
    instrument: InstrumentId
    title: str
    source: str
    published_at: datetime
    available_at: datetime
    fetched_at: datetime
    content: str | None
    is_macro: bool
    finbert_label: str | None
    finbert_score: float | None
    relevance: float | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not str(self.title or "").strip() or not str(self.source or "").strip():
            raise ContractViolation("news title and source cannot be empty")
        published_at = ensure_utc(self.published_at, "published_at")
        available_at = ensure_utc(self.available_at, "available_at")
        if available_at < published_at:
            raise ContractViolation("news available_at cannot precede published_at")
        if self.finbert_label not in {None, "positive", "neutral", "negative"}:
            raise ContractViolation("unsupported finbert label")
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "fetched_at", ensure_utc(self.fetched_at, "fetched_at"))
        object.__setattr__(self, "finbert_score", ensure_optional_probability(self.finbert_score, "finbert_score"))
        object.__setattr__(self, "relevance", ensure_optional_probability(self.relevance, "relevance"))

    @property
    def stable_key(self) -> str:
        normalized_title = " ".join(self.title.lower().split())
        return f"{self.instrument.stable_key}:{utc_iso(self.published_at)}:{self.source}:{normalized_title}"


@dataclass(frozen=True, slots=True)
class FundamentalValue:
    value: float | str | None
    unit: str | None
    period_end: date | None
    published_at: datetime | None
    source: str

    def __post_init__(self) -> None:
        if not str(self.source or "").strip():
            raise ContractViolation("fundamental value source cannot be empty")
        if isinstance(self.value, float):
            object.__setattr__(self, "value", ensure_finite(self.value, "fundamental value"))
        if self.period_end is not None:
            object.__setattr__(self, "period_end", ensure_date(self.period_end, "period_end"))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", ensure_utc(self.published_at, "published_at"))


@dataclass(frozen=True, slots=True)
class FundamentalSnapshot:
    instrument: InstrumentId
    fields: Mapping[str, FundamentalValue]
    available_at: datetime
    fetched_at: datetime
    provider: str
    quality_status: QualityStatus
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not str(self.provider or "").strip():
            raise ContractViolation("fundamental provider cannot be empty")
        normalized = {str(key): value for key, value in self.fields.items()}
        if any(not key or not isinstance(value, FundamentalValue) for key, value in normalized.items()):
            raise ContractViolation("fundamental fields must map names to FundamentalValue")
        try:
            quality_status = (
                self.quality_status
                if isinstance(self.quality_status, QualityStatus)
                else QualityStatus(str(self.quality_status))
            )
        except ValueError as exc:
            raise ContractViolation("unsupported fundamental quality status") from exc
        object.__setattr__(self, "fields", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "available_at", ensure_utc(self.available_at, "available_at"))
        object.__setattr__(self, "fetched_at", ensure_utc(self.fetched_at, "fetched_at"))
        object.__setattr__(self, "quality_status", quality_status)
