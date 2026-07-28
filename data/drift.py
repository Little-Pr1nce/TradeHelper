"""Auditable comparison of completed daily bars from independent providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from contracts.market_data import CanonicalBar, ContractViolation, ensure_utc

from .repository import DailyBarDriftRecord, SQLiteRepository


@dataclass(frozen=True, slots=True)
class DailyBarDriftPolicy:
    """Explicit comparison tolerances, separate from source-routing policy."""

    max_abs_price_diff: float = 0.01
    max_volume_ratio_deviation: float = 0.02

    def __post_init__(self) -> None:
        if self.max_abs_price_diff < 0 or self.max_volume_ratio_deviation < 0:
            raise ValueError("daily-bar drift tolerances must be non-negative")


class DailyBarDriftMonitor:
    """Persist source disagreements without replacing canonical daily bars."""

    def __init__(self, repository: SQLiteRepository, policy: DailyBarDriftPolicy | None = None) -> None:
        self.repository = repository
        self.policy = policy or DailyBarDriftPolicy()

    def compare(
        self,
        primary: Iterable[CanonicalBar],
        comparator: Iterable[CanonicalBar],
        observed_at: datetime,
    ) -> tuple[DailyBarDriftRecord, ...]:
        """Compare overlapping sessions only; missing rows are not silently called a match."""
        now = ensure_utc(observed_at, "observed_at")
        primary_bars = tuple(primary)
        comparator_bars = tuple(comparator)
        primary_by_date = {bar.trading_date: bar for bar in primary_bars}
        comparator_by_date = {bar.trading_date: bar for bar in comparator_bars}
        if len(primary_by_date) != len(primary_bars) or len(comparator_by_date) != len(comparator_bars):
            raise ContractViolation("daily-bar drift comparison cannot contain duplicate trading dates")
        records: list[DailyBarDriftRecord] = []
        for trading_date in sorted(set(primary_by_date).intersection(comparator_by_date)):
            left, right = primary_by_date[trading_date], comparator_by_date[trading_date]
            if left.instrument != right.instrument:
                raise ContractViolation("daily-bar drift comparison requires the same instrument")
            max_diff = max(
                abs(left.open - right.open),
                abs(left.high - right.high),
                abs(left.low - right.low),
                abs(left.close - right.close),
            )
            volume_ratio = None if not left.volume or not right.volume else right.volume / left.volume
            volume_matches = volume_ratio is None or abs(volume_ratio - 1.0) <= self.policy.max_volume_ratio_deviation
            status = "match" if max_diff <= self.policy.max_abs_price_diff and volume_matches else "drift"
            self.repository.record_daily_bar_drift(
                left,
                right,
                max_abs_price_diff=max_diff,
                volume_ratio=volume_ratio,
                status=status,
                observed_at=now,
            )
            records.append(
                DailyBarDriftRecord(
                    instrument=left.instrument,
                    trading_date=trading_date,
                    primary_source=left.source,
                    comparator_source=right.source,
                    max_abs_price_diff=max_diff,
                    volume_ratio=volume_ratio,
                    status=status,
                    observed_at=now,
                )
            )
        return tuple(records)
