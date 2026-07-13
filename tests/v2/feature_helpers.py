from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tradehelper_v2.contracts import (
    AdjustmentMode, CanonicalBar, DataCapabilities, DataQualityReport, DecisionMode,
    FeatureEvidenceMode, FeatureInputs, Market, QualityAction, QualityStatus,
)
from tradehelper_v2.data.calendar import StaticTradingCalendar


def bars(instrument, count: int, *, start: date = date(2025, 1, 1), close_start: float = 100.0, volume: int = 1000, fetched_at: datetime) -> tuple[CanonicalBar, ...]:
    return tuple(CanonicalBar(
        instrument, start + timedelta(days=index), close_start + index - 1.0,
        close_start + index + 2.0, close_start + index - 2.0, close_start + index,
        volume + index * 3, AdjustmentMode.FRONT_ADJUSTED, "fixture", fetched_at,
    ) for index in range(count))


def quality(now: datetime) -> DataQualityReport:
    return DataQualityReport(QualityStatus.OK, QualityAction.NORMAL, 100.0, 1.0, False, (), DataCapabilities(daily_price=True), now)


def inputs(instrument, now: datetime, values: tuple[CanonicalBar, ...], **overrides) -> FeatureInputs:
    payload = dict(
        instrument=instrument, mode=DecisionMode.EOD, cutoff_at=now, bars=values, quote=None,
        news=(), news_status="empty", fundamentals=None, fundamentals_status="empty",
        data_quality=quality(now), evidence_mode=FeatureEvidenceMode.RECONSTRUCTED_HISTORY,
    )
    payload.update(overrides)
    return FeatureInputs(**payload)


def calendar(values: tuple[CanonicalBar, ...]) -> StaticTradingCalendar:
    sessions = tuple(bar.trading_date for bar in values)
    return StaticTradingCalendar(sessions=sessions, completed_sessions=sessions)
