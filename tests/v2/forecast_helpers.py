from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math

from contracts import (
    DecisionMode,
    FeatureEvidenceMode,
    FeatureSnapshot,
    FeatureStatus,
    FeatureValue,
    ForecastDirection,
    ForecastScope,
    ForecastTrainingSample,
    stable_hash,
)
from forecast.feature_sets import FUNDAMENTALS_V1, NEWS_V1, TECHNICAL_CORE_V1


UTC = timezone.utc


def synthetic_samples(
    instrument,
    *,
    count: int = 320,
    horizon: int = 1,
    predictable: bool = True,
    extended: bool = False,
    start: date = date(2024, 1, 1),
) -> tuple[ForecastTrainingSample, ...]:
    samples = []
    for index in range(count):
        origin = start + timedelta(days=index)
        class_index = index % 3 if predictable else (index * 37 + index // 7) % 3
        direction = (
            ForecastDirection.BULLISH,
            ForecastDirection.NEUTRAL,
            ForecastDirection.BEARISH,
        )[class_index]
        signal = (1.0, 0.0, -1.0)[class_index] if predictable else math.sin(index * 1.731)
        noise = (((index * 37) % 101) / 100.0 - 0.5) * 0.012
        future_return = (0.022 + noise, noise * 0.5, -0.022 + noise)[class_index]
        cutoff = datetime.combine(origin, datetime.min.time(), tzinfo=UTC) + timedelta(hours=23)
        selected_names = TECHNICAL_CORE_V1 + NEWS_V1 + FUNDAMENTALS_V1 if extended else TECHNICAL_CORE_V1
        values = tuple(
            FeatureValue(
                name=name,
                value=signal if name == "closed.return_5" else 0.0,
                status=FeatureStatus.AVAILABLE,
                unit=None,
                lookback=20,
                available_at=cutoff,
                sources=("synthetic",),
                model_eligible=True,
                reason=None,
            )
            for feature_index, name in enumerate(selected_names)
        )
        feature_hash = stable_hash((instrument.stable_key, origin, index))
        snapshot = FeatureSnapshot(
            instrument=instrument,
            mode=DecisionMode.EOD,
            cutoff_at=cutoff,
            latest_bar_date=origin,
            quote_observed_at=None,
            feature_set_version="2.2.0",
            evidence_mode=FeatureEvidenceMode.RECONSTRUCTED_HISTORY,
            values=values,
            input_hash=stable_hash(("input", index)),
            feature_hash=feature_hash,
            generated_at=datetime(2026, 7, 14, tzinfo=UTC),
        )
        target = origin + timedelta(days=horizon)
        samples.append(
            ForecastTrainingSample(
                instrument=instrument,
                scope_membership={ForecastScope.STOCK: instrument.stable_key},
                origin_session_date=origin,
                target_session_date=target,
                horizon=horizon,
                reference_price=100.0,
                target_price=100.0 * (1.0 + future_return),
                future_return=future_return,
                flat_band=0.005,
                direction=direction,
                feature_snapshot=snapshot,
                feature_hash=feature_hash,
                evidence_mode=FeatureEvidenceMode.RECONSTRUCTED_HISTORY,
                matured_at=target,
            )
        )
    return tuple(samples)
