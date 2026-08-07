"""Online forecast quality gate used after OOF promotion.

OOF promotion proves that a model survived a historical selection protocol.  It
does not grant permanent execution eligibility.  This module evaluates matured
online forecasts by market and horizon and can conservatively suspend their use
for new entries while keeping the forecast visible for diagnosis.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True, slots=True)
class LiveForecastVerdict:
    execution_allowed: bool
    sample_count: int
    direction_accuracy: float | None
    majority_baseline_accuracy: float | None
    mean_brier: float | None
    reason: str


def live_forecast_verdict(outcomes, *, minimum_samples: int = 20) -> LiveForecastVerdict:
    """Judge current online evidence without replacing the OOF trainer.

    A uniform three-class forecast has Brier 2/3.  Direction accuracy is only
    used together with probability quality so a regime with one dominant class
    does not mechanically reject an otherwise useful probabilistic model.
    """
    matured = tuple(
        item for item in outcomes
        if str(getattr(getattr(item, "status", None), "value", getattr(item, "status", ""))) == "matured"
        and item.direction_correct is not None
        and item.actual_direction is not None
        and item.event_brier is not None
    )
    # Re-running Tab1/Tab3 must not manufacture additional evidence for the
    # same stock, origin, target and horizon. Keep the latest persisted fact.
    latest_by_event = {}
    for item in matured:
        instrument = getattr(item, "instrument", None)
        event_identity = (
            getattr(instrument, "stable_key", None),
            getattr(item, "origin_session_date", None),
            item.target_session_date,
            getattr(item, "horizon", None),
        )
        if event_identity[0] is None or event_identity[1] is None or event_identity[3] is None:
            event_identity = ("outcome", item.forecast_outcome_id)
        existing = latest_by_event.get(event_identity)
        if existing is None or (item.generated_at, item.forecast_outcome_id) > (
            existing.generated_at, existing.forecast_outcome_id,
        ):
            latest_by_event[event_identity] = item
    matured = tuple(latest_by_event.values())
    if len(matured) < minimum_samples:
        return LiveForecastVerdict(
            True, len(matured), None, None, None, "LIVE_TRACK_RECORD_SAMPLE_INSUFFICIENT",
        )
    recent = tuple(sorted(
        matured,
        key=lambda item: (item.target_session_date, item.generated_at, item.forecast_outcome_id),
    )[-120:])
    accuracy = mean(float(item.direction_correct) for item in recent)
    counts = Counter(str(getattr(item.actual_direction, "value", item.actual_direction)) for item in recent)
    majority = max(counts.values()) / len(recent)
    brier = mean(float(item.event_brier) for item in recent)
    materially_worse_direction = accuracy + 0.05 < majority
    weak_probability_quality = brier >= 0.63
    worse_than_uniform_probability = brier >= (2.0 / 3.0 + 0.005)
    allowed = not (
        worse_than_uniform_probability
        or (materially_worse_direction and weak_probability_quality)
    )
    return LiveForecastVerdict(
        allowed,
        len(recent),
        accuracy,
        majority,
        brier,
        "LIVE_TRACK_RECORD_ACCEPTABLE" if allowed else "LIVE_TRACK_RECORD_BELOW_BASELINE",
    )
