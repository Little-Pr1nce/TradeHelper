"""String enums shared by all V2 contracts."""

from enum import Enum


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Market(_StringEnum):
    A = "A"
    US = "US"


class Exchange(_StringEnum):
    XSHG = "XSHG"
    XSHE = "XSHE"
    XBSE = "XBSE"
    XNYS = "XNYS"
    XNAS = "XNAS"
    UNKNOWN = "UNKNOWN"


class DecisionMode(_StringEnum):
    PRE = "pre"
    INTRADAY = "intraday"
    EOD = "eod"


class TradingSession(_StringEnum):
    PRE = "pre"
    REGULAR = "regular"
    POST = "post"
    CLOSED = "closed"


class AdjustmentMode(_StringEnum):
    FRONT_ADJUSTED = "front_adjusted"


class FreshnessStatus(_StringEnum):
    FRESH = "fresh"
    STALE = "stale"
    FUTURE = "future"
    MISSING_TIMESTAMP = "missing_timestamp"
    NOT_REQUIRED = "not_required"


class ProviderStatus(_StringEnum):
    OK = "ok"
    EMPTY = "empty"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_PAYLOAD = "invalid_payload"


class QualitySeverity(_StringEnum):
    BLOCK = "block"
    WARNING = "warning"
    OPTIONAL_MISSING = "optional_missing"
    INFO = "info"


class QualityStatus(_StringEnum):
    OK = "ok"
    WATCH = "watch"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class QualityAction(_StringEnum):
    NORMAL = "normal"
    WATCH = "watch"
    REDUCE_POSITION = "reduce_position"
    BLOCK_NEW_ENTRIES = "block_new_entries"
