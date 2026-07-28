"""Persistence boundary for complete immutable FeatureSnapshot objects."""

from __future__ import annotations

from datetime import datetime

from contracts import DecisionMode, FeatureSnapshot, InstrumentId
from data.repository import FeatureSnapshotWriteResult, SQLiteRepository

from .snapshot import FEATURE_SET_VERSION


class FeatureStore:
    def __init__(self, repository: SQLiteRepository, *, feature_set_version: str = FEATURE_SET_VERSION) -> None:
        self._repository = repository
        self._feature_set_version = feature_set_version

    def save(self, snapshot: FeatureSnapshot) -> FeatureSnapshotWriteResult:
        return self._repository.upsert_feature_snapshot(snapshot)

    def get(self, instrument: InstrumentId, mode: DecisionMode, cutoff_at: datetime, *, feature_set_version: str | None = None) -> FeatureSnapshot | None:
        return self._repository.get_feature_snapshot(
            instrument,
            mode,
            cutoff_at,
            feature_set_version=feature_set_version or self._feature_set_version,
        )
