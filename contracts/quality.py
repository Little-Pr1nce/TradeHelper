"""独立于交易决策的数据质量合同；质量降级必须可解释、可审计。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .enums import QualityAction, QualitySeverity, QualityStatus
from .market_data import ContractViolation, ensure_finite, ensure_utc


@dataclass(frozen=True, slots=True)
class DataQualityIssue:
    code: str
    severity: QualitySeverity
    field: str | None
    message: str
    source: str | None

    def __post_init__(self) -> None:
        if not str(self.code or "").strip() or not str(self.message or "").strip():
            raise ContractViolation("quality issue code and message cannot be empty")
        severity = self.severity if isinstance(self.severity, QualitySeverity) else QualitySeverity(str(self.severity))
        object.__setattr__(self, "severity", severity)

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        return (self.code, self.field or "", self.source or "")


@dataclass(frozen=True, slots=True)
class DataCapabilities:
    daily_price: bool = False
    short_technical_20: bool = False
    medium_technical_60: bool = False
    ma120: bool = False
    realtime_price: bool = False
    intraday_ohlc: bool = False
    volume: bool = False
    bid_ask: bool = False
    news: bool = False
    fundamentals: bool = False


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    status: QualityStatus
    action: QualityAction
    score: float
    max_position_multiplier: float
    block_new_entries: bool
    issues: tuple[DataQualityIssue, ...]
    capabilities: DataCapabilities
    evaluated_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        status = self.status if isinstance(self.status, QualityStatus) else QualityStatus(str(self.status))
        action = self.action if isinstance(self.action, QualityAction) else QualityAction(str(self.action))
        score = ensure_finite(self.score, "quality score")
        multiplier = ensure_finite(self.max_position_multiplier, "max_position_multiplier")
        if not 0.0 <= score <= 100.0 or not 0.0 <= multiplier <= 1.0:
            raise ContractViolation("quality score and multiplier are out of range")
        deduped = {issue.dedupe_key: issue for issue in self.issues}
        ordered = tuple(sorted(deduped.values(), key=lambda issue: issue.dedupe_key))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "max_position_multiplier", multiplier)
        object.__setattr__(self, "issues", ordered)
        object.__setattr__(self, "evaluated_at", ensure_utc(self.evaluated_at, "evaluated_at"))
