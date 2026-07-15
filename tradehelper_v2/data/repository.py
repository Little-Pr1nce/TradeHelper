"""隔离的 V2 SQLite 持久化边界。

此模块是唯一允许写入 V2 数据库的低层边界：它负责幂等、冲突 quarantine、
事务和时间序列身份，绝不在这里计算指标、预测或交易建议。V1 数据库只可
通过只读预检访问，不能被本模块迁移或覆盖。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterable, Iterator, Mapping

from tradehelper_v2.contracts.account import AccountSnapshot, PositionSnapshot
from tradehelper_v2.contracts.analysis import FeatureEvidenceMode, FeatureSnapshot, FeatureStatus, FeatureValue
from tradehelper_v2.contracts.enums import (
    AdjustmentMode,
    DecisionMode,
    Exchange,
    FreshnessStatus,
    Market,
    ProviderStatus,
    QualityStatus,
    TradingSession,
)
from tradehelper_v2.contracts.market_data import (
    CanonicalBar,
    ContractViolation,
    FundamentalSnapshot,
    FundamentalValue,
    InstrumentId,
    IntradayBar,
    NewsSnapshot,
    QuoteSnapshot,
    StockMetadata,
    canonical_json,
    ensure_utc,
    stable_hash,
    utc_iso,
)
from tradehelper_v2.contracts.providers import DailyBarsRequest, MigrationPreflight
from tradehelper_v2.contracts.forecast import (
    DirectionProbabilities,
    ForecastAvailability,
    ForecastDirection,
    ForecastDriver,
    ForecastModelVersion,
    ForecastResult,
    ForecastScope,
    ModelFamily,
    ModelLifecycle,
    ModelSpec,
    ReturnDistribution,
    ValidationStatus,
)
from tradehelper_v2.contracts.scenario import (BandSignal, CurrentOverlay, CurrentPriceState, DecisionSession, EntryPosture, ExitPosture, ForecastEvidenceGrade, ForecastSupportLevel, HorizonAlignment, HorizonAssessment, HorizonSignal, NewsDeltaState, PriceLocation, ScenarioBias, ScenarioState, ScenarioStatus, StrategyFamily, TradingScenario, VolatilityShock)
from tradehelper_v2.contracts.strategy import (ConditionEvaluation, ConditionExpression, ConditionOperand, ConditionOperator, ConditionResult, DerivedPriceLevel, EvidenceRequirement, ObservedValue, OperandKind, PlanAction, PlanProfile, PlanReadiness, PositionState, QuantityIntent, StopMode, StopSpec, StrategyBranch, StrategyBundle, TakeProfitMode, TakeProfitSpec, TradePlan)
from tradehelper_v2.contracts.risk import (ConstraintResult, DecisionDisposition, EvidenceStatus, ExecutionDecision, ExecutionLevel, FrozenAccountValuation, MarketEligibility, PositionValuation, RiskAdjustment, RiskConstraintKind, RiskDecisionBundle, RiskProfile, ValuationStatus)
from tradehelper_v2.contracts.execution import (ExecutionEvidenceGrade, ExecutionMode, ExecutionRun, ExecutionStateDelta, EventGranularity, FillEvidence, FillOutcome, IntentBuildStatus, IntentState, OrderIntent, OrderIntentBuildRecord, OrderSide, OrderStyle, PathAssumption, TriggerEvaluation, TriggerState)
from .migrations.schema import apply_schema


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _instrument_from_row(row: sqlite3.Row) -> InstrumentId:
    return InstrumentId(
        code=row["code"],
        market=Market(row["market"]),
        exchange=Exchange(row["exchange"]),
    )


class _ExecutionBatchConflict(Exception):
    def __init__(self, records: tuple[object, ...]) -> None:
        self.records = records


def _bar_hash(bar: CanonicalBar) -> str:
    """Ignore fetched_at so a later identical source refresh stays idempotent."""
    return stable_hash(
        {
            "instrument": bar.instrument.to_dict(),
            "trading_date": bar.trading_date.isoformat(),
            "adjustment_mode": bar.adjustment_mode.value,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "source": bar.source,
            "corporate_action_version": bar.corporate_action_version,
            "schema_version": bar.schema_version,
        }
    )


def _fundamental_payload(snapshot: FundamentalSnapshot) -> str:
    values: dict[str, dict[str, object]] = {}
    for name, field in snapshot.fields.items():
        values[name] = {
            "value": field.value,
            "unit": field.unit,
            "period_end": field.period_end.isoformat() if field.period_end else None,
            "published_at": utc_iso(field.published_at) if field.published_at else None,
            "source": field.source,
        }
    return canonical_json(values)


def _without_generated_at(value):
    """Ignore issuance timestamps recursively when comparing immutable business facts."""
    if isinstance(value, dict):
        return {key: _without_generated_at(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [_without_generated_at(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class DailyBarWriteResult:
    inserted: int
    idempotent: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class FeatureSnapshotWriteResult:
    inserted: int
    idempotent: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class ForecastWriteResult:
    inserted: int
    idempotent: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class QueuedDailyRefresh:
    queue_id: int
    request: DailyBarsRequest
    priority: int
    next_retry_at: datetime
    attempts: int


@dataclass(frozen=True, slots=True)
class DailyBarDriftRecord:
    instrument: InstrumentId
    trading_date: date
    primary_source: str
    comparator_source: str
    max_abs_price_diff: float
    volume_ratio: float | None
    status: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class QueuedProviderRefresh:
    queue_id: int
    task_type: str
    instrument: InstrumentId
    mode: DecisionMode | None
    next_retry_at: datetime
    attempts: int


class SQLiteRepository:
    """只写调用方显式提供的 V2 路径，避免误碰用户已有 V1 数据库。"""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.database_path, timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        apply_schema(self._connection)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def reserve_provider_slot(
        self,
        provider: str,
        market: Market,
        data_type: str,
        as_of: datetime,
        *,
        limit: int,
        window: timedelta,
    ) -> datetime | None:
        """原子预留一个持久化 Provider 配额，或返回可重试的精确时间。"""
        now = ensure_utc(as_of, "as_of")
        cutoff = utc_iso(now - window)
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM provider_rate_events WHERE provider=? AND market=? AND data_type=? AND requested_at <= ?",
                (provider, market.value, data_type, cutoff),
            )
            rows = connection.execute(
                """SELECT requested_at FROM provider_rate_events
                   WHERE provider=? AND market=? AND data_type=? ORDER BY requested_at""",
                (provider, market.value, data_type),
            ).fetchall()
            if len(rows) >= limit:
                return _parse_datetime(rows[0]["requested_at"]) + window
            connection.execute(
                "INSERT INTO provider_rate_events(provider, market, data_type, requested_at) VALUES (?, ?, ?, ?)",
                (provider, market.value, data_type, utc_iso(now)),
            )
        return None

    def reserve_provider_slots(
        self,
        provider: str,
        market: Market,
        data_type: str,
        as_of: datetime,
        *,
        count: int,
        limit: int,
        window: timedelta,
    ) -> datetime | None:
        """Atomically reserve several requests in one provider rate-limit window."""
        if count <= 0 or limit <= 0 or count > limit:
            raise ValueError("provider slot count must be between 1 and its limit")
        now = ensure_utc(as_of, "as_of")
        cutoff = utc_iso(now - window)
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM provider_rate_events WHERE provider=? AND market=? AND data_type=? AND requested_at <= ?",
                (provider, market.value, data_type, cutoff),
            )
            rows = connection.execute(
                """SELECT requested_at FROM provider_rate_events
                   WHERE provider=? AND market=? AND data_type=? ORDER BY requested_at""",
                (provider, market.value, data_type),
            ).fetchall()
            if len(rows) + count > limit:
                return _parse_datetime(rows[0]["requested_at"]) + window
            connection.executemany(
                "INSERT INTO provider_rate_events(provider, market, data_type, requested_at) VALUES (?, ?, ?, ?)",
                [(provider, market.value, data_type, utc_iso(now))] * count,
            )
        return None

    def enqueue_daily_refresh(
        self,
        request: DailyBarsRequest,
        next_retry_at: datetime,
        *,
        priority: int = 0,
        attempts: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO refresh_queue(
                       task_type, instrument_key, code, market, exchange, requested_start, requested_end,
                       listing_date, priority, next_retry_at, status, attempts, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                   ON CONFLICT(task_type, instrument_key, requested_start, requested_end)
                   DO UPDATE SET priority=MAX(priority, excluded.priority), next_retry_at=excluded.next_retry_at,
                                 status='pending', attempts=excluded.attempts, updated_at=excluded.updated_at""",
                (
                    "daily_bars", request.instrument.stable_key, request.instrument.code,
                    request.instrument.market.value, request.instrument.exchange.value,
                    request.requested_start.isoformat(), request.requested_end.isoformat(),
                    request.listing_date.isoformat() if request.listing_date else None,
                    priority, utc_iso(next_retry_at), attempts, utc_iso(now), utc_iso(now),
                ),
            )

    def due_daily_refreshes(self, as_of: datetime, *, limit: int) -> tuple[QueuedDailyRefresh, ...]:
        rows = self._fetchall(
            """SELECT * FROM refresh_queue WHERE task_type='daily_bars' AND status='pending' AND next_retry_at <= ?
               ORDER BY priority DESC, created_at, id LIMIT ?""",
            (utc_iso(as_of), limit),
        )
        return tuple(
            QueuedDailyRefresh(
                queue_id=int(row["id"]),
                request=DailyBarsRequest(
                    InstrumentId(row["code"], Market(row["market"]), Exchange(row["exchange"])),
                    date.fromisoformat(row["requested_start"]), date.fromisoformat(row["requested_end"]),
                    date.fromisoformat(row["listing_date"]) if row["listing_date"] else None,
                ),
                priority=int(row["priority"]), next_retry_at=_parse_datetime(row["next_retry_at"]), attempts=int(row["attempts"]),
            )
            for row in rows
        )

    def mark_daily_refresh_complete(self, queue_id: int) -> None:
        with self._transaction() as connection:
            connection.execute("UPDATE refresh_queue SET status='complete', updated_at=? WHERE id=?", (utc_iso(datetime.now(timezone.utc)), queue_id))

    def mark_daily_refresh_failed(self, queue_id: int, attempts: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE refresh_queue SET status='failed', attempts=?, updated_at=? WHERE id=?",
                (attempts, utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def reschedule_daily_refresh(self, queue_id: int, next_retry_at: datetime, attempts: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE refresh_queue SET next_retry_at=?, attempts=?, updated_at=? WHERE id=?",
                (utc_iso(next_retry_at), attempts, utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def enqueue_provider_refresh(
        self,
        task_type: str,
        instrument: InstrumentId,
        next_retry_at: datetime,
        *,
        mode: DecisionMode | None = None,
        attempts: int = 0,
    ) -> None:
        if task_type not in {"metadata", "listing_date", "fundamentals", "news"}:
            raise ValueError("unsupported provider refresh task")
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO provider_refresh_queue(
                       task_type, instrument_key, code, market, exchange, mode, next_retry_at, status, attempts, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                   ON CONFLICT(task_type, instrument_key, mode)
                   DO UPDATE SET next_retry_at=excluded.next_retry_at, status='pending',
                                 attempts=excluded.attempts, updated_at=excluded.updated_at""",
                (
                    task_type, instrument.stable_key, instrument.code, instrument.market.value, instrument.exchange.value,
                    mode.value if mode is not None else "", utc_iso(next_retry_at), attempts, utc_iso(now), utc_iso(now),
                ),
            )

    def due_provider_refreshes(self, as_of: datetime, *, limit: int) -> tuple[QueuedProviderRefresh, ...]:
        rows = self._fetchall(
            """SELECT * FROM provider_refresh_queue WHERE status='pending' AND next_retry_at <= ?
               ORDER BY created_at, id LIMIT ?""",
            (utc_iso(as_of), limit),
        )
        return tuple(
            QueuedProviderRefresh(
                queue_id=int(row["id"]), task_type=row["task_type"],
                instrument=InstrumentId(row["code"], Market(row["market"]), Exchange(row["exchange"])),
                mode=DecisionMode(row["mode"]) if row["mode"] else None,
                next_retry_at=_parse_datetime(row["next_retry_at"]), attempts=int(row["attempts"]),
            )
            for row in rows
        )

    def mark_provider_refresh_complete(self, queue_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE provider_refresh_queue SET status='complete', updated_at=? WHERE id=?",
                (utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def mark_provider_refresh_failed(self, queue_id: int, attempts: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE provider_refresh_queue SET status='failed', attempts=?, updated_at=? WHERE id=?",
                (attempts, utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def reschedule_provider_refresh(self, queue_id: int, next_retry_at: datetime, attempts: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE provider_refresh_queue SET next_retry_at=?, attempts=?, updated_at=? WHERE id=?",
                (utc_iso(next_retry_at), attempts, utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def record_daily_bar_drift(
        self,
        primary: CanonicalBar,
        comparator: CanonicalBar,
        *,
        max_abs_price_diff: float,
        volume_ratio: float | None,
        status: str,
        observed_at: datetime,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO daily_bar_drift_records(
                       instrument_key, trading_date, primary_source, comparator_source, max_abs_price_diff,
                       volume_ratio, status, primary_payload_json, comparator_payload_json, observed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(instrument_key, trading_date, primary_source, comparator_source, observed_at)
                   DO UPDATE SET max_abs_price_diff=excluded.max_abs_price_diff, volume_ratio=excluded.volume_ratio,
                                 status=excluded.status, primary_payload_json=excluded.primary_payload_json,
                                 comparator_payload_json=excluded.comparator_payload_json""",
                (
                    primary.instrument.stable_key, primary.trading_date.isoformat(), primary.source, comparator.source,
                    max_abs_price_diff, volume_ratio, status, canonical_json(primary.to_dict()),
                    canonical_json(comparator.to_dict()), utc_iso(observed_at),
                ),
            )

    def list_daily_bar_drift(self, instrument: InstrumentId) -> tuple[DailyBarDriftRecord, ...]:
        rows = self._fetchall(
            "SELECT * FROM daily_bar_drift_records WHERE instrument_key=? ORDER BY observed_at DESC, trading_date DESC",
            (instrument.stable_key,),
        )
        return tuple(
            DailyBarDriftRecord(
                instrument=instrument, trading_date=date.fromisoformat(row["trading_date"]),
                primary_source=row["primary_source"], comparator_source=row["comparator_source"],
                max_abs_price_diff=float(row["max_abs_price_diff"]), volume_ratio=row["volume_ratio"],
                status=row["status"], observed_at=_parse_datetime(row["observed_at"]),
            )
            for row in rows
        )

    @staticmethod
    def _feature_payload_hash(snapshot: FeatureSnapshot) -> str:
        payload = snapshot.to_dict()
        payload.pop("generated_at")
        return stable_hash(payload)

    def upsert_feature_snapshot(self, snapshot: FeatureSnapshot) -> FeatureSnapshotWriteResult:
        """按冻结输入身份幂等保存特征；同 key 不同事实进入 quarantine。"""
        if not isinstance(snapshot, FeatureSnapshot):
            raise ContractViolation("feature store only accepts FeatureSnapshot")
        payload_hash = self._feature_payload_hash(snapshot)
        key = (snapshot.instrument.stable_key, snapshot.mode.value, utc_iso(snapshot.cutoff_at),
               snapshot.feature_set_version, snapshot.input_hash)
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT payload_hash FROM feature_snapshots
                   WHERE instrument_key=? AND mode=? AND cutoff_at=? AND feature_set_version=? AND input_hash=?""", key
            ).fetchone()
            if row is not None:
                if row["payload_hash"] == payload_hash:
                    return FeatureSnapshotWriteResult(inserted=0, idempotent=1, conflicts=0)
                connection.execute(
                    """INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at)
                       VALUES (?, ?, NULL, ?, ?, ?)""",
                    ("feature_snapshot_conflict", snapshot.instrument.stable_key, "CONFLICTING_FEATURE_SNAPSHOT",
                     canonical_json(snapshot.to_dict()), utc_iso(datetime.now(timezone.utc))),
                )
                return FeatureSnapshotWriteResult(inserted=0, idempotent=0, conflicts=1)
            connection.execute(
                """INSERT INTO feature_snapshots(
                       instrument_key, code, market, exchange, mode, cutoff_at, latest_bar_date,
                       feature_set_version, evidence_mode, input_hash, feature_hash, payload_json,
                       payload_hash, generated_at, schema_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.instrument.stable_key, snapshot.instrument.code, snapshot.instrument.market.value,
                    snapshot.instrument.exchange.value, snapshot.mode.value, utc_iso(snapshot.cutoff_at),
                    snapshot.latest_bar_date.isoformat() if snapshot.latest_bar_date else None,
                    snapshot.feature_set_version, snapshot.evidence_mode.value, snapshot.input_hash,
                    snapshot.feature_hash, canonical_json(snapshot.to_dict()), payload_hash,
                    utc_iso(snapshot.generated_at), snapshot.schema_version,
                ),
            )
        return FeatureSnapshotWriteResult(inserted=1, idempotent=0, conflicts=0)

    def get_feature_snapshot(
        self, instrument: InstrumentId, mode: DecisionMode, cutoff_at: datetime, *, feature_set_version: str | None = None,
    ) -> FeatureSnapshot | None:
        sql = """SELECT payload_json FROM feature_snapshots WHERE instrument_key=? AND mode=? AND cutoff_at=?"""
        parameters: tuple[object, ...] = (instrument.stable_key, mode.value, utc_iso(cutoff_at))
        if feature_set_version is not None:
            sql += " AND feature_set_version=?"
            parameters += (feature_set_version,)
        sql += " ORDER BY generated_at DESC, feature_set_version DESC, input_hash DESC LIMIT 1"
        row = self._fetchone(sql, parameters)
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        values = tuple(
            FeatureValue(name=item["name"], value=item["value"], status=FeatureStatus(item["status"]), unit=item["unit"],
                         lookback=item["lookback"], available_at=_parse_datetime(item["available_at"]),
                         sources=tuple(item["sources"]), model_eligible=bool(item["model_eligible"]),
                         reason=item["reason"], schema_version=item["schema_version"])
            for item in payload["values"]
        )
        return FeatureSnapshot(
            instrument=instrument, mode=DecisionMode(payload["mode"]), cutoff_at=_parse_datetime(payload["cutoff_at"]),
            latest_bar_date=date.fromisoformat(payload["latest_bar_date"]) if payload["latest_bar_date"] else None,
            quote_observed_at=_parse_datetime(payload["quote_observed_at"]) if payload["quote_observed_at"] else None,
            feature_set_version=payload["feature_set_version"], evidence_mode=FeatureEvidenceMode(payload["evidence_mode"]),
            values=values, input_hash=payload["input_hash"], feature_hash=payload["feature_hash"],
            generated_at=_parse_datetime(payload["generated_at"]), schema_version=payload["schema_version"],
        )

    @staticmethod
    def _forecast_model_payload(version: ForecastModelVersion) -> dict[str, object]:
        return {
            "spec_id": version.spec.spec_id, "family": version.spec.family.value,
            "feature_set_id": version.spec.feature_set_id, "hyperparameters": dict(version.spec.hyperparameters),
            "primary_metric": version.spec.primary_metric, "label_policy_version": version.spec.label_policy_version,
            "preprocessing_version": version.spec.preprocessing_version, "complexity_rank": version.spec.complexity_rank,
        }

    def save_forecast_model_version(self, version: ForecastModelVersion) -> ForecastWriteResult:
        """保存不可变模型版本，并再次校验 artifact 字节哈希。"""
        if version.lifecycle is ModelLifecycle.CHAMPION:
            raise ContractViolation("champion versions must use atomic promote_forecast_model")
        if sha256(version.artifact).hexdigest() != version.artifact_hash:
            raise ContractViolation("forecast model artifact hash does not match artifact bytes")
        payload = self._forecast_model_payload(version)
        with self._transaction() as connection:
            existing = connection.execute("SELECT artifact_hash FROM forecast_model_versions WHERE version=?", (version.version,)).fetchone()
            if existing is not None:
                if existing["artifact_hash"] == version.artifact_hash:
                    return ForecastWriteResult(0, 1, 0)
                connection.execute("INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at) VALUES (?, NULL, NULL, ?, ?, ?)", ("forecast_model_conflict", "CONFLICTING_FORECAST_MODEL", canonical_json(version), utc_iso(datetime.now(timezone.utc))))
                return ForecastWriteResult(0, 0, 1)
            connection.execute(
                """INSERT INTO forecast_model_versions(version, market, scope, scope_key, horizon, spec_json, lifecycle,
                   validation_status, training_start, training_end, selection_start, selection_end, confirmation_start,
                   confirmation_end, training_data_hash, artifact_format, artifact_hash, artifact, random_seed,
                   sample_count, oof_sample_count, created_at, promoted_at, schema_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (version.version, version.market.value, version.scope.value, version.scope_key, version.horizon,
                 canonical_json(payload), version.lifecycle.value, version.validation_status.value,
                 version.training_start.isoformat(), version.training_end.isoformat(),
                 version.selection_start.isoformat() if version.selection_start else None,
                 version.selection_end.isoformat() if version.selection_end else None,
                 version.confirmation_start.isoformat() if version.confirmation_start else None,
                 version.confirmation_end.isoformat() if version.confirmation_end else None,
                 version.training_data_hash, version.artifact_format, version.artifact_hash, version.artifact,
                 version.random_seed, version.sample_count, version.oof_sample_count,
                 utc_iso(version.created_at), utc_iso(version.promoted_at) if version.promoted_at else None,
                 version.schema_version),
            )
        return ForecastWriteResult(1, 0, 0)

    def promote_forecast_model(self, version: ForecastModelVersion) -> None:
        """单事务退役旧 Champion、晋升新版本并追加不可变事件记录。"""
        if version.lifecycle is not ModelLifecycle.CHAMPION or version.validation_status.value != "confirmation_passed":
            raise ContractViolation("only confirmation-passed champion can be promoted")
        if sha256(version.artifact).hexdigest() != version.artifact_hash:
            raise ContractViolation("forecast model artifact hash does not match artifact bytes")
        with self._transaction() as connection:
            existing = connection.execute("SELECT artifact_hash FROM forecast_model_versions WHERE version=?", (version.version,)).fetchone()
            if existing is None:
                payload = self._forecast_model_payload(version)
                # Insert as candidate first; this permits the partial unique champion
                # index to enforce the one-champion invariant throughout the transaction.
                connection.execute(
                    """INSERT INTO forecast_model_versions(version, market, scope, scope_key, horizon, spec_json, lifecycle,
                       validation_status, training_start, training_end, selection_start, selection_end, confirmation_start,
                       confirmation_end, training_data_hash, artifact_format, artifact_hash, artifact, random_seed,
                       sample_count, oof_sample_count, created_at, promoted_at, schema_version)
                       VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                    (version.version, version.market.value, version.scope.value, version.scope_key, version.horizon,
                     canonical_json(payload), version.validation_status.value, version.training_start.isoformat(),
                     version.training_end.isoformat(), version.selection_start.isoformat() if version.selection_start else None,
                     version.selection_end.isoformat() if version.selection_end else None,
                     version.confirmation_start.isoformat() if version.confirmation_start else None,
                     version.confirmation_end.isoformat() if version.confirmation_end else None, version.training_data_hash,
                     version.artifact_format, version.artifact_hash, version.artifact, version.random_seed,
                     version.sample_count, version.oof_sample_count, utc_iso(version.created_at), version.schema_version),
                )
            elif existing["artifact_hash"] != version.artifact_hash:
                raise ContractViolation("existing forecast version conflicts with promoted artifact")
            prior = connection.execute("SELECT version FROM forecast_model_versions WHERE market=? AND scope=? AND scope_key=? AND horizon=? AND lifecycle='champion'", (version.market.value, version.scope.value, version.scope_key, version.horizon)).fetchone()
            if prior and prior["version"] != version.version:
                connection.execute("UPDATE forecast_model_versions SET lifecycle='retired' WHERE version=?", (prior["version"],))
            connection.execute("UPDATE forecast_model_versions SET lifecycle='champion', validation_status='confirmation_passed', promoted_at=? WHERE version=?", (utc_iso(version.promoted_at or datetime.now(timezone.utc)), version.version))
            connection.execute("INSERT OR IGNORE INTO forecast_promotion_events(market, scope, scope_key, horizon, previous_version, promoted_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (version.market.value, version.scope.value, version.scope_key, version.horizon, prior["version"] if prior else None, version.version, utc_iso(datetime.now(timezone.utc))))

    @staticmethod
    def _forecast_model_from_row(row: sqlite3.Row) -> ForecastModelVersion:
        spec_payload = json.loads(row["spec_json"])
        spec = ModelSpec(
            spec_id=spec_payload["spec_id"], family=ModelFamily(spec_payload["family"]),
            feature_set_id=spec_payload["feature_set_id"], hyperparameters=spec_payload["hyperparameters"],
            primary_metric=spec_payload["primary_metric"], label_policy_version=spec_payload["label_policy_version"],
            preprocessing_version=spec_payload["preprocessing_version"], complexity_rank=int(spec_payload["complexity_rank"]),
        )
        return ForecastModelVersion(
            version=row["version"], scope=ForecastScope(row["scope"]), scope_key=row["scope_key"],
            market=Market(row["market"]), horizon=int(row["horizon"]), spec=spec,
            lifecycle=ModelLifecycle(row["lifecycle"]), validation_status=ValidationStatus(row["validation_status"]),
            training_start=date.fromisoformat(row["training_start"]), training_end=date.fromisoformat(row["training_end"]),
            selection_start=date.fromisoformat(row["selection_start"]) if row["selection_start"] else None,
            selection_end=date.fromisoformat(row["selection_end"]) if row["selection_end"] else None,
            confirmation_start=date.fromisoformat(row["confirmation_start"]) if row["confirmation_start"] else None,
            confirmation_end=date.fromisoformat(row["confirmation_end"]) if row["confirmation_end"] else None,
            training_data_hash=row["training_data_hash"], artifact_format=row["artifact_format"],
            artifact_hash=row["artifact_hash"], artifact=bytes(row["artifact"]), random_seed=int(row["random_seed"]),
            sample_count=int(row["sample_count"]), oof_sample_count=int(row["oof_sample_count"]),
            created_at=_parse_datetime(row["created_at"]),
            promoted_at=_parse_datetime(row["promoted_at"]) if row["promoted_at"] else None,
            schema_version=int(row["schema_version"]),
        )

    def get_forecast_model_version(self, version: str) -> ForecastModelVersion | None:
        row = self._fetchone("SELECT * FROM forecast_model_versions WHERE version=?", (version,))
        return self._forecast_model_from_row(row) if row is not None else None

    def list_forecast_champions(self) -> tuple[ForecastModelVersion, ...]:
        rows = self._fetchall(
            "SELECT * FROM forecast_model_versions WHERE lifecycle='champion' ORDER BY market, scope, scope_key, horizon",
            (),
        )
        return tuple(self._forecast_model_from_row(row) for row in rows)

    def save_forecast_model_evaluation(
        self, *, model_version: str, phase: str, data_hash: str, payload: Mapping[str, object], created_at: datetime,
    ) -> ForecastWriteResult:
        if phase not in {"selection", "confirmation"} or len(data_hash) != 64:
            raise ContractViolation("invalid forecast model evaluation identity")
        payload_json = canonical_json(payload)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM forecast_model_evaluations WHERE model_version=? AND phase=? AND data_hash=?",
                (model_version, phase, data_hash),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] == payload_json:
                    return ForecastWriteResult(0, 1, 0)
                connection.execute(
                    "INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at) VALUES (?, NULL, NULL, ?, ?, ?)",
                    ("forecast_evaluation_conflict", "CONFLICTING_FORECAST_EVALUATION", payload_json, utc_iso(datetime.now(timezone.utc))),
                )
                return ForecastWriteResult(0, 0, 1)
            connection.execute(
                "INSERT INTO forecast_model_evaluations(model_version, phase, data_hash, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (model_version, phase, data_hash, payload_json, utc_iso(created_at)),
            )
        return ForecastWriteResult(1, 0, 0)

    def list_forecast_model_evaluations(self, model_version: str) -> tuple[dict[str, object], ...]:
        rows = self._fetchall(
            "SELECT phase, data_hash, payload_json, created_at FROM forecast_model_evaluations WHERE model_version=? ORDER BY created_at, phase",
            (model_version,),
        )
        return tuple({"phase": row["phase"], "data_hash": row["data_hash"], "payload": json.loads(row["payload_json"]), "created_at": _parse_datetime(row["created_at"])} for row in rows)

    @staticmethod
    def _forecast_result_from_payload(payload: Mapping[str, object]) -> ForecastResult:
        instrument_payload = payload["instrument"]
        assert isinstance(instrument_payload, Mapping)
        instrument = InstrumentId(
            str(instrument_payload["code"]), Market(str(instrument_payload["market"])), Exchange(str(instrument_payload["exchange"])),
        )
        probabilities_payload = payload.get("probabilities")
        distribution_payload = payload.get("return_distribution")
        drivers_payload = payload.get("drivers") or ()
        probabilities = DirectionProbabilities(**probabilities_payload) if isinstance(probabilities_payload, Mapping) else None
        distribution = ReturnDistribution(**distribution_payload) if isinstance(distribution_payload, Mapping) else None
        drivers = tuple(
            ForecastDriver(
                feature_name=str(item["feature_name"]), observed_value=float(item["observed_value"]),
                winning_probability_effect=float(item["winning_probability_effect"]),
                direction=ForecastDirection(str(item["direction"])), rank=int(item["rank"]),
            )
            for item in drivers_payload if isinstance(item, Mapping)
        )
        target = payload.get("target_session_date")
        return ForecastResult(
            instrument=instrument, cutoff_at=_parse_datetime(str(payload["cutoff_at"])),
            origin_session_date=date.fromisoformat(str(payload["origin_session_date"])),
            target_session_date=date.fromisoformat(str(target)) if target else None,
            horizon=int(payload["horizon"]), reference_price=float(payload["reference_price"]),
            availability=ForecastAvailability(str(payload["availability"])), probabilities=probabilities,
            return_distribution=distribution,
            direction=ForecastDirection(str(payload["direction"])) if payload.get("direction") else None,
            confidence_margin=float(payload["confidence_margin"]) if payload.get("confidence_margin") is not None else None,
            model_scope=ForecastScope(str(payload["model_scope"])), scope_key=str(payload["scope_key"]),
            model_family=ModelFamily(str(payload["model_family"])), model_version=str(payload["model_version"]),
            lifecycle=ModelLifecycle(str(payload["lifecycle"])), validation_status=ValidationStatus(str(payload["validation_status"])),
            execution_eligible=bool(payload["execution_eligible"]), feature_set_id=str(payload["feature_set_id"]),
            feature_set_version=str(payload["feature_set_version"]), model_input_hash=str(payload["model_input_hash"]),
            training_data_hash=str(payload["training_data_hash"]) if payload.get("training_data_hash") else None,
            sample_count=int(payload["sample_count"]), oof_sample_count=int(payload["oof_sample_count"]), drivers=drivers,
            calendar_source=str(payload["calendar_source"]), reason=str(payload["reason"]) if payload.get("reason") else None,
            event_key=str(payload["event_key"]), generated_at=_parse_datetime(str(payload["generated_at"])),
            schema_version=int(payload.get("schema_version", 1)),
        )

    def get_forecast_result(self, event_key: str) -> ForecastResult | None:
        row = self._fetchone("SELECT payload_json FROM forecast_snapshots WHERE event_key=?", (event_key,))
        return self._forecast_result_from_payload(json.loads(row["payload_json"])) if row is not None else None

    def list_forecast_results(self, instrument: InstrumentId, *, horizon: int | None = None) -> tuple[ForecastResult, ...]:
        sql = "SELECT payload_json FROM forecast_snapshots WHERE instrument_key=?"
        parameters: tuple[object, ...] = (instrument.stable_key,)
        if horizon is not None:
            sql += " AND horizon=?"
            parameters += (horizon,)
        sql += " ORDER BY origin_session_date, horizon, event_key"
        return tuple(self._forecast_result_from_payload(json.loads(row["payload_json"])) for row in self._fetchall(sql, parameters))

    def save_forecast_result(self, result: ForecastResult) -> ForecastWriteResult:
        """按 event_key 幂等记录预测发行事实；冲突不覆盖而是 quarantine。"""
        identity_payload = json.loads(canonical_json(result))
        identity_payload.pop("generated_at", None)
        payload_hash = stable_hash(identity_payload)
        with self._transaction() as connection:
            existing = connection.execute("SELECT payload_hash, payload_json FROM forecast_snapshots WHERE event_key=?", (result.event_key,)).fetchone()
            if existing is not None:
                if existing["payload_hash"] == payload_hash:
                    return ForecastWriteResult(0, 1, 0)
                stored_payload = json.loads(existing["payload_json"])
                stored_payload.pop("generated_at", None)
                if stable_hash(stored_payload) == payload_hash:
                    connection.execute("UPDATE forecast_snapshots SET payload_hash=? WHERE event_key=?", (payload_hash, result.event_key))
                    return ForecastWriteResult(0, 1, 0)
                connection.execute("INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", ("forecast_snapshot_conflict", result.instrument.stable_key, result.origin_session_date.isoformat(), "CONFLICTING_FORECAST_SNAPSHOT", canonical_json(result), utc_iso(datetime.now(timezone.utc))))
                return ForecastWriteResult(0, 0, 1)
            connection.execute("INSERT INTO forecast_snapshots(event_key, instrument_key, origin_session_date, target_session_date, horizon, model_version, payload_json, payload_hash, generated_at, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (result.event_key, result.instrument.stable_key, result.origin_session_date.isoformat(), result.target_session_date.isoformat() if result.target_session_date else None, result.horizon, result.model_version, canonical_json(result), payload_hash, utc_iso(result.generated_at), result.schema_version))
        return ForecastWriteResult(1, 0, 0)

    def save_trading_scenario(self, scenario: TradingScenario) -> ForecastWriteResult:
        """保存情景事实；仅 generated_at 不同视为同一业务发行。"""
        payload = json.loads(canonical_json(scenario)); identity = dict(payload); identity.pop("generated_at", None)
        payload_hash = stable_hash(identity)
        with self._transaction() as connection:
            row = connection.execute("SELECT payload_json FROM trading_scenarios WHERE scenario_id=? OR event_key=?", (scenario.scenario_id, scenario.event_key)).fetchone()
            if row is not None:
                old = json.loads(row["payload_json"]); old.pop("generated_at", None)
                if stable_hash(old) == payload_hash: return ForecastWriteResult(0, 1, 0)
                connection.execute("INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", ("trading_scenario_conflict", scenario.instrument.stable_key, scenario.origin_session_date.isoformat(), "CONFLICTING_TRADING_SCENARIO", canonical_json(scenario), utc_iso(datetime.now(timezone.utc))))
                return ForecastWriteResult(0, 0, 1)
            connection.execute("INSERT INTO trading_scenarios(scenario_id,event_key,instrument_key,market,exchange,mode,origin_session_date,decision_session_date,forecast_bundle_hash,current_feature_hash,fact_update_hash,quality_hash,policy_version,payload_json,generated_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (scenario.scenario_id,scenario.event_key,scenario.instrument.stable_key,scenario.instrument.market.value,scenario.instrument.exchange.value,scenario.mode.value,scenario.origin_session_date.isoformat(),scenario.decision_session.session_date.isoformat() if scenario.decision_session else None,scenario.forecast_bundle_hash,scenario.current_feature_hash,scenario.fact_update_hash,scenario.quality_hash,scenario.policy_version,canonical_json(scenario),utc_iso(scenario.generated_at),scenario.schema_version))
        return ForecastWriteResult(1,0,0)

    @staticmethod
    def _scenario_from_payload(payload: dict) -> TradingScenario:
        instrument = InstrumentId(payload["instrument"]["code"], Market(payload["instrument"]["market"]), Exchange(payload["instrument"]["exchange"]))
        session_raw = payload["decision_session"]
        session = None if session_raw is None else DecisionSession(Market(session_raw["market"]), Exchange(session_raw["exchange"]), date.fromisoformat(session_raw["session_date"]), _parse_datetime(session_raw["regular_open"]), _parse_datetime(session_raw["regular_close"]), tuple((_parse_datetime(left),_parse_datetime(right)) for left,right in session_raw["breaks"]), session_raw["source"], int(session_raw.get("schema_version",1)))
        assessments=[]
        for item in payload["horizon_assessments"]:
            prob=None if item["probabilities"] is None else DirectionProbabilities(**item["probabilities"])
            def dist(value): return None if value is None else ReturnDistribution(value["p10"],value["p50"],value["p90"],value["method"])
            assessments.append(HorizonAssessment(int(item["horizon"]),date.fromisoformat(item["target_session_date"]) if item["target_session_date"] else None,item["forecast_event_key"],ForecastEvidenceGrade(item["evidence_grade"]),HorizonSignal(item["signal"]),prob,dist(item["original_distribution"]),dist(item["remaining_distribution"]),item["confidence_margin"],PriceLocation(item["price_location"]),tuple(item["reason_codes"])))
        overlay_raw=payload["current_overlay"]
        overlay=CurrentOverlay(CurrentPriceState(overlay_raw["price_state"]),overlay_raw["current_price"],overlay_raw["price_source"],_parse_datetime(overlay_raw["observed_at"]) if overlay_raw["observed_at"] else None,overlay_raw["realized_return_from_origin"],PriceLocation(overlay_raw["tactical_price_location"]),VolatilityShock(overlay_raw["volatility_shock"]),NewsDeltaState(overlay_raw["news_delta"]),bool(overlay_raw["news_update_present"]),bool(overlay_raw["fundamental_update_present"]),int(overlay_raw["fact_update_count"]),bool(overlay_raw["unmodeled_fact_update"]),tuple(overlay_raw["reason_codes"]))
        return TradingScenario(payload["scenario_id"],payload["event_key"],instrument,DecisionMode(payload["mode"]),_parse_datetime(payload["as_of"]),date.fromisoformat(payload["origin_session_date"]),session,_parse_datetime(payload["valid_from"]) if payload["valid_from"] else None,_parse_datetime(payload["expires_at"]) if payload["expires_at"] else None,ScenarioBias(payload["bias"]),BandSignal(payload["tactical_signal"]),BandSignal(payload["swing_signal"]),HorizonAlignment(payload["alignment"]),ScenarioState(payload["state"]),ForecastSupportLevel(payload["forecast_support"]),ScenarioStatus(payload["status"]),tuple(assessments),overlay,tuple(StrategyFamily(item) for item in payload["allowed_strategy_families"]),tuple(StrategyFamily(item) for item in payload["blocked_strategy_families"]),EntryPosture(payload["entry_posture"]),ExitPosture(payload["exit_posture"]),tuple(payload["reason_codes"]),payload["forecast_bundle_hash"],payload["current_feature_hash"],payload["fact_update_hash"],payload["quality_hash"],payload["policy_version"],_parse_datetime(payload["generated_at"]),int(payload.get("schema_version",1)))

    @classmethod
    def _scenario_from_row(cls, row: sqlite3.Row) -> TradingScenario:
        scenario = cls._scenario_from_payload(json.loads(row["payload_json"]))
        expected = {
            "scenario_id": scenario.scenario_id,
            "event_key": scenario.event_key,
            "instrument_key": scenario.instrument.stable_key,
            "market": scenario.instrument.market.value,
            "exchange": scenario.instrument.exchange.value,
            "mode": scenario.mode.value,
            "origin_session_date": scenario.origin_session_date.isoformat(),
            "decision_session_date": (
                scenario.decision_session.session_date.isoformat()
                if scenario.decision_session
                else None
            ),
            "forecast_bundle_hash": scenario.forecast_bundle_hash,
            "current_feature_hash": scenario.current_feature_hash,
            "fact_update_hash": scenario.fact_update_hash,
            "quality_hash": scenario.quality_hash,
            "policy_version": scenario.policy_version,
            "schema_version": scenario.schema_version,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise ContractViolation("stored trading scenario columns do not match payload identity")
        return scenario

    def get_trading_scenario(self, scenario_id: str) -> TradingScenario | None:
        row=self._fetchone("SELECT * FROM trading_scenarios WHERE scenario_id=?",(scenario_id,))
        return self._scenario_from_row(row) if row else None

    def list_trading_scenarios(self, instrument: InstrumentId, mode: DecisionMode, decision_session_date: date | None) -> tuple[TradingScenario,...]:
        sql="SELECT * FROM trading_scenarios WHERE instrument_key=? AND mode=? AND decision_session_date IS ? ORDER BY generated_at, scenario_id"
        return tuple(self._scenario_from_row(row) for row in self._fetchall(sql,(instrument.stable_key,mode.value,decision_session_date.isoformat() if decision_session_date else None)))

    def save_trade_plan(self, plan: TradePlan) -> ForecastWriteResult:
        """保存不可变计划；同 event_key 的不同业务事实只进入 quarantine。"""
        payload = json.loads(canonical_json(plan)); payload_hash = stable_hash(_without_generated_at(payload))
        session = plan.event_key.split("|")[1] if "|" in plan.event_key else "calendar-unavailable"
        with self._transaction() as connection:
            row = connection.execute("SELECT payload_json FROM trade_plans WHERE plan_id=? OR event_key=?", (plan.plan_id, plan.event_key)).fetchone()
            if row is not None:
                old = json.loads(row["payload_json"])
                if stable_hash(_without_generated_at(old)) == payload_hash:
                    return ForecastWriteResult(0, 1, 0)
                connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)", ("trade_plan_conflict", plan.instrument.stable_key, session, "CONFLICTING_TRADE_PLAN", canonical_json(plan), utc_iso(datetime.now(timezone.utc))))
                return ForecastWriteResult(0, 0, 1)
            connection.execute("INSERT INTO trade_plans(plan_id,event_key,instrument_key,scenario_id,strategy_id,strategy_version,family,action,readiness,decision_session_date,payload_json,generated_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (plan.plan_id, plan.event_key, plan.instrument.stable_key, plan.scenario_id, plan.strategy_id, plan.strategy_version, plan.family.value, plan.action.value, plan.readiness.value, session if session != "calendar-unavailable" else None, canonical_json(plan), utc_iso(plan.generated_at), plan.schema_version))
        return ForecastWriteResult(1, 0, 0)

    @staticmethod
    def _plan_from_payload(payload: dict) -> TradePlan:
        def operand(item):
            return None if item is None else ConditionOperand(OperandKind(item["kind"]), item["key"], item["value"], item["unit"], tuple(item["source_features"]))
        def expression(item):
            return None if item is None else ConditionExpression(item["condition_id"], ConditionOperator(item["operator"]), operand(item["left"]), operand(item["right"]), operand(item["lower"]), operand(item["upper"]), tuple(expression(child) for child in item["children"]), EvidenceRequirement(item["evidence_requirement"]), item["reason_code"], int(item.get("schema_version", 1)))
        def level(item):
            return None if item is None else DerivedPriceLevel(item["level_id"], item["value"], item["role"], item["calculation_code"], item["calculation_version"], tuple(item["source_features"]), item["source_scenario_id"])
        def evaluation(item):
            def observed(value):
                raw = value["status"]
                try: status = FeatureStatus(raw)
                except ValueError: status = ConditionResult(raw)
                return ObservedValue(value["key"], value["value"], status, _parse_datetime(value["available_at"]) if value["available_at"] else None)
            return ConditionEvaluation(item["condition_id"], ConditionResult(item["result"]), tuple(observed(value) for value in item["observed_values"]), tuple(item["missing_features"]), _parse_datetime(item["evaluated_at"]))
        instrument = InstrumentId(payload["instrument"]["code"], Market(payload["instrument"]["market"]), Exchange(payload["instrument"]["exchange"]))
        stop_raw = payload["stop"]
        stop = None if stop_raw is None else StopSpec(StopMode(stop_raw["mode"]), level(stop_raw["level"]), expression(stop_raw["condition"]), stop_raw["reason_code"])
        take_raw = payload["take_profit"]
        take = None if take_raw is None else TakeProfitSpec(TakeProfitMode(take_raw["mode"]), level(take_raw["level"]), take_raw["risk_multiple"], expression(take_raw["condition"]), take_raw["reason_code"])
        return TradePlan(payload["plan_id"], payload["event_key"], instrument, payload["scenario_id"], payload["strategy_id"], payload["strategy_version"], payload["parameter_hash"], StrategyFamily(payload["family"]), PlanAction(payload["action"]), QuantityIntent(payload["quantity_intent"]), tuple(PlanProfile(item) for item in payload["profiles"]), PlanReadiness(payload["readiness"]), expression(payload["trigger_condition"]), expression(payload["confirmation_condition"]), level(payload["trigger_level"]), stop, take, expression(payload["hold_condition"]), expression(payload["invalidation_condition"]), tuple(evaluation(item) for item in payload["evaluations"]), tuple(payload["evidence_features"]), tuple(payload["missing_conditions"]), tuple(payload["reason_codes"]), _parse_datetime(payload["valid_from"]) if payload["valid_from"] else None, _parse_datetime(payload["expires_at"]) if payload["expires_at"] else None, payload["position_hash"], payload["policy_version"], _parse_datetime(payload["generated_at"]), int(payload.get("schema_version", 1)))

    @classmethod
    def _plan_from_row(cls, row: sqlite3.Row) -> TradePlan:
        plan = cls._plan_from_payload(json.loads(row["payload_json"]))
        session = plan.event_key.split("|")[1]
        expected = {"plan_id": plan.plan_id, "event_key": plan.event_key, "instrument_key": plan.instrument.stable_key, "scenario_id": plan.scenario_id, "strategy_id": plan.strategy_id, "strategy_version": plan.strategy_version, "family": plan.family.value, "action": plan.action.value, "readiness": plan.readiness.value, "decision_session_date": None if session == "calendar-unavailable" else session, "generated_at": utc_iso(plan.generated_at), "schema_version": plan.schema_version}
        if any(row[name] != value for name, value in expected.items()):
            raise ContractViolation("stored trade plan columns do not match payload identity")
        return plan

    def get_trade_plan(self, plan_id: str) -> TradePlan | None:
        row = self._fetchone("SELECT * FROM trade_plans WHERE plan_id=?", (plan_id,))
        return self._plan_from_row(row) if row else None

    def list_trade_plans(self, instrument: InstrumentId, scenario_id: str | None = None) -> tuple[TradePlan, ...]:
        sql = "SELECT * FROM trade_plans WHERE instrument_key=?"; params: tuple[object, ...] = (instrument.stable_key,)
        if scenario_id is not None:
            sql += " AND scenario_id=?"; params += (scenario_id,)
        return tuple(self._plan_from_row(row) for row in self._fetchall(sql + " ORDER BY generated_at, plan_id", params))

    def save_strategy_bundle(self, bundle: StrategyBundle) -> ForecastWriteResult:
        payload = json.loads(canonical_json(bundle)); payload_hash = stable_hash(_without_generated_at(payload))
        session = bundle.event_key.split("|")[1] if "|" in bundle.event_key else "calendar-unavailable"
        plans = bundle.entry_or_add.plans + bundle.reduce_or_exit.plans + bundle.hold.plans + bundle.invalidation.plans
        position_hash = next((item.position_hash for item in plans), stable_hash("flat"))
        with self._transaction() as connection:
            row = connection.execute("SELECT payload_json FROM strategy_bundles WHERE bundle_id=? OR event_key=?", (bundle.bundle_id, bundle.event_key)).fetchone()
            if row is not None:
                old = json.loads(row["payload_json"])
                if stable_hash(_without_generated_at(old)) == payload_hash:
                    return ForecastWriteResult(0, 1, 0)
                connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)", ("strategy_bundle_conflict", bundle.instrument.stable_key, session, "CONFLICTING_STRATEGY_BUNDLE", canonical_json(bundle), utc_iso(datetime.now(timezone.utc))))
                return ForecastWriteResult(0, 0, 1)
            connection.execute("INSERT INTO strategy_bundles(bundle_id,event_key,instrument_key,scenario_id,position_hash,payload_json,generated_at,schema_version) VALUES (?,?,?,?,?,?,?,?)", (bundle.bundle_id, bundle.event_key, bundle.instrument.stable_key, bundle.scenario_id, position_hash, canonical_json(bundle), utc_iso(bundle.generated_at), bundle.schema_version))
        return ForecastWriteResult(1, 0, 0)

    @classmethod
    def _bundle_from_row(cls, row: sqlite3.Row) -> StrategyBundle:
        payload = json.loads(row["payload_json"])
        def branch(item):
            return StrategyBranch(item["branch"], tuple(cls._plan_from_payload(plan) for plan in item["plans"]), PlanReadiness(item["readiness"]), item["not_applicable_reason"])
        instrument = InstrumentId(payload["instrument"]["code"], Market(payload["instrument"]["market"]), Exchange(payload["instrument"]["exchange"]))
        bundle = StrategyBundle(payload["bundle_id"], payload["event_key"], instrument, payload["scenario_id"], PositionState(payload["position_state"]), branch(payload["entry_or_add"]), branch(payload["reduce_or_exit"]), branch(payload["hold"]), branch(payload["invalidation"]), tuple(payload["conservative_plan_ids"]), tuple(payload["aggressive_plan_ids"]), payload["conflict_state"], tuple(payload["reason_codes"]), payload["policy_version"], _parse_datetime(payload["generated_at"]), int(payload.get("schema_version", 1)))
        plans = bundle.entry_or_add.plans + bundle.reduce_or_exit.plans + bundle.hold.plans + bundle.invalidation.plans
        position_hashes = {plan.position_hash for plan in plans}
        if (row["bundle_id"] != bundle.bundle_id or row["event_key"] != bundle.event_key or
                row["instrument_key"] != bundle.instrument.stable_key or row["scenario_id"] != bundle.scenario_id or
                row["schema_version"] != bundle.schema_version or len(position_hashes) != 1 or
                row["position_hash"] != next(iter(position_hashes)) or row["generated_at"] != utc_iso(bundle.generated_at)):
            raise ContractViolation("stored strategy bundle columns do not match payload identity")
        return bundle

    def get_strategy_bundle(self, bundle_id: str) -> StrategyBundle | None:
        row = self._fetchone("SELECT * FROM strategy_bundles WHERE bundle_id=?", (bundle_id,))
        return self._bundle_from_row(row) if row else None

    def list_strategy_bundles(self, instrument: InstrumentId, scenario_id: str | None = None) -> tuple[StrategyBundle, ...]:
        sql = "SELECT * FROM strategy_bundles WHERE instrument_key=?"; params: tuple[object, ...] = (instrument.stable_key,)
        if scenario_id is not None:
            sql += " AND scenario_id=?"; params += (scenario_id,)
        return tuple(self._bundle_from_row(row) for row in self._fetchall(sql + " ORDER BY generated_at, bundle_id", params))

    def _save_risk_record(self, *, table: str, id_column: str, record, columns: tuple[str, ...], values: tuple[object, ...], record_type: str, instrument_key: str | None, event_day: str | None) -> ForecastWriteResult:
        payload = json.loads(canonical_json(record)); identity = _without_generated_at(payload); payload_hash = stable_hash(identity)
        record_id, event_key = getattr(record, id_column), record.event_key
        with self._transaction() as connection:
            row = connection.execute(f"SELECT payload_json FROM {table} WHERE {id_column}=? OR event_key=?", (record_id, event_key)).fetchone()
            if row is not None:
                if stable_hash(_without_generated_at(json.loads(row["payload_json"]))) == payload_hash:
                    return ForecastWriteResult(0, 1, 0)
                connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)", (record_type, instrument_key, event_day, "CONFLICTING_RISK_RECORD", canonical_json(record), utc_iso(datetime.now(timezone.utc))))
                return ForecastWriteResult(0, 0, 1)
            placeholders = ",".join("?" for _ in columns)
            connection.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})", values + (canonical_json(record), utc_iso(record.generated_at), record.schema_version))
        return ForecastWriteResult(1, 0, 0)

    def save_frozen_account_valuation(self, valuation: FrozenAccountValuation) -> ForecastWriteResult:
        return self._save_risk_record(table="frozen_account_valuations", id_column="valuation_id", record=valuation, columns=("valuation_id","event_key","market","currency","account_hash","price_batch_hash","valuation_at","status","payload_json","generated_at","schema_version"), values=(valuation.valuation_id,valuation.event_key,valuation.market.value,valuation.currency,valuation.account_hash,valuation.price_batch_hash,utc_iso(valuation.valuation_at),valuation.status.value), record_type="frozen_account_valuation_conflict", instrument_key=None, event_day=valuation.valuation_at.date().isoformat())

    @staticmethod
    def _valuation_from_payload(payload: dict) -> FrozenAccountValuation:
        positions = tuple(PositionValuation(InstrumentId(item["instrument"]["code"], Market(item["instrument"]["market"]), Exchange(item["instrument"]["exchange"])), Decimal(item["shares"]), Decimal(item["price"]), Decimal(item["market_value"]), item["position_pct"], Decimal(item["unrealized_pnl_amount"]), item["unrealized_pnl_pct"]) for item in payload["position_values"])
        missing = tuple(InstrumentId(item["code"], Market(item["market"]), Exchange(item["exchange"])) for item in payload["missing_price_instruments"])
        return FrozenAccountValuation(payload["valuation_id"], payload["event_key"], Market(payload["market"]), payload["currency"], payload["account_hash"], payload["price_batch_hash"], _parse_datetime(payload["valuation_at"]), ValuationStatus(payload["status"]), Decimal(payload["equity"]) if payload["equity"] is not None else None, Decimal(payload["cash"]), Decimal(payload["invested_value"]) if payload["invested_value"] is not None else None, payload["invested_pct"], positions, missing, _parse_datetime(payload["generated_at"]), int(payload.get("schema_version",1)))

    def get_frozen_account_valuation(self, valuation_id: str) -> FrozenAccountValuation | None:
        row=self._fetchone("SELECT * FROM frozen_account_valuations WHERE valuation_id=?",(valuation_id,))
        if row is None: return None
        value=self._valuation_from_payload(json.loads(row["payload_json"]))
        if (row["event_key"] != value.event_key or row["market"] != value.market.value or row["currency"] != value.currency or
                row["account_hash"] != value.account_hash or row["price_batch_hash"] != value.price_batch_hash or
                row["valuation_at"] != utc_iso(value.valuation_at) or row["status"] != value.status.value or
                row["generated_at"] != utc_iso(value.generated_at) or row["schema_version"] != value.schema_version):
            raise ContractViolation("stored valuation columns do not match payload")
        return value

    def list_frozen_account_valuations(self, market: Market) -> tuple[FrozenAccountValuation,...]:
        return tuple(self.get_frozen_account_valuation(row["valuation_id"]) for row in self._fetchall("SELECT valuation_id FROM frozen_account_valuations WHERE market=? ORDER BY valuation_at,valuation_id",(market.value,)))

    def save_execution_decision(self, decision: ExecutionDecision) -> ForecastWriteResult:
        return self._save_risk_record(table="execution_decisions", id_column="decision_id", record=decision, columns=("decision_id","event_key","instrument_key","scenario_id","bundle_id","plan_id","profile","level","disposition","account_hash","valuation_id","payload_json","generated_at","schema_version"), values=(decision.decision_id,decision.event_key,decision.instrument.stable_key,decision.scenario_id,decision.bundle_id,decision.plan_id,decision.profile.value,decision.level.value,decision.disposition.value,decision.account_hash,decision.valuation_id), record_type="execution_decision_conflict", instrument_key=decision.instrument.stable_key, event_day=decision.valid_from.date().isoformat() if decision.valid_from else None)

    @staticmethod
    def _decision_from_payload(payload: dict) -> ExecutionDecision:
        instrument=InstrumentId(payload["instrument"]["code"],Market(payload["instrument"]["market"]),Exchange(payload["instrument"]["exchange"]))
        constraints=tuple(ConstraintResult(item["code"],RiskConstraintKind(item["kind"]),bool(item["passed"]),Decimal(item["limit"]) if item["limit"] is not None else None,Decimal(item["observed"]) if item["observed"] is not None else None) for item in payload["hard_constraints"])
        adjustments=tuple(RiskAdjustment(item["code"],Decimal(item["multiplier"])) for item in payload["soft_adjustments"])
        def money(name): return Decimal(payload[name]) if payload[name] is not None else None
        return ExecutionDecision(payload["decision_id"],payload["event_key"],instrument,payload["scenario_id"],payload["bundle_id"],payload["plan_id"],RiskProfile(payload["profile"]),PlanAction(payload["action"]),QuantityIntent(payload["quantity_intent"]),ExecutionLevel(payload["level"]),DecisionDisposition(payload["disposition"]),bool(payload["executable_now"]),bool(payload["recheck_at_trigger"]),Decimal(payload["approved_shares"]),Decimal(payload["blocked_shares"]),money("entry_price"),money("stop_price"),money("current_position_value"),payload["current_position_pct"],money("planned_position_value"),payload["post_trade_position_pct"],money("risk_budget_amount"),money("incremental_planned_loss"),money("total_position_planned_loss"),money("max_loss_amount"),money("friction_reserve"),MarketEligibility(payload["market_eligibility"]),EvidenceStatus(payload["evidence_status"]),constraints,adjustments,tuple(payload["reason_codes"]),_parse_datetime(payload["valid_from"]) if payload["valid_from"] else None,_parse_datetime(payload["expires_at"]) if payload["expires_at"] else None,payload["account_hash"],payload["valuation_id"],payload["quality_hash"],payload["evidence_hash"],payload["market_rule_version"],payload["risk_policy_version"],_parse_datetime(payload["generated_at"]),int(payload.get("schema_version",1)))

    def get_execution_decision(self, decision_id: str) -> ExecutionDecision | None:
        row=self._fetchone("SELECT * FROM execution_decisions WHERE decision_id=?",(decision_id,))
        if row is None: return None
        value=self._decision_from_payload(json.loads(row["payload_json"]))
        expected_columns = {"event_key":value.event_key,"instrument_key":value.instrument.stable_key,"scenario_id":value.scenario_id,
                            "bundle_id":value.bundle_id,"plan_id":value.plan_id,"profile":value.profile.value,"level":value.level.value,
                            "disposition":value.disposition.value,"account_hash":value.account_hash,"valuation_id":value.valuation_id,
                            "generated_at":utc_iso(value.generated_at),"schema_version":value.schema_version}
        if any(row[name] != expected for name,expected in expected_columns.items()): raise ContractViolation("stored execution decision columns do not match payload")
        return value

    def list_execution_decisions(self, instrument: InstrumentId, scenario_id: str | None = None) -> tuple[ExecutionDecision,...]:
        sql="SELECT decision_id FROM execution_decisions WHERE instrument_key=?"; params:tuple[object,...]=(instrument.stable_key,)
        if scenario_id: sql+=" AND scenario_id=?"; params+=(scenario_id,)
        return tuple(self.get_execution_decision(row["decision_id"]) for row in self._fetchall(sql+" ORDER BY generated_at,decision_id",params))

    def save_risk_decision_bundle(self, bundle: RiskDecisionBundle) -> ForecastWriteResult:
        return self._save_risk_record(table="risk_decision_bundles", id_column="risk_bundle_id", record=bundle, columns=("risk_bundle_id","event_key","instrument_key","scenario_id","strategy_bundle_id","account_hash","valuation_id","payload_json","generated_at","schema_version"), values=(bundle.risk_bundle_id,bundle.event_key,bundle.instrument.stable_key,bundle.scenario_id,bundle.strategy_bundle_id,bundle.account_hash,bundle.valuation_id), record_type="risk_decision_bundle_conflict", instrument_key=bundle.instrument.stable_key, event_day=None)

    @classmethod
    def _risk_bundle_from_payload(cls,payload:dict)->RiskDecisionBundle:
        instrument=InstrumentId(payload["instrument"]["code"],Market(payload["instrument"]["market"]),Exchange(payload["instrument"]["exchange"]))
        return RiskDecisionBundle(payload["risk_bundle_id"],payload["event_key"],instrument,payload["scenario_id"],payload["strategy_bundle_id"],PositionState(payload["position_state"]),tuple(cls._decision_from_payload(item) for item in payload["decisions"]),tuple(payload["conservative_decision_ids"]),tuple(payload["aggressive_decision_ids"]),tuple(payload["protective_decision_ids"]),payload["account_hash"],payload["valuation_id"],payload["quality_hash"],payload["market_rule_version"],payload["risk_policy_version"],_parse_datetime(payload["generated_at"]),int(payload.get("schema_version",1)))

    def get_risk_decision_bundle(self,risk_bundle_id:str)->RiskDecisionBundle|None:
        row=self._fetchone("SELECT * FROM risk_decision_bundles WHERE risk_bundle_id=?",(risk_bundle_id,))
        if row is None:return None
        value=self._risk_bundle_from_payload(json.loads(row["payload_json"]))
        expected_columns = {"event_key":value.event_key,"instrument_key":value.instrument.stable_key,"scenario_id":value.scenario_id,
                            "strategy_bundle_id":value.strategy_bundle_id,"account_hash":value.account_hash,"valuation_id":value.valuation_id,
                            "generated_at":utc_iso(value.generated_at),"schema_version":value.schema_version}
        if any(row[name] != expected for name,expected in expected_columns.items()): raise ContractViolation("stored risk bundle columns do not match payload")
        return value

    def list_risk_decision_bundles(self,instrument:InstrumentId,scenario_id:str|None=None)->tuple[RiskDecisionBundle,...]:
        sql="SELECT risk_bundle_id FROM risk_decision_bundles WHERE instrument_key=?"; params:tuple[object,...]=(instrument.stable_key,)
        if scenario_id:sql+=" AND scenario_id=?";params+=(scenario_id,)
        return tuple(self.get_risk_decision_bundle(row["risk_bundle_id"]) for row in self._fetchall(sql+" ORDER BY generated_at,risk_bundle_id",params))

    def _save_execution_record(self, *, table: str, id_column: str, record, columns: tuple[str, ...], values: tuple[object, ...], record_type: str, instrument_key: str | None, connection: sqlite3.Connection | None = None) -> ForecastWriteResult:
        """V2-7 的统一幂等/冲突隔离写入边界。"""
        if connection is None:
            with self._transaction() as active:
                return self._save_execution_record(table=table,id_column=id_column,record=record,columns=columns,values=values,record_type=record_type,instrument_key=instrument_key,connection=active)
        payload = json.loads(canonical_json(record)); payload_hash = stable_hash(_without_generated_at(payload))
        record_id, event_key = getattr(record, id_column), getattr(record, "event_key", getattr(record, id_column))
        row = connection.execute(f"SELECT payload_json FROM {table} WHERE {id_column}=? OR event_key=?", (record_id, event_key)).fetchone()
        if row is not None:
            if stable_hash(_without_generated_at(json.loads(row["payload_json"]))) == payload_hash: return ForecastWriteResult(0, 1, 0)
            connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)", (record_type, instrument_key, None, "CONFLICTING_EXECUTION_RECORD", canonical_json(record), utc_iso(datetime.now(timezone.utc))))
            return ForecastWriteResult(0, 0, 1)
        placeholders = ",".join("?" for _ in columns)
        connection.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})", values + (canonical_json(record), utc_iso(record.generated_at), record.schema_version))
        return ForecastWriteResult(1, 0, 0)

    def save_order_intent(self, intent: OrderIntent) -> ForecastWriteResult:
        return self._save_execution_record(table="order_intents", id_column="intent_id", record=intent, columns=("intent_id","event_key","instrument_key","risk_bundle_id","plan_id","decision_id","profile","action","state","requested_shares","payload_json","generated_at","schema_version"), values=(intent.intent_id,intent.event_key,intent.instrument.stable_key,intent.risk_bundle_id,intent.plan_id,intent.decision_id,intent.profile.value,intent.action.value,intent.state.value,str(intent.requested_shares)), record_type="order_intent_conflict", instrument_key=intent.instrument.stable_key)

    def save_order_intent_build_record(self, record: OrderIntentBuildRecord) -> ForecastWriteResult:
        # build record 没有独立 event_key，使用 build_id 作为稳定审计事件键。
        payload = json.loads(canonical_json(record)); payload["event_key"] = record.build_id
        with self._transaction() as connection:
            row=connection.execute("SELECT payload_json FROM order_intent_build_records WHERE build_id=? OR event_key=?",(record.build_id,record.build_id)).fetchone()
            if row:
                old=json.loads(row["payload_json"]); old.pop("event_key",None)
                if stable_hash(_without_generated_at(old))==stable_hash(_without_generated_at(json.loads(canonical_json(record)))): return ForecastWriteResult(0,1,0)
                connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)",("order_intent_build_record_conflict",None,None,"CONFLICTING_EXECUTION_RECORD",canonical_json(record),utc_iso(datetime.now(timezone.utc)))); return ForecastWriteResult(0,0,1)
            connection.execute("INSERT INTO order_intent_build_records(build_id,event_key,decision_id,plan_id,status,intent_id,payload_json,generated_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?)",(record.build_id,record.build_id,record.decision_id,record.plan_id,record.status.value,record.intent_id,canonical_json(record),utc_iso(record.generated_at),record.schema_version))
        return ForecastWriteResult(1,0,0)

    def save_trigger_evaluation(self, value: TriggerEvaluation) -> ForecastWriteResult:
        return self._save_execution_record(table="trigger_evaluations", id_column="trigger_evaluation_id", record=value, columns=("trigger_evaluation_id","event_key","intent_id","state","triggered_at","evidence_grade","payload_json","generated_at","schema_version"), values=(value.trigger_evaluation_id,value.event_key,value.intent_id,value.state.value,utc_iso(value.triggered_at) if value.triggered_at else None,value.evidence_grade.value), record_type="trigger_evaluation_conflict", instrument_key=None)

    def save_execution_run(self, run: ExecutionRun) -> ForecastWriteResult:
        return self._save_execution_record(table="execution_runs", id_column="run_id", record=run, columns=("run_id","event_key","intent_id","mode","initial_state_hash","event_batch_hash","outcome","evidence_grade","payload_json","generated_at","schema_version"), values=(run.run_id,run.run_id,run.intent_id,run.mode.value,run.initial_state_hash,run.event_batch_hash,run.outcome.value,run.evidence_grade.value), record_type="execution_run_conflict", instrument_key=None)

    def save_fill_evidence(self, fill: FillEvidence) -> ForecastWriteResult:
        return self._save_execution_record(table="fill_evidence", id_column="fill_id", record=fill, columns=("fill_id","event_key","run_id","intent_id","instrument_key","outcome","filled_at","payload_json","generated_at","schema_version"), values=(fill.fill_id,fill.event_key,fill.run_id,fill.intent_id,fill.instrument.stable_key,fill.outcome.value,utc_iso(fill.filled_at) if fill.filled_at else None), record_type="fill_evidence_conflict", instrument_key=fill.instrument.stable_key)

    def save_execution_result(self, run: ExecutionRun, fills: tuple[FillEvidence, ...]) -> tuple[ForecastWriteResult, tuple[ForecastWriteResult, ...]]:
        """保存一次回放前复核 run/fill 的双向引用。"""
        if (tuple(sorted(item.fill_id for item in fills)) != run.fill_ids or any(item.run_id != run.run_id or item.intent_id != run.intent_id or item.market_rule_version != run.market_rule_version or item.execution_policy_version != run.execution_policy_version for item in fills)):
            raise ContractViolation("execution run and fills have inconsistent references")
        try:
            with self._transaction() as connection:
                run_result = self._save_execution_record(table="execution_runs",id_column="run_id",record=run,columns=("run_id","event_key","intent_id","mode","initial_state_hash","event_batch_hash","outcome","evidence_grade","payload_json","generated_at","schema_version"),values=(run.run_id,run.run_id,run.intent_id,run.mode.value,run.initial_state_hash,run.event_batch_hash,run.outcome.value,run.evidence_grade.value),record_type="execution_run_conflict",instrument_key=None,connection=connection)
                fill_results = tuple(self._save_execution_record(table="fill_evidence",id_column="fill_id",record=item,columns=("fill_id","event_key","run_id","intent_id","instrument_key","outcome","filled_at","payload_json","generated_at","schema_version"),values=(item.fill_id,item.event_key,item.run_id,item.intent_id,item.instrument.stable_key,item.outcome.value,utc_iso(item.filled_at) if item.filled_at else None),record_type="fill_evidence_conflict",instrument_key=item.instrument.stable_key,connection=connection) for item in fills)
                conflicts = ((run,) if run_result.conflicts else ()) + tuple(item for item, result in zip(fills, fill_results) if result.conflicts)
                if conflicts:
                    raise _ExecutionBatchConflict(conflicts)
                return run_result, fill_results
        except _ExecutionBatchConflict as conflict:
            # 主事务已经回滚；单独重放冲突对象，只保存 quarantine，不留下半条 run/fill。
            for item in conflict.records:
                if isinstance(item, ExecutionRun): self.save_execution_run(item)
                else: self.save_fill_evidence(item)
            return ForecastWriteResult(0,0,1), tuple(ForecastWriteResult(0,0,1) for _ in fills)

    @staticmethod
    def _execution_expression(payload):
        def operand(value): return None if value is None else ConditionOperand(OperandKind(value["kind"]), value["key"], value["value"], value["unit"], tuple(value["source_features"]))
        def expression(value): return None if value is None else ConditionExpression(value["condition_id"],ConditionOperator(value["operator"]),operand(value["left"]),operand(value["right"]),operand(value["lower"]),operand(value["upper"]),tuple(expression(item) for item in value["children"]),EvidenceRequirement(value["evidence_requirement"]),value["reason_code"],int(value.get("schema_version",1)))
        return expression(payload)

    @classmethod
    def _intent_from_payload(cls, payload: dict) -> OrderIntent:
        instrument=InstrumentId(payload["instrument"]["code"],Market(payload["instrument"]["market"]),Exchange(payload["instrument"]["exchange"]))
        money=lambda name: Decimal(payload[name]) if payload[name] is not None else None
        def evaluation(item):
            def observed(value):
                raw=value["status"]
                try: status=FeatureStatus(raw)
                except ValueError: status=ConditionResult(raw)
                return ObservedValue(value["key"],value["value"],status,_parse_datetime(value["available_at"]) if value["available_at"] else None)
            return ConditionEvaluation(item["condition_id"],ConditionResult(item["result"]),tuple(observed(value) for value in item["observed_values"]),tuple(item["missing_features"]),_parse_datetime(item["evaluated_at"]))
        return OrderIntent(payload["intent_id"],payload["event_key"],instrument,payload["scenario_id"],payload["strategy_bundle_id"],payload["risk_bundle_id"],payload["plan_id"],payload["decision_id"],RiskProfile(payload["profile"]),PlanAction(payload["action"]),QuantityIntent(payload["quantity_intent"]),OrderSide(payload["side"]),OrderStyle(payload["order_style"]),IntentState(payload["state"]),Decimal(payload["requested_shares"]),Decimal(payload["risk_approved_shares"]),cls._execution_expression(payload["trigger_condition"]),cls._execution_expression(payload["confirmation_condition"]),cls._execution_expression(payload["invalidation_condition"]),tuple(evaluation(item) for item in payload["condition_evaluations"]),money("trigger_level"),money("stop"),money("take_profit"),_parse_datetime(payload["valid_from"]),_parse_datetime(payload["expires_at"]),_parse_datetime(payload["earliest_execution_at"]),payload["account_hash"],payload["valuation_id"],payload["quality_hash"],payload["evidence_hash"],payload["market_rule_version"],payload["risk_policy_version"],payload["execution_policy_version"],_parse_datetime(payload["generated_at"]),int(payload.get("schema_version",1)))

    def get_order_intent(self, intent_id: str) -> OrderIntent | None:
        row=self._fetchone("SELECT * FROM order_intents WHERE intent_id=?",(intent_id,))
        if row is None:return None
        value=self._intent_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.event_key,"instrument_key":value.instrument.stable_key,"risk_bundle_id":value.risk_bundle_id,"plan_id":value.plan_id,"decision_id":value.decision_id,"profile":value.profile.value,"action":value.action.value,"state":value.state.value,"requested_shares":str(value.requested_shares),"generated_at":utc_iso(value.generated_at),"schema_version":value.schema_version}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored order intent columns do not match payload")
        return value

    def list_order_intents(self, instrument: InstrumentId, risk_bundle_id: str | None = None) -> tuple[OrderIntent,...]:
        sql="SELECT intent_id FROM order_intents WHERE instrument_key=?"; params=(instrument.stable_key,)
        if risk_bundle_id is not None: sql+=" AND risk_bundle_id=?"; params+=(risk_bundle_id,)
        return tuple(self.get_order_intent(item["intent_id"]) for item in self._fetchall(sql+" ORDER BY generated_at,intent_id",params))

    def get_order_intent_build_record(self, build_id: str) -> OrderIntentBuildRecord | None:
        row=self._fetchone("SELECT * FROM order_intent_build_records WHERE build_id=?",(build_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"]); value=OrderIntentBuildRecord(payload["build_id"],payload["decision_id"],payload["plan_id"],IntentBuildStatus(payload["status"]),payload["intent_id"],tuple(payload["reasons"]),_parse_datetime(payload["generated_at"]),int(payload.get("schema_version",1)))
        if row["event_key"]!=value.build_id or row["decision_id"]!=value.decision_id or row["plan_id"]!=value.plan_id or row["status"]!=value.status.value or row["intent_id"]!=value.intent_id: raise ContractViolation("stored intent build record columns do not match payload")
        return value

    def list_order_intent_build_records(self, decision_id: str) -> tuple[OrderIntentBuildRecord,...]:
        return tuple(self.get_order_intent_build_record(item["build_id"]) for item in self._fetchall("SELECT build_id FROM order_intent_build_records WHERE decision_id=? ORDER BY generated_at,build_id",(decision_id,)))

    @staticmethod
    def _trigger_from_payload(payload: dict) -> TriggerEvaluation:
        return TriggerEvaluation(payload["trigger_evaluation_id"],payload["event_key"],payload["intent_id"],TriggerState(payload["state"]),tuple(payload["evaluated_event_ids"]),payload["event_batch_hash"],payload["trigger_event_id"],payload["invalidation_event_id"],_parse_datetime(payload["evaluated_from"]) if payload["evaluated_from"] else None,_parse_datetime(payload["evaluated_to"]) if payload["evaluated_to"] else None,_parse_datetime(payload["triggered_at"]) if payload["triggered_at"] else None,_parse_datetime(payload["invalidated_at"]) if payload["invalidated_at"] else None,payload["source"],EventGranularity(payload["granularity"]) if payload["granularity"] else None,PathAssumption(payload["path_assumption"]),ExecutionEvidenceGrade(payload["evidence_grade"]),payload["execution_policy_version"],tuple(payload["reason_codes"]),_parse_datetime(payload["generated_at"]),int(payload.get("schema_version",1)))

    def get_trigger_evaluation(self, trigger_evaluation_id: str) -> TriggerEvaluation | None:
        row=self._fetchone("SELECT * FROM trigger_evaluations WHERE trigger_evaluation_id=?",(trigger_evaluation_id,))
        if row is None:return None
        value=self._trigger_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.event_key,"intent_id":value.intent_id,"state":value.state.value,"triggered_at":utc_iso(value.triggered_at) if value.triggered_at else None,"evidence_grade":value.evidence_grade.value,"generated_at":utc_iso(value.generated_at),"schema_version":value.schema_version}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored trigger evaluation columns do not match payload")
        return value

    def list_trigger_evaluations(self, intent_id: str) -> tuple[TriggerEvaluation,...]:
        return tuple(self.get_trigger_evaluation(item["trigger_evaluation_id"]) for item in self._fetchall("SELECT trigger_evaluation_id FROM trigger_evaluations WHERE intent_id=? ORDER BY generated_at,trigger_evaluation_id",(intent_id,)))

    @staticmethod
    def _fill_from_payload(payload: dict) -> FillEvidence:
        instrument=InstrumentId(payload["instrument"]["code"],Market(payload["instrument"]["market"]),Exchange(payload["instrument"]["exchange"]))
        money=lambda name: Decimal(payload[name]) if payload[name] is not None else None
        return FillEvidence(payload["fill_id"],payload["event_key"],payload["run_id"],payload["intent_id"],payload["decision_id"],payload["plan_id"],instrument,PlanAction(payload["action"]),OrderSide(payload["side"]),FillOutcome(payload["outcome"]),Decimal(payload["requested_shares"]),Decimal(payload["filled_shares"]),Decimal(payload["unfilled_shares"]),money("raw_price"),money("slippage_rate"),money("fill_price"),money("gross_value"),money("commission"),money("sell_tax"),money("total_fee"),money("cash_delta"),_parse_datetime(payload["triggered_at"]) if payload["triggered_at"] else None,_parse_datetime(payload["filled_at"]) if payload["filled_at"] else None,payload["source"],EventGranularity(payload["granularity"]) if payload["granularity"] else None,PathAssumption(payload["path_assumption"]),ExecutionEvidenceGrade(payload["evidence_grade"]),payload["market_rule_version"],payload["execution_policy_version"],tuple(payload["reason_codes"]),_parse_datetime(payload["generated_at"]),int(payload.get("schema_version",1)))

    def get_fill_evidence(self, fill_id: str) -> FillEvidence | None:
        row=self._fetchone("SELECT * FROM fill_evidence WHERE fill_id=?",(fill_id,))
        if row is None:return None
        value=self._fill_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.event_key,"run_id":value.run_id,"intent_id":value.intent_id,"instrument_key":value.instrument.stable_key,"outcome":value.outcome.value,"filled_at":utc_iso(value.filled_at) if value.filled_at else None,"generated_at":utc_iso(value.generated_at),"schema_version":value.schema_version}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored fill evidence columns do not match payload")
        return value

    def list_fill_evidence(self, run_id: str) -> tuple[FillEvidence,...]:
        return tuple(self.get_fill_evidence(item["fill_id"]) for item in self._fetchall("SELECT fill_id FROM fill_evidence WHERE run_id=? ORDER BY fill_id",(run_id,)))

    @staticmethod
    def _run_from_payload(payload: dict) -> ExecutionRun:
        delta=payload["final_state_delta"]
        state_delta=ExecutionStateDelta(Decimal(delta["cash_delta"]),Decimal(delta["position_delta"]),Decimal(delta["sellable_delta"]) if delta["sellable_delta"] is not None else None,Decimal(delta["average_cost"]) if delta["average_cost"] is not None else None,Decimal(delta["active_stop"]) if delta["active_stop"] is not None else None,Decimal(delta["active_take_profit"]) if delta["active_take_profit"] is not None else None,tuple(delta["reason_codes"]),date.fromisoformat(delta["acquired_session_date"]) if delta.get("acquired_session_date") else None)
        return ExecutionRun(payload["run_id"],payload["intent_id"],ExecutionMode(payload["mode"]),payload["initial_state_hash"],payload["event_batch_hash"],_parse_datetime(payload["replay_as_of"]),payload["market_rule_version"],payload["execution_policy_version"],payload["trigger_evaluation_id"],tuple(payload["fill_ids"]),state_delta,FillOutcome(payload["outcome"]),ExecutionEvidenceGrade(payload["evidence_grade"]),tuple(payload["reason_codes"]),_parse_datetime(payload["generated_at"]),int(payload.get("schema_version",1)))

    def get_execution_run(self, run_id: str) -> ExecutionRun | None:
        row=self._fetchone("SELECT * FROM execution_runs WHERE run_id=?",(run_id,))
        if row is None:return None
        value=self._run_from_payload(json.loads(row["payload_json"]))
        fills=self.list_fill_evidence(value.run_id)
        expected={"event_key":value.run_id,"intent_id":value.intent_id,"mode":value.mode.value,"initial_state_hash":value.initial_state_hash,"event_batch_hash":value.event_batch_hash,"outcome":value.outcome.value,"evidence_grade":value.evidence_grade.value,"generated_at":utc_iso(value.generated_at),"schema_version":value.schema_version}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored execution run columns do not match payload")
        if tuple(item.fill_id for item in fills)!=value.fill_ids or any(item.intent_id!=value.intent_id or item.market_rule_version!=value.market_rule_version or item.execution_policy_version!=value.execution_policy_version for item in fills):raise ContractViolation("stored execution run and fills do not have bidirectional references")
        return value

    def list_execution_runs(self, intent_id: str) -> tuple[ExecutionRun,...]:
        return tuple(self.get_execution_run(item["run_id"]) for item in self._fetchall("SELECT run_id FROM execution_runs WHERE intent_id=? ORDER BY generated_at,run_id",(intent_id,)))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """统一事务边界：任意异常回滚，成功才提交，且受可重入锁保护。"""
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _fetchall(self, sql: str, parameters: tuple[object, ...]) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchall()

    def _fetchone(self, sql: str, parameters: tuple[object, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchone()

    def upsert_daily_bars(self, bars: Iterable[CanonicalBar]) -> DailyBarWriteResult:
        materialized = tuple(bars)
        if any(not isinstance(bar, CanonicalBar) for bar in materialized):
            raise ContractViolation("daily bar batch must contain CanonicalBar objects")
        # Reconstruct every object before opening the transaction.  This protects
        # the repository from deserialized or intentionally bypassed dataclass
        # construction and guarantees all-or-nothing batch validation.
        try:
            validated = tuple(
                CanonicalBar(
                    instrument=bar.instrument,
                    trading_date=bar.trading_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    adjustment_mode=bar.adjustment_mode,
                    source=bar.source,
                    fetched_at=bar.fetched_at,
                    corporate_action_version=bar.corporate_action_version,
                    schema_version=bar.schema_version,
                )
                for bar in materialized
            )
        except ContractViolation as exc:
            raise ContractViolation(f"daily bar batch validation failed: {exc}") from exc
        hashes = [(bar, _bar_hash(bar)) for bar in validated]
        inserted = idempotent = conflicts = 0
        with self._transaction() as connection:
            for bar, payload_hash in hashes:
                row = connection.execute(
                    """SELECT payload_hash FROM daily_bars
                       WHERE instrument_key=? AND trading_date=? AND adjustment_mode=?""",
                    (bar.instrument.stable_key, bar.trading_date.isoformat(), bar.adjustment_mode.value),
                ).fetchone()
                if row is not None:
                    if row["payload_hash"] == payload_hash:
                        idempotent += 1
                        continue
                    conflicts += 1
                    connection.execute(
                        """INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            "daily_bar_conflict",
                            bar.instrument.stable_key,
                            bar.trading_date.isoformat(),
                            "CONFLICTING_DUPLICATE_BAR",
                            canonical_json(bar.to_dict()),
                            utc_iso(datetime.now(timezone.utc)),
                        ),
                    )
                    continue
                connection.execute(
                    """INSERT INTO daily_bars(
                           instrument_key, code, market, exchange, trading_date, adjustment_mode,
                           open, high, low, close, volume, source, fetched_at,
                           corporate_action_version, payload_hash, schema_version
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bar.instrument.stable_key,
                        bar.instrument.code,
                        bar.instrument.market.value,
                        bar.instrument.exchange.value,
                        bar.trading_date.isoformat(),
                        bar.adjustment_mode.value,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.source,
                        utc_iso(bar.fetched_at),
                        bar.corporate_action_version,
                        payload_hash,
                        bar.schema_version,
                    ),
                )
                inserted += 1
        return DailyBarWriteResult(inserted=inserted, idempotent=idempotent, conflicts=conflicts)

    def list_daily_bars(
        self,
        instrument: InstrumentId,
        start: date,
        end: date,
        adjustment_mode: AdjustmentMode = AdjustmentMode.FRONT_ADJUSTED,
    ) -> tuple[CanonicalBar, ...]:
        rows = self._fetchall(
            """SELECT * FROM daily_bars
               WHERE instrument_key=? AND adjustment_mode=? AND trading_date BETWEEN ? AND ?
               ORDER BY trading_date""",
            (instrument.stable_key, adjustment_mode.value, start.isoformat(), end.isoformat()),
        )
        return tuple(
            CanonicalBar(
                instrument=_instrument_from_row(row),
                trading_date=date.fromisoformat(row["trading_date"]),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                adjustment_mode=AdjustmentMode(row["adjustment_mode"]),
                source=row["source"],
                fetched_at=_parse_datetime(row["fetched_at"]),
                corporate_action_version=row["corporate_action_version"],
                schema_version=row["schema_version"],
            )
            for row in rows
        )

    def quarantine_daily_bars(self, instrument: InstrumentId, before_date: date, reason: str) -> int:
        rows = self._fetchall(
            """SELECT * FROM daily_bars
               WHERE instrument_key=? AND trading_date < ?""",
            (instrument.stable_key, before_date.isoformat()),
        )
        if not rows:
            return 0
        with self._transaction() as connection:
            for row in rows:
                connection.execute(
                    """INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "daily_bar",
                        instrument.stable_key,
                        row["trading_date"],
                        reason,
                        canonical_json(dict(row)),
                        utc_iso(datetime.now(timezone.utc)),
                    ),
                )
            connection.execute(
                "DELETE FROM daily_bars WHERE instrument_key=? AND trading_date < ?",
                (instrument.stable_key, before_date.isoformat()),
            )
        return len(rows)

    def quarantine_received_daily_bars(
        self,
        bars: Iterable[CanonicalBar],
        reason: str,
    ) -> int:
        """Audit rejected provider bars without ever admitting them to daily_bars."""
        materialized = tuple(bars)
        if any(not isinstance(bar, CanonicalBar) for bar in materialized):
            raise ContractViolation("quarantined daily bars must contain CanonicalBar objects")
        if not materialized:
            return 0
        with self._transaction() as connection:
            for bar in materialized:
                connection.execute(
                    """INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "daily_bar_rejected",
                        bar.instrument.stable_key,
                        bar.trading_date.isoformat(),
                        reason,
                        canonical_json(bar.to_dict()),
                        utc_iso(datetime.now(timezone.utc)),
                    ),
                )
        return len(materialized)

    def upsert_intraday_bars(self, bars: Iterable[IntradayBar]) -> int:
        materialized = tuple(bars)
        if any(not isinstance(bar, IntradayBar) for bar in materialized):
            raise ContractViolation("intraday bar batch must contain IntradayBar objects")
        with self._transaction() as connection:
            for bar in materialized:
                connection.execute(
                    """INSERT OR REPLACE INTO intraday_bars(
                           instrument_key, code, market, exchange, observed_at, session_date,
                           open, high, low, close, volume, source, evidence_quality, fetched_at, schema_version
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bar.instrument.stable_key,
                        bar.instrument.code,
                        bar.instrument.market.value,
                        bar.instrument.exchange.value,
                        utc_iso(bar.observed_at),
                        bar.session_date.isoformat(),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.source,
                        bar.evidence_quality,
                        utc_iso(bar.fetched_at),
                        bar.schema_version,
                    ),
                )
        return len(materialized)

    def list_intraday_bars(self, instrument: InstrumentId, start_at: datetime, end_at: datetime) -> tuple[IntradayBar, ...]:
        rows = self._fetchall(
            """SELECT * FROM intraday_bars
               WHERE instrument_key=? AND observed_at BETWEEN ? AND ? ORDER BY observed_at""",
            (instrument.stable_key, utc_iso(start_at), utc_iso(end_at)),
        )
        return tuple(
            IntradayBar(
                instrument=_instrument_from_row(row),
                observed_at=_parse_datetime(row["observed_at"]),
                session_date=date.fromisoformat(row["session_date"]),
                open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                volume=row["volume"], source=row["source"], evidence_quality=row["evidence_quality"],
                fetched_at=_parse_datetime(row["fetched_at"]), schema_version=row["schema_version"],
            )
            for row in rows
        )

    def save_quote_snapshot(self, quote: QuoteSnapshot) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO quote_snapshots(
                       instrument_key, code, market, exchange, session, price, prev_close, open, high, low,
                       volume, bid, ask, observed_at, fetched_at, source, freshness_status, schema_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    quote.instrument.stable_key, quote.instrument.code, quote.instrument.market.value,
                    quote.instrument.exchange.value, quote.session.value, quote.price, quote.prev_close,
                    quote.open, quote.high, quote.low, quote.volume, quote.bid, quote.ask,
                    utc_iso(quote.observed_at), utc_iso(quote.fetched_at), quote.source,
                    quote.freshness_status.value, quote.schema_version,
                ),
            )

    def get_latest_quote(self, instrument: InstrumentId, session: TradingSession) -> QuoteSnapshot | None:
        row = self._fetchone(
            """SELECT * FROM quote_snapshots WHERE instrument_key=? AND session=?
               ORDER BY observed_at DESC LIMIT 1""",
            (instrument.stable_key, session.value),
        )
        if row is None:
            return None
        return QuoteSnapshot(
            instrument=_instrument_from_row(row), session=TradingSession(row["session"]), price=row["price"],
            prev_close=row["prev_close"], open=row["open"], high=row["high"], low=row["low"],
            volume=row["volume"], bid=row["bid"], ask=row["ask"],
            observed_at=_parse_datetime(row["observed_at"]), fetched_at=_parse_datetime(row["fetched_at"]),
            source=row["source"], freshness_status=FreshnessStatus(row["freshness_status"]),
            schema_version=row["schema_version"],
        )

    def upsert_stock_metadata(self, metadata: StockMetadata) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO stock_metadata(
                       instrument_key, code, market, exchange, name, industry, description, listing_date,
                       source, fetched_at, schema_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    metadata.instrument.stable_key, metadata.instrument.code, metadata.instrument.market.value,
                    metadata.instrument.exchange.value, metadata.name, metadata.industry, metadata.description,
                    metadata.listing_date.isoformat() if metadata.listing_date else None, metadata.source,
                    utc_iso(metadata.fetched_at), metadata.schema_version,
                ),
            )

    def get_stock_metadata(self, instrument: InstrumentId) -> StockMetadata | None:
        row = self._fetchone(
            "SELECT * FROM stock_metadata WHERE instrument_key=?", (instrument.stable_key,)
        )
        if row is None:
            return None
        return StockMetadata(
            instrument=_instrument_from_row(row), name=row["name"], industry=row["industry"],
            description=row["description"], listing_date=date.fromisoformat(row["listing_date"]) if row["listing_date"] else None,
            source=row["source"], fetched_at=_parse_datetime(row["fetched_at"]), schema_version=row["schema_version"],
        )

    def upsert_news(self, items: Iterable[NewsSnapshot]) -> int:
        materialized = tuple(items)
        if any(not isinstance(item, NewsSnapshot) for item in materialized):
            raise ContractViolation("news batch must contain NewsSnapshot objects")
        with self._transaction() as connection:
            for item in materialized:
                connection.execute(
                    """INSERT INTO news_snapshots(
                           stable_key, instrument_key, code, market, exchange, title, source, published_at,
                           available_at, fetched_at, content, is_macro, finbert_label, finbert_score,
                           relevance, schema_version
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(stable_key) DO UPDATE SET
                           available_at=MIN(news_snapshots.available_at, excluded.available_at),
                           fetched_at=MAX(news_snapshots.fetched_at, excluded.fetched_at),
                           content=COALESCE(excluded.content, news_snapshots.content),
                           is_macro=MAX(news_snapshots.is_macro, excluded.is_macro),
                           finbert_label=COALESCE(excluded.finbert_label, news_snapshots.finbert_label),
                           finbert_score=COALESCE(excluded.finbert_score, news_snapshots.finbert_score),
                           relevance=COALESCE(excluded.relevance, news_snapshots.relevance),
                           schema_version=MAX(news_snapshots.schema_version, excluded.schema_version)""",
                    (
                        item.stable_key, item.instrument.stable_key, item.instrument.code, item.instrument.market.value,
                        item.instrument.exchange.value, item.title, item.source, utc_iso(item.published_at),
                        utc_iso(item.available_at), utc_iso(item.fetched_at), item.content, int(item.is_macro),
                        item.finbert_label, item.finbert_score, item.relevance, item.schema_version,
                    ),
                )
        return len(materialized)

    def list_news_as_of(self, instrument: InstrumentId, as_of: datetime) -> tuple[NewsSnapshot, ...]:
        rows = self._fetchall(
            """SELECT * FROM news_snapshots WHERE instrument_key=? AND available_at <= ?
               ORDER BY available_at, published_at""",
            (instrument.stable_key, utc_iso(as_of)),
        )
        return tuple(
            NewsSnapshot(
                instrument=_instrument_from_row(row), title=row["title"], source=row["source"],
                published_at=_parse_datetime(row["published_at"]), available_at=_parse_datetime(row["available_at"]),
                fetched_at=_parse_datetime(row["fetched_at"]), content=row["content"], is_macro=bool(row["is_macro"]),
                finbert_label=row["finbert_label"], finbert_score=row["finbert_score"], relevance=row["relevance"],
                schema_version=row["schema_version"],
            )
            for row in rows
        )

    def upsert_fundamental_snapshot(self, snapshot: FundamentalSnapshot) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO fundamental_snapshots(
                       instrument_key, code, market, exchange, fields_json, available_at, fetched_at,
                       provider, quality_status, schema_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.instrument.stable_key, snapshot.instrument.code, snapshot.instrument.market.value,
                    snapshot.instrument.exchange.value, _fundamental_payload(snapshot), utc_iso(snapshot.available_at),
                    utc_iso(snapshot.fetched_at), snapshot.provider, snapshot.quality_status.value,
                    snapshot.schema_version,
                ),
            )

    def get_fundamentals_as_of(self, instrument: InstrumentId, as_of: datetime) -> FundamentalSnapshot | None:
        row = self._fetchone(
            """SELECT * FROM fundamental_snapshots WHERE instrument_key=? AND available_at <= ?
               ORDER BY available_at DESC LIMIT 1""",
            (instrument.stable_key, utc_iso(as_of)),
        )
        if row is None:
            return None
        raw_fields = json.loads(row["fields_json"])
        fields = {
            name: FundamentalValue(
                value=value["value"], unit=value["unit"],
                period_end=date.fromisoformat(value["period_end"]) if value["period_end"] else None,
                published_at=_parse_datetime(value["published_at"]) if value["published_at"] else None,
                source=value["source"],
            )
            for name, value in raw_fields.items()
        }
        return FundamentalSnapshot(
            instrument=_instrument_from_row(row), fields=fields, available_at=_parse_datetime(row["available_at"]),
            fetched_at=_parse_datetime(row["fetched_at"]), provider=row["provider"],
            quality_status=QualityStatus(row["quality_status"]), schema_version=row["schema_version"],
        )

    def save_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO account_snapshots(market, currency, cash, captured_at, schema_version)
                   VALUES (?, ?, ?, ?, ?)""",
                (snapshot.market.value, snapshot.currency, str(snapshot.cash), utc_iso(snapshot.captured_at), snapshot.schema_version),
            )
            row = connection.execute(
                "SELECT id FROM account_snapshots WHERE market=? AND captured_at=?",
                (snapshot.market.value, utc_iso(snapshot.captured_at)),
            ).fetchone()
            snapshot_id = int(row["id"])
            connection.execute("DELETE FROM account_positions WHERE account_snapshot_id=?", (snapshot_id,))
            for position in snapshot.positions:
                connection.execute(
                    """INSERT INTO account_positions(
                           account_snapshot_id, instrument_key, code, market, exchange, shares, cost_price, captured_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id, position.instrument.stable_key, position.instrument.code,
                        position.instrument.market.value, position.instrument.exchange.value,
                        str(position.shares), str(position.cost_price), utc_iso(position.captured_at),
                    ),
                )

    def get_latest_account_snapshot(self, market: Market) -> AccountSnapshot | None:
        row = self._fetchone(
            "SELECT * FROM account_snapshots WHERE market=? ORDER BY captured_at DESC LIMIT 1", (market.value,)
        )
        if row is None:
            return None
        positions_rows = self._fetchall(
            "SELECT * FROM account_positions WHERE account_snapshot_id=? ORDER BY instrument_key", (row["id"],)
        )
        positions = tuple(
            PositionSnapshot(
                instrument=_instrument_from_row(position), shares=Decimal(position["shares"]),
                cost_price=Decimal(position["cost_price"]), captured_at=_parse_datetime(position["captured_at"]),
            )
            for position in positions_rows
        )
        return AccountSnapshot(
            market=Market(row["market"]), currency=row["currency"], cash=Decimal(row["cash"]),
            positions=positions, captured_at=_parse_datetime(row["captured_at"]), schema_version=row["schema_version"],
        )

    def migration_preflight(self, source_path: Path | str, as_of: datetime) -> MigrationPreflight:
        source = Path(source_path)
        if not source.exists():
            return MigrationPreflight(
                source_path=str(source), source_exists=False, source_schema_detected=False,
                table_counts={}, migratable_counts={}, conflict_counts={},
                warnings=("V1 database does not exist",), read_only=True, evaluated_at=as_of,
            )
        connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        try:
            table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            tables = {row[0] for row in table_rows}
            known = ("holdings", "watchlist", "account_balance", "price_history", "stocks")
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in known if table in tables
            }
            conflicts: dict[str, int] = {}
            if {"price_history", "stocks"}.issubset(tables):
                try:
                    conflicts["before_listing_date"] = int(
                        connection.execute(
                            """SELECT COUNT(*) FROM price_history p JOIN stocks s ON s.code=p.code
                               WHERE s.listing_date IS NOT NULL AND s.listing_date != '' AND p.date < s.listing_date"""
                        ).fetchone()[0]
                    )
                except sqlite3.DatabaseError:
                    conflicts["before_listing_date"] = 0
            return MigrationPreflight(
                source_path=str(source), source_exists=True, source_schema_detected=bool(tables.intersection(known)),
                table_counts=counts,
                migratable_counts={
                    "holdings": counts.get("holdings", 0),
                    "watchlist": counts.get("watchlist", 0),
                    "account_balance": counts.get("account_balance", 0),
                },
                conflict_counts=conflicts,
                warnings=(), read_only=True, evaluated_at=as_of,
            )
        finally:
            connection.close()
