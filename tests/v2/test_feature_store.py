from __future__ import annotations

from dataclasses import replace

from data.repository import SQLiteRepository
from features import FeatureBuilder, FeatureStore

from feature_helpers import bars, calendar, inputs


def test_f11_feature_store_is_idempotent_and_conflict_safe(tmp_path, us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    snapshot = FeatureBuilder(calendar(values)).build(inputs(us_instrument, now, values))
    repository = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    try:
        store = FeatureStore(repository)
        assert store.save(snapshot).inserted == 1
        assert store.save(snapshot).idempotent == 1
        assert store.get(us_instrument, snapshot.mode, snapshot.cutoff_at) == snapshot
        result = store.save(replace(snapshot, feature_hash="0" * 64))
        assert result.conflicts == 1
        assert store.get(us_instrument, snapshot.mode, snapshot.cutoff_at) == snapshot
        assert repository._connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=5").fetchone()[0] == 1
    finally:
        repository.close()


def test_f11_default_store_reads_its_bound_feature_version(tmp_path, us_instrument, now) -> None:
    values = bars(us_instrument, 30, fetched_at=now)
    current = FeatureBuilder(calendar(values)).build(inputs(us_instrument, now, values))
    next_version = FeatureBuilder(calendar(values), feature_set_version="2.2.1").build(
        inputs(us_instrument, now, values)
    )
    repository = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    try:
        store = FeatureStore(repository)
        store.save(current)
        store.save(next_version)
        assert store.get(us_instrument, current.mode, current.cutoff_at) == current
        assert store.get(
            us_instrument,
            current.mode,
            current.cutoff_at,
            feature_set_version="2.2.1",
        ) == next_version
    finally:
        repository.close()
