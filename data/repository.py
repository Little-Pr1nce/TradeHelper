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

from contracts.account import AccountSnapshot, PositionSnapshot
from contracts.analysis import FeatureEvidenceMode, FeatureSnapshot, FeatureStatus, FeatureValue
from contracts.enums import (
    AdjustmentMode,
    DecisionMode,
    Exchange,
    FreshnessStatus,
    Market,
    ProviderStatus,
    QualityStatus,
    TradingSession,
)
from contracts.market_data import (
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
from contracts.providers import DailyBarsRequest, MigrationPreflight
from contracts.forecast import (
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
from contracts.scenario import (BandSignal, CurrentOverlay, CurrentPriceState, DecisionSession, EntryPosture, ExitPosture, ForecastEvidenceGrade, ForecastSupportLevel, HorizonAlignment, HorizonAssessment, HorizonSignal, NewsDeltaState, PriceLocation, ScenarioBias, ScenarioState, ScenarioStatus, StrategyFamily, TradingScenario, VolatilityShock)
from contracts.strategy import (ConditionEvaluation, ConditionExpression, ConditionOperand, ConditionOperator, ConditionResult, DerivedPriceLevel, EvidenceRequirement, ObservedValue, OperandKind, PlanAction, PlanProfile, PlanReadiness, PositionState, QuantityIntent, StopMode, StopSpec, StrategyBranch, StrategyBundle, TakeProfitMode, TakeProfitSpec, TradePlan)
from contracts.risk import (ConstraintResult, DecisionDisposition, EvidenceStatus, ExecutionDecision, ExecutionLevel, FrozenAccountValuation, InstrumentClassification, MarketEligibility, MarketRuleSet, PlanEvidenceSnapshot, PositionValuation, RiskAdjustment, RiskConstraintKind, RiskDecisionBundle, RiskPolicy, RiskProfile, ValuationStatus)
from contracts.execution import (ExecutionEvidenceGrade, ExecutionMode, ExecutionRun, ExecutionStateDelta, EventGranularity, FillEvidence, FillOutcome, IntentBuildStatus, IntentState, OrderIntent, OrderIntentBuildRecord, OrderSide, OrderStyle, PathAssumption, TriggerEvaluation, TriggerState)
from contracts.portfolio import (AllocationStatus, CorrelationPair, CorrelationStatus, HoldingRiskSnapshot, HoldingRiskStatus, InstrumentReturnRisk, PortfolioAllocation, PortfolioCandidate, PortfolioCorrelationSnapshot, PortfolioDecisionBundle, PortfolioEvidenceGrade, PortfolioHeatStatus, PortfolioInputBatch, PortfolioPolicy, PortfolioProfileDecision, PortfolioReservationGroup, PortfolioReservationSnapshot, PortfolioRiskSnapshot, PortfolioRole, ReplacementCandidate, ReplacementStatus)
from contracts.learning import CandidateKind, CandidateLifecycle, CandidateScope, EvidenceOrigin, ForecastOutcome, JointOutcome, JointOutcomeKind, LearningCandidateVersion, LearningEvidenceGrade, LearningMetricSnapshot, LearningRun, LearningRunStatus, LedgerKind, MaturityEvidence, OutcomeStatus, PromotionDecision, PromotionEvent, ScenarioOutcome, StrategyOutcome
from contracts.research import (CandidateEligibility, HypothesisCandidateLink, HypothesisKind, HypothesisNovelty, HypothesisOutcome, HypothesisOutcomeStatus, HypothesisValidation, HypothesisValidationStatus, RawResearchResponse, ResearchContext, ResearchFact, ResearchFactManifest, ResearchHypothesis, ResearchMetricSnapshot, ResearchScope)
from contracts.presentation import (ChartKind, ChartSpec, ExportFormat, ExportStatus,
    MetricDefinition, ReportBlock, ReportBlockKind, ReportDocument, ReportExportArtifact,
    ReportFeedback, ReportHistoryQuery, ReportHistoryPage, ReportKind, ReportSection,
    ReportSeverity, ReportSnapshot, ReportTable, ReportTableRow, WatchlistSnapshot)
from contracts.migration import MigrationItem, MigrationPlan, MigrationRun
from contracts.runtime import AnalysisRunResult, AnalysisRunStatus, ReportRevisionLink, report_revision_invariant
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
class SimpleNamespaceFoldRecord:
    """仅用于把 FoldDefinition 与其所属 run 的持久化元数据绑定。"""
    run_id: str
    fold: object
    generated_at: datetime

    @property
    def fold_id(self) -> str:
        return self.fold.fold_id


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
        if version.lifecycle is not ModelLifecycle.CHAMPION or version.validation_status not in {
            ValidationStatus.CONFIRMATION_PASSED, ValidationStatus.NONINFERIOR_PASSED,
        }:
            raise ContractViolation("only confirmed champion can be promoted")
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
            connection.execute("UPDATE forecast_model_versions SET lifecycle='champion', validation_status=?, promoted_at=? WHERE version=?", (version.validation_status.value, utc_iso(version.promoted_at or datetime.now(timezone.utc)), version.version))
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

    def save_forecast_validation_summary(
        self, *, market: Market, scope_key: str, horizon: int, status: ValidationStatus,
        reason: str | None, data_hash: str, created_at: datetime,
    ) -> ForecastWriteResult:
        if horizon not in {1, 3, 5, 10} or len(data_hash) != 64:
            raise ContractViolation("invalid forecast validation summary")
        validation_id = stable_hash({
            "market": market, "scope_key": scope_key, "horizon": horizon,
            "status": status, "reason": reason, "data_hash": data_hash,
        })
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT status, reason FROM forecast_validation_summaries WHERE market=? AND scope_key=? AND horizon=? AND data_hash=?",
                (market.value, scope_key, horizon, data_hash),
            ).fetchone()
            if existing is not None:
                if existing["status"] == status.value and existing["reason"] == reason:
                    return ForecastWriteResult(0, 1, 0)
                return ForecastWriteResult(0, 0, 1)
            connection.execute(
                """INSERT INTO forecast_validation_summaries
                   (validation_id, market, scope_key, horizon, status, reason, data_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (validation_id, market.value, scope_key, horizon, status.value, reason, data_hash, utc_iso(created_at)),
            )
        return ForecastWriteResult(1, 0, 0)

    def list_latest_forecast_validations(self) -> tuple[dict[str, object], ...]:
        """Restore the latest stock/horizon OOF verdict after an app restart."""
        rows = self._fetchall(
            """SELECT market, scope_key, horizon, status, reason, data_hash, created_at
               FROM forecast_validation_summaries ORDER BY created_at DESC""",
            (),
        )
        latest = {}
        for row in rows:
            payload = dict(row)
            payload["created_at"] = _parse_datetime(payload["created_at"])
            key = (payload["market"], payload["scope_key"], payload["horizon"])
            if key not in latest:
                latest[key] = payload
        return tuple(latest[key] for key in sorted(latest, key=lambda item: (str(item[0]), str(item[1]), int(item[2]))))

    def save_forecast_candidate_evaluation(
        self, *, market: Market, scope: ForecastScope, scope_key: str, horizon: int,
        spec_id: str, phase: str, data_hash: str, payload: Mapping[str, object],
        created_at: datetime,
    ) -> ForecastWriteResult:
        """Persist preregistered candidate OOF metrics without inventing a model version."""
        if horizon not in {1, 3, 5, 10} or phase not in {"selection", "confirmation"} or len(data_hash) != 64:
            raise ContractViolation("invalid forecast candidate evaluation")
        payload_json = canonical_json(payload)
        identity = {
            "market": market, "scope": scope, "scope_key": scope_key,
            "horizon": horizon, "spec_id": spec_id, "phase": phase,
            "data_hash": data_hash,
        }
        candidate_evaluation_id = stable_hash(identity)
        with self._transaction() as connection:
            existing = connection.execute(
                """SELECT payload_json FROM forecast_candidate_evaluations
                   WHERE market=? AND scope=? AND scope_key=? AND horizon=?
                   AND spec_id=? AND phase=? AND data_hash=?""",
                (market.value, scope.value, scope_key, horizon, spec_id, phase, data_hash),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] == payload_json:
                    return ForecastWriteResult(0, 1, 0)
                return ForecastWriteResult(0, 0, 1)
            connection.execute(
                """INSERT INTO forecast_candidate_evaluations
                   (candidate_evaluation_id, market, scope, scope_key, horizon, spec_id,
                    phase, data_hash, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (candidate_evaluation_id, market.value, scope.value, scope_key, horizon,
                 spec_id, phase, data_hash, payload_json, utc_iso(created_at)),
            )
        return ForecastWriteResult(1, 0, 0)

    def list_forecast_candidate_evaluations(
        self, *, market: Market, scope_key: str, horizon: int,
    ) -> tuple[dict[str, object], ...]:
        rows = self._fetchall(
            """SELECT scope, spec_id, phase, data_hash, payload_json, created_at
               FROM forecast_candidate_evaluations
               WHERE market=? AND scope_key=? AND horizon=?
               ORDER BY created_at, spec_id, phase""",
            (market.value, scope_key, horizon),
        )
        return tuple({
            "scope": row["scope"], "spec_id": row["spec_id"], "phase": row["phase"],
            "data_hash": row["data_hash"], "payload": json.loads(row["payload_json"]),
            "created_at": _parse_datetime(row["created_at"]),
        } for row in rows)

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
            label_policy_version=str(payload.get("label_policy_version", "direction_v1_vol_scaled")),
            label_flat_band=float(payload["label_flat_band"]) if payload.get("label_flat_band") is not None else None,
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

    def _save_portfolio_record(self, *, table: str, id_column: str, record, event_key: str, columns: tuple[str, ...], values: tuple[object, ...], record_type: str, instrument_key: str | None, connection: sqlite3.Connection | None = None) -> ForecastWriteResult:
        """V2-8 的通用幂等/隔离写入；generated_at 不影响业务等价性。"""
        if connection is None:
            with self._transaction() as active:
                return self._save_portfolio_record(table=table, id_column=id_column, record=record, event_key=event_key, columns=columns, values=values, record_type=record_type, instrument_key=instrument_key, connection=active)
        payload = json.loads(canonical_json(record))
        row = connection.execute(f"SELECT payload_json FROM {table} WHERE {id_column}=? OR event_key=?", (getattr(record, id_column), event_key)).fetchone()
        if row:
            if stable_hash(_without_generated_at(json.loads(row["payload_json"]))) == stable_hash(_without_generated_at(payload)):
                return ForecastWriteResult(0, 1, 0)
            connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)", (record_type, instrument_key, None, "CONFLICTING_PORTFOLIO_RECORD", canonical_json(record), utc_iso(datetime.now(timezone.utc))))
            return ForecastWriteResult(0, 0, 1)
        placeholders = ",".join("?" for _ in columns)
        connection.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})", values + (canonical_json(record), utc_iso(record.generated_at), getattr(record, "schema_version", 1)))
        return ForecastWriteResult(1, 0, 0)

    def save_portfolio_input_batch(self, batch: PortfolioInputBatch) -> ForecastWriteResult:
        return self._save_portfolio_record(table="portfolio_input_batches", id_column="batch_id", record=batch, event_key=batch.batch_id,
            columns=("batch_id","event_key","market","currency","mode","account_hash","valuation_id","as_of","policy_version","payload_json","generated_at","schema_version"),
            values=(batch.batch_id,batch.batch_id,batch.market.value,batch.currency,batch.mode.value,stable_hash(batch.account_snapshot),batch.valuation.valuation_id,utc_iso(batch.as_of),batch.portfolio_policy.policy_version), record_type="portfolio_batch_conflict", instrument_key=None)

    def _save_portfolio_bundle(self, bundle: PortfolioDecisionBundle, *, connection: sqlite3.Connection | None = None) -> ForecastWriteResult:
        return self._save_portfolio_record(table="portfolio_decision_bundles", id_column="portfolio_bundle_id", record=bundle, event_key=bundle.portfolio_bundle_id,
            columns=("portfolio_bundle_id","event_key","batch_id","market","account_hash","valuation_id","policy_version","payload_json","generated_at","schema_version"),
            values=(bundle.portfolio_bundle_id,bundle.portfolio_bundle_id,bundle.batch_id,bundle.market.value,bundle.account_hash,bundle.valuation_id,bundle.portfolio_policy_version), record_type="portfolio_bundle_conflict", instrument_key=None, connection=connection)

    def save_portfolio_result(self, batch: PortfolioInputBatch, bundle: PortfolioDecisionBundle) -> tuple[ForecastWriteResult, ForecastWriteResult]:
        """一次事务保存批次与完整 bundle；子集合不完整时不留下半条数据。"""
        if (bundle.batch_id != batch.batch_id or bundle.market is not batch.market
                or bundle.valuation_id != batch.valuation.valuation_id
                or bundle.account_hash != batch.valuation.account_hash
                or bundle.account_hash != stable_hash(batch.account_snapshot)
                or bundle.portfolio_policy_version != batch.portfolio_policy.policy_version):
            raise ContractViolation("portfolio batch and bundle mismatch")
        records = [item for profile in (bundle.conservative, bundle.aggressive) for item in profile.allocations]
        groups = [item for profile in (bundle.conservative, bundle.aggressive) for item in profile.reservation_groups]
        replacements = [item for profile in (bundle.conservative, bundle.aggressive) for item in profile.replacement_candidates]
        if (len({item.allocation_id for item in records}) != len(records)
                or len({item.group_id for item in groups}) != len(groups)
                or len({item.replacement_id for item in replacements}) != len(replacements)):
            raise ContractViolation("portfolio child ids are not unique")

        batch_spec = dict(table="portfolio_input_batches", id_column="batch_id", record=batch,
            event_key=batch.batch_id,
            columns=("batch_id","event_key","market","currency","mode","account_hash","valuation_id","as_of","policy_version","payload_json","generated_at","schema_version"),
            values=(batch.batch_id,batch.batch_id,batch.market.value,batch.currency,batch.mode.value,stable_hash(batch.account_snapshot),batch.valuation.valuation_id,utc_iso(batch.as_of),batch.portfolio_policy.policy_version),
            record_type="portfolio_batch_conflict", instrument_key=None)
        bundle_spec = dict(table="portfolio_decision_bundles", id_column="portfolio_bundle_id", record=bundle,
            event_key=bundle.portfolio_bundle_id,
            columns=("portfolio_bundle_id","event_key","batch_id","market","account_hash","valuation_id","policy_version","payload_json","generated_at","schema_version"),
            values=(bundle.portfolio_bundle_id,bundle.portfolio_bundle_id,bundle.batch_id,bundle.market.value,bundle.account_hash,bundle.valuation_id,bundle.portfolio_policy_version),
            record_type="portfolio_bundle_conflict", instrument_key=None)
        child_specs = []
        for item in records:
            child_specs.append(dict(table="portfolio_allocations",id_column="allocation_id",record=item,event_key=item.allocation_id,
                columns=("allocation_id","event_key","portfolio_bundle_id","batch_id","profile","instrument_key","decision_id","action","status","final_requested_shares","payload_json","generated_at","schema_version"),
                values=(item.allocation_id,item.allocation_id,bundle.portfolio_bundle_id,item.batch_id,item.profile.value,item.instrument.stable_key,item.decision_id,item.action.value,item.status.value,str(item.final_requested_shares)),record_type="portfolio_allocation_conflict",instrument_key=item.instrument.stable_key))
        for item in groups:
            child_specs.append(dict(table="portfolio_reservation_groups",id_column="group_id",record=item,event_key=item.group_id,
                columns=("group_id","event_key","portfolio_bundle_id","profile","instrument_key","side","max_aggregate_shares","payload_json","generated_at","schema_version"),
                values=(item.group_id,item.group_id,bundle.portfolio_bundle_id,item.profile.value,item.instrument.stable_key,item.side,str(item.max_aggregate_shares)),record_type="portfolio_group_conflict",instrument_key=item.instrument.stable_key))
        for item in replacements:
            child_specs.append(dict(table="portfolio_replacement_candidates",id_column="replacement_id",record=item,event_key=item.replacement_id,
                columns=("replacement_id","event_key","portfolio_bundle_id","profile","source_instrument_key","target_instrument_key","status","payload_json","generated_at","schema_version"),
                values=(item.replacement_id,item.replacement_id,bundle.portfolio_bundle_id,item.profile.value,item.source_instrument.stable_key,item.target_instrument.stable_key,item.status.value),record_type="portfolio_replacement_conflict",instrument_key=item.target_instrument.stable_key))
        try:
            with self._transaction() as connection:
                batch_result = self._save_portfolio_record(**batch_spec, connection=connection)
                bundle_result = self._save_portfolio_record(**bundle_spec, connection=connection)
                child_results = [self._save_portfolio_record(**spec, connection=connection) for spec in child_specs]
                conflict_specs = tuple(spec for spec, result in zip(
                    (batch_spec, bundle_spec, *child_specs),
                    (batch_result, bundle_result, *child_results),
                ) if result.conflicts)
                if conflict_specs:
                    raise _ExecutionBatchConflict(conflict_specs)
                return batch_result, bundle_result
        except _ExecutionBatchConflict as conflict:
            # 主事务已回滚；只重放真正冲突的记录以写入 quarantine。
            for spec in conflict.records:
                self._save_portfolio_record(**spec)
            return ForecastWriteResult(0,0,1), ForecastWriteResult(0,0,1)

    @staticmethod
    def _portfolio_instrument(payload: dict) -> InstrumentId:
        return InstrumentId(payload["code"], Market(payload["market"]), Exchange(payload["exchange"]))

    @classmethod
    def _portfolio_candidate_from_payload(cls, payload: dict) -> PortfolioCandidate:
        rule = payload["market_rules"]
        market_rules = MarketRuleSet(rule["rule_version"], Market(rule["market"]), Exchange(rule["exchange"]), Decimal(rule["lot_size"]), bool(rule["same_day_sell_restricted"]), Decimal(rule["commission_rate"]), Decimal(rule["minimum_commission"]), Decimal(rule["sell_tax_rate"]), Decimal(rule["base_slippage_reserve"]), rule["price_limit_pct"], InstrumentClassification(rule["instrument_classification"]), rule["source"], _parse_datetime(rule["effective_from"]), _parse_datetime(rule["effective_to"]) if rule["effective_to"] else None)
        evidence_raw = payload["plan_evidence"]
        evidence = None if evidence_raw is None else PlanEvidenceSnapshot(evidence_raw["evidence_id"], cls._portfolio_instrument(evidence_raw["instrument"]), evidence_raw["strategy_id"], evidence_raw["strategy_version"], evidence_raw["parameter_hash"], RiskProfile(evidence_raw["profile"]) if evidence_raw["profile"] else None, int(evidence_raw["sample_count"]), int(evidence_raw["oof_sample_count"]), evidence_raw["expected_net_return"], evidence_raw["confidence_low"], evidence_raw["confidence_high"], evidence_raw["win_rate"], evidence_raw["max_adverse_excursion"], EvidenceStatus(evidence_raw["status"]), evidence_raw["source_ledger_version"], _parse_datetime(evidence_raw["data_cutoff_at"]), _parse_datetime(evidence_raw["evaluated_at"]), _parse_datetime(evidence_raw["generated_at"]))
        return PortfolioCandidate(payload["candidate_id"], PortfolioRole(payload["role"]), cls._scenario_from_payload(payload["trading_scenario"]), cls._plan_from_payload(payload["trade_plan"]), cls._decision_from_payload(payload["execution_decision"]), evidence, market_rules, _parse_datetime(payload["generated_at"]), int(payload.get("schema_version", 1)))

    @classmethod
    def _portfolio_batch_from_payload(cls, payload: dict) -> PortfolioInputBatch:
        account_raw = payload["account_snapshot"]
        account = AccountSnapshot(Market(account_raw["market"]), account_raw["currency"], Decimal(account_raw["cash"]), tuple(PositionSnapshot(cls._portfolio_instrument(item["instrument"]), Decimal(item["shares"]), Decimal(item["cost_price"]), _parse_datetime(item["captured_at"])) for item in account_raw["positions"]), _parse_datetime(account_raw["captured_at"]), int(account_raw.get("schema_version", 1)))
        policy_raw = payload["risk_policy"]
        risk_policy = RiskPolicy(**{key: Decimal(value) if key.endswith(("_pct", "_cap", "_multiplier", "_fraction")) else value for key, value in policy_raw.items()})
        pp = payload["portfolio_policy"]
        portfolio_policy = PortfolioPolicy(**{key: Decimal(value) if key in {"conservative_heat_cap","aggressive_heat_cap","absolute_heat_hard_cap","high_correlation_threshold","high_correlation_group_cap","hhi_warning","unknown_correlation_multiplier"} else value for key,value in pp.items()})
        risks = tuple(HoldingRiskSnapshot(item["holding_risk_id"], cls._portfolio_instrument(item["instrument"]), Decimal(item["shares"]), Decimal(item["reference_price"]) if item["reference_price"] is not None else None, Decimal(item["market_value"]) if item["market_value"] is not None else None, Decimal(item["stop_price"]) if item["stop_price"] is not None else None, Decimal(item["exit_friction_reserve"]), Decimal(item["planned_loss_amount"]) if item["planned_loss_amount"] is not None else None, HoldingRiskStatus(item["status"]), item["source_plan_id"], item["source_decision_id"], _parse_datetime(item["captured_at"]), _parse_datetime(item["generated_at"]), int(item.get("schema_version", 1))) for item in payload["holding_risks"])
        corr = payload["correlation_snapshot"]
        correlation = PortfolioCorrelationSnapshot(corr["correlation_snapshot_id"], Market(corr["market"]), tuple(cls._portfolio_instrument(item) for item in corr["universe"]), tuple(InstrumentReturnRisk(cls._portfolio_instrument(item["instrument"]), int(item["sample_count"]), date.fromisoformat(item["start_session_date"]) if item["start_session_date"] else None, date.fromisoformat(item["end_session_date"]) if item["end_session_date"] else None, Decimal(item["annualized_volatility"]) if item["annualized_volatility"] is not None else None, item["adjustment_mode"], item["source_bar_hash"]) for item in corr["instrument_risks"]), tuple(CorrelationPair(cls._portfolio_instrument(item["left"]), cls._portfolio_instrument(item["right"]), Decimal(item["coefficient"]) if item["coefficient"] is not None else None, int(item["overlapping_samples"]), CorrelationStatus(item["status"])) for item in corr["pairs"]), int(corr["lookback_sessions"]), int(corr["minimum_samples"]), corr["return_method"], int(corr["annualization_sessions"]), _parse_datetime(corr["cutoff_at"]), CorrelationStatus(corr["status"]), corr["source_batch_hash"], _parse_datetime(corr["generated_at"]))
        return PortfolioInputBatch(payload["batch_id"], Market(payload["market"]), payload["currency"], DecisionMode(payload["mode"]), account, cls._valuation_from_payload(payload["valuation"]), risk_policy, portfolio_policy, tuple(cls._risk_bundle_from_payload(item) for item in payload["risk_bundles"]), tuple(cls._portfolio_candidate_from_payload(item) for item in payload["candidates"]), tuple(cls._portfolio_instrument(item) for item in payload["watchlist"]), risks, correlation, _parse_datetime(payload["as_of"]), _parse_datetime(payload["generated_at"]), int(payload.get("schema_version", 1)))

    def get_portfolio_input_batch(self, batch_id: str) -> PortfolioInputBatch | None:
        row = self._fetchone("SELECT * FROM portfolio_input_batches WHERE batch_id=?", (batch_id,))
        if row is None: return None
        value = self._portfolio_batch_from_payload(json.loads(row["payload_json"]))
        expected = {"event_key": value.batch_id, "market": value.market.value, "currency": value.currency, "mode": value.mode.value, "account_hash": stable_hash(value.account_snapshot), "valuation_id": value.valuation.valuation_id, "as_of": utc_iso(value.as_of), "policy_version": value.portfolio_policy.policy_version, "generated_at": utc_iso(value.generated_at), "schema_version": value.schema_version}
        if any(row[key] != item for key,item in expected.items()): raise ContractViolation("stored portfolio batch columns do not match payload")
        return value

    def list_portfolio_input_batches(self, market: Market) -> tuple[PortfolioInputBatch, ...]:
        return tuple(self.get_portfolio_input_batch(row["batch_id"]) for row in self._fetchall("SELECT batch_id FROM portfolio_input_batches WHERE market=? ORDER BY as_of,batch_id", (market.value,)))

    @classmethod
    def _portfolio_allocation_from_payload(cls, item: dict) -> PortfolioAllocation:
        money = lambda key: Decimal(item[key]) if item[key] is not None else None
        return PortfolioAllocation(item["allocation_id"], item["batch_id"], RiskProfile(item["profile"]), item["candidate_id"], cls._portfolio_instrument(item["instrument"]), item["plan_id"], item["decision_id"], PlanAction(item["action"]), item["level"], AllocationStatus(item["status"]), item["rank"], tuple(tuple(value) for value in item["rank_components"]), Decimal(item["approved_shares"]), Decimal(item["final_requested_shares"]), money("current_position_value"), money("reference_entry_price"), Decimal(item["reserved_cash"]), Decimal(item["reserved_incremental_loss"]), money("estimated_position_pct"), item["reservation_group_id"], tuple(item["binding_constraints"]), tuple(item["reason_codes"]), _parse_datetime(item["generated_at"]))

    @classmethod
    def _portfolio_risk_snapshot_from_payload(cls, item: dict) -> PortfolioRiskSnapshot:
        money = lambda key: Decimal(item[key]) if item[key] is not None else None
        pairs = tuple(CorrelationPair(cls._portfolio_instrument(pair["left"]), cls._portfolio_instrument(pair["right"]), Decimal(pair["coefficient"]) if pair["coefficient"] is not None else None, int(pair["overlapping_samples"]), CorrelationStatus(pair["status"])) for pair in item["high_correlation_pairs"])
        return PortfolioRiskSnapshot(item["risk_snapshot_id"], Market(item["market"]), item["valuation_id"], Decimal(item["equity"]), Decimal(item["cash"]), Decimal(item["invested_value"]), Decimal(item["invested_pct"]), tuple((cls._portfolio_instrument(pair[0]), Decimal(pair[1])) for pair in item["weights_by_instrument"]), cls._portfolio_instrument(item["max_position_instrument"]) if item["max_position_instrument"] else None, Decimal(item["max_position_pct"]), Decimal(item["hhi"]), money("portfolio_annualized_volatility"), money("planned_loss_amount"), money("planned_loss_pct"), pairs, PortfolioHeatStatus(item["heat_status"]), PortfolioEvidenceGrade(item["evidence_grade"]), tuple(item["reason_codes"]), _parse_datetime(item["calculated_at"]))

    @classmethod
    def _portfolio_profile_from_payload(cls, item: dict) -> PortfolioProfileDecision:
        groups = tuple(PortfolioReservationGroup(group["group_id"], group["batch_id"], RiskProfile(group["profile"]), cls._portfolio_instrument(group["instrument"]), group["side"], tuple(group["member_allocation_ids"]), Decimal(group["max_aggregate_shares"]), group["consumption_policy"], tuple(group["reason_codes"]), _parse_datetime(group["generated_at"]), int(group.get("schema_version", 1))) for group in item["reservation_groups"])
        allocations = tuple(cls._portfolio_allocation_from_payload(value) for value in item["allocations"])
        reservation = item["reservation_snapshot"]
        snapshot = PortfolioReservationSnapshot(RiskProfile(reservation["profile"]), Decimal(reservation["frozen_equity"]), Decimal(reservation["frozen_cash"]), Decimal(reservation["deployable_cash"]), Decimal(reservation["reserved_entry_cash"]), Decimal(reservation["remaining_cash"]), Decimal(reservation["reserved_entry_notional"]), Decimal(reservation["projected_invested_pct_at_reference_price"]), Decimal(reservation["current_planned_loss"]) if reservation["current_planned_loss"] is not None else None, Decimal(reservation["reserved_incremental_loss"]), Decimal(reservation["projected_heat_pct"]) if reservation["projected_heat_pct"] is not None else None, Decimal(reservation["exit_release_estimate"]), PortfolioEvidenceGrade(reservation["evidence_grade"]), tuple(reservation["reason_codes"]))
        replacements = tuple(ReplacementCandidate(value["replacement_id"], RiskProfile(value["profile"]), cls._portfolio_instrument(value["source_instrument"]), value["source_exit_allocation_id"], cls._portfolio_instrument(value["target_instrument"]), value["target_entry_allocation_id"], ReplacementStatus(value["status"]), tuple(value["source_exit_reason_codes"]), tuple(tuple(pair) for pair in value["target_rank_components"]), Decimal(value["estimated_release_amount"]), Decimal(value["target_required_cash"]), Decimal(value["funding_shortfall_after_current_cash"]), bool(value["reanalysis_required"]), tuple(value["reason_codes"]), _parse_datetime(value["generated_at"]), int(value.get("schema_version", 1))) for value in item["replacement_candidates"])
        return PortfolioProfileDecision(item["profile_decision_id"], item["batch_id"], RiskProfile(item["profile"]), allocations, groups, tuple(item["holding_priority_allocation_ids"]), tuple(item["entry_priority_allocation_ids"]), tuple(item["blocked_allocation_ids"]), cls._portfolio_risk_snapshot_from_payload(item["current_risk_snapshot"]), snapshot, replacements, PortfolioEvidenceGrade(item["evidence_grade"]), tuple(item["reason_codes"]), _parse_datetime(item["generated_at"]))

    @classmethod
    def _portfolio_bundle_from_payload(cls, item: dict) -> PortfolioDecisionBundle:
        return PortfolioDecisionBundle(item["portfolio_bundle_id"], item["batch_id"], Market(item["market"]), item["account_hash"], item["valuation_id"], cls._portfolio_profile_from_payload(item["conservative"]), cls._portfolio_profile_from_payload(item["aggressive"]), item["portfolio_policy_version"], _parse_datetime(item["generated_at"]), int(item.get("schema_version", 1)))

    def get_portfolio_decision_bundle(self, portfolio_bundle_id: str) -> PortfolioDecisionBundle | None:
        row = self._fetchone("SELECT * FROM portfolio_decision_bundles WHERE portfolio_bundle_id=?", (portfolio_bundle_id,))
        if row is None: return None
        value = self._portfolio_bundle_from_payload(json.loads(row["payload_json"]))
        expected = {"event_key": value.portfolio_bundle_id, "batch_id": value.batch_id, "market": value.market.value, "account_hash": value.account_hash, "valuation_id": value.valuation_id, "policy_version": value.portfolio_policy_version, "generated_at": utc_iso(value.generated_at), "schema_version": value.schema_version}
        if any(row[key] != item for key,item in expected.items()): raise ContractViolation("stored portfolio bundle columns do not match payload")
        # 子表既是查询索引也是防损坏的双向集合校验。
        allocation_rows = self._fetchall("SELECT * FROM portfolio_allocations WHERE portfolio_bundle_id=?", (portfolio_bundle_id,))
        group_rows = self._fetchall("SELECT * FROM portfolio_reservation_groups WHERE portfolio_bundle_id=?", (portfolio_bundle_id,))
        replacement_rows = self._fetchall("SELECT * FROM portfolio_replacement_candidates WHERE portfolio_bundle_id=?", (portfolio_bundle_id,))
        expected_allocations = {item.allocation_id:item for profile in (value.conservative,value.aggressive) for item in profile.allocations}
        expected_groups = {item.group_id:item for profile in (value.conservative,value.aggressive) for item in profile.reservation_groups}
        expected_replacements = {item.replacement_id:item for profile in (value.conservative,value.aggressive) for item in profile.replacement_candidates}
        if ({row["allocation_id"] for row in allocation_rows} != set(expected_allocations)
                or {row["group_id"] for row in group_rows} != set(expected_groups)
                or {row["replacement_id"] for row in replacement_rows} != set(expected_replacements)):
            raise ContractViolation("stored portfolio child collections are inconsistent")
        for child_row in allocation_rows:
            child=self._portfolio_allocation_from_payload(json.loads(child_row["payload_json"])); expected_child=expected_allocations[child_row["allocation_id"]]
            expected_columns={"event_key":child.allocation_id,"portfolio_bundle_id":portfolio_bundle_id,"batch_id":child.batch_id,"profile":child.profile.value,"instrument_key":child.instrument.stable_key,"decision_id":child.decision_id,"action":child.action.value,"status":child.status.value,"final_requested_shares":str(child.final_requested_shares),"generated_at":utc_iso(child.generated_at),"schema_version":1}
            if child!=expected_child or any(child_row[key]!=item for key,item in expected_columns.items()): raise ContractViolation("stored portfolio allocation does not match bundle")
        for child_row in group_rows:
            child_payload=json.loads(child_row["payload_json"]); child=PortfolioReservationGroup(child_payload["group_id"],child_payload["batch_id"],RiskProfile(child_payload["profile"]),self._portfolio_instrument(child_payload["instrument"]),child_payload["side"],tuple(child_payload["member_allocation_ids"]),Decimal(child_payload["max_aggregate_shares"]),child_payload["consumption_policy"],tuple(child_payload["reason_codes"]),_parse_datetime(child_payload["generated_at"]),int(child_payload.get("schema_version",1))); expected_child=expected_groups[child_row["group_id"]]
            expected_columns={"event_key":child.group_id,"portfolio_bundle_id":portfolio_bundle_id,"profile":child.profile.value,"instrument_key":child.instrument.stable_key,"side":child.side,"max_aggregate_shares":str(child.max_aggregate_shares),"generated_at":utc_iso(child.generated_at),"schema_version":child.schema_version}
            if child!=expected_child or any(child_row[key]!=item for key,item in expected_columns.items()): raise ContractViolation("stored portfolio reservation group does not match bundle")
        for child_row in replacement_rows:
            child_payload=json.loads(child_row["payload_json"]); child=ReplacementCandidate(child_payload["replacement_id"],RiskProfile(child_payload["profile"]),self._portfolio_instrument(child_payload["source_instrument"]),child_payload["source_exit_allocation_id"],self._portfolio_instrument(child_payload["target_instrument"]),child_payload["target_entry_allocation_id"],ReplacementStatus(child_payload["status"]),tuple(child_payload["source_exit_reason_codes"]),tuple(tuple(pair) for pair in child_payload["target_rank_components"]),Decimal(child_payload["estimated_release_amount"]),Decimal(child_payload["target_required_cash"]),Decimal(child_payload["funding_shortfall_after_current_cash"]),bool(child_payload["reanalysis_required"]),tuple(child_payload["reason_codes"]),_parse_datetime(child_payload["generated_at"]),int(child_payload.get("schema_version",1))); expected_child=expected_replacements[child_row["replacement_id"]]
            expected_columns={"event_key":child.replacement_id,"portfolio_bundle_id":portfolio_bundle_id,"profile":child.profile.value,"source_instrument_key":child.source_instrument.stable_key,"target_instrument_key":child.target_instrument.stable_key,"status":child.status.value,"generated_at":utc_iso(child.generated_at),"schema_version":child.schema_version}
            if child!=expected_child or any(child_row[key]!=item for key,item in expected_columns.items()): raise ContractViolation("stored portfolio replacement does not match bundle")
        return value

    def list_portfolio_decision_bundles(self, market: Market) -> tuple[PortfolioDecisionBundle, ...]:
        return tuple(self.get_portfolio_decision_bundle(row["portfolio_bundle_id"]) for row in self._fetchall("SELECT portfolio_bundle_id FROM portfolio_decision_bundles WHERE market=? ORDER BY generated_at,portfolio_bundle_id", (market.value,)))

    def _save_learning_record(self, *, table, id_column, record, event_key, columns, values, instrument_key=None, connection=None):
        if connection is None:
            with self._transaction() as active: return self._save_learning_record(table=table,id_column=id_column,record=record,event_key=event_key,columns=columns,values=values,instrument_key=instrument_key,connection=active)
        payload=canonical_json(record); payload_hash=stable_hash(_without_generated_at(json.loads(payload)))
        row=connection.execute(f"SELECT payload_hash FROM {table} WHERE {id_column}=? OR event_key=?",(getattr(record,id_column),event_key)).fetchone()
        if row:
            if row["payload_hash"]==payload_hash: return ForecastWriteResult(0,1,0)
            connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)",("learning_conflict",instrument_key,None,"CONFLICTING_LEARNING_RECORD",payload,utc_iso(datetime.now(timezone.utc))))
            return ForecastWriteResult(0,0,1)
        generated_at=getattr(record,"generated_at",getattr(record,"created_at",None))
        if generated_at is None: raise ContractViolation("immutable record requires generated_at or created_at")
        connection.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",values+(payload_hash,payload,utc_iso(generated_at),1))
        return ForecastWriteResult(1,0,0)

    def save_watchlist_snapshot(self, snapshot: WatchlistSnapshot) -> ForecastWriteResult:
        """关注列表与成员同一事务写入；旧快照永不被原地修改。"""
        with self._transaction() as connection:
            result=self._save_learning_record(table="watchlist_snapshots",id_column="watchlist_id",record=snapshot,event_key=snapshot.watchlist_id,columns=("watchlist_id","event_key","market","payload_hash","payload_json","created_at","schema_version"),values=(snapshot.watchlist_id,snapshot.watchlist_id,snapshot.market.value),connection=connection)
            if result.conflicts:return result
            for position,instrument in enumerate(snapshot.instruments):
                connection.execute("INSERT OR IGNORE INTO watchlist_snapshot_members(watchlist_id,instrument_key,position) VALUES (?,?,?)",(snapshot.watchlist_id,instrument.stable_key,position))
            return result

    def get_watchlist_snapshot(self, watchlist_id: str) -> WatchlistSnapshot | None:
        row=self._fetchone("SELECT * FROM watchlist_snapshots WHERE watchlist_id=?",(watchlist_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"])
        instruments=tuple(InstrumentId(item["code"],Market(item["market"]),Exchange(item["exchange"])) for item in payload["instruments"])
        value=WatchlistSnapshot(payload["watchlist_id"],Market(payload["market"]),instruments,_parse_datetime(payload["created_at"]))
        members=self._fetchall("SELECT instrument_key,position FROM watchlist_snapshot_members WHERE watchlist_id=? ORDER BY position",(watchlist_id,))
        if tuple(row["instrument_key"] for row in members)!=tuple(item.stable_key for item in value.instruments) or tuple(row["position"] for row in members)!=tuple(range(len(value.instruments))):
            raise ContractViolation("stored watchlist members do not match payload")
        expected={"event_key":value.watchlist_id,"market":value.market.value,"created_at":utc_iso(value.created_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()): raise ContractViolation("stored watchlist columns do not match payload")
        return value

    def latest_watchlist_snapshot(self, market: Market) -> WatchlistSnapshot | None:
        row=self._fetchone("SELECT watchlist_id FROM watchlist_snapshots WHERE market=? ORDER BY created_at DESC,watchlist_id DESC LIMIT 1",(market.value,))
        return None if row is None else self.get_watchlist_snapshot(row["watchlist_id"])

    def save_report_document(self, document: ReportDocument) -> ForecastWriteResult:
        """报告快照先完整写入，导出失败不影响其历史可读性。"""
        return self._save_learning_record(table="report_snapshots",id_column="report_id",record=document,event_key=document.report_id,columns=("report_id","event_key","document_hash","report_kind","market","instrument_key","analysis_mode","as_of","source_refs_json","renderer_version","payload_hash","payload_json","created_at","schema_version"),values=(document.report_id,document.report_id,document.document_hash,document.report_kind.value,document.market.value,document.instrument.stable_key if document.instrument else None,document.analysis_mode.value,utc_iso(document.as_of),canonical_json(document.source_artifact_refs),document.renderer_version),instrument_key=document.instrument.stable_key if document.instrument else None)

    def archive_report(self, report_id: str, *, archived: bool=True) -> None:
        with self._transaction() as connection:
            if connection.execute("UPDATE report_snapshots SET archived=? WHERE report_id=?",(1 if archived else 0,report_id)).rowcount!=1: raise ContractViolation("report does not exist")

    def save_report_feedback(self, feedback: ReportFeedback) -> ForecastWriteResult:
        if self._fetchone("SELECT 1 FROM report_snapshots WHERE report_id=?",(feedback.report_id,)) is None: raise ContractViolation("feedback references missing report")
        return self._save_learning_record(table="report_feedback",id_column="feedback_id",record=feedback,event_key=feedback.feedback_id,columns=("feedback_id","event_key","report_id","rating","payload_hash","payload_json","created_at","schema_version"),values=(feedback.feedback_id,feedback.feedback_id,feedback.report_id,feedback.rating))

    def save_report_export(self, artifact: ReportExportArtifact) -> ForecastWriteResult:
        if self._fetchone("SELECT 1 FROM report_snapshots WHERE report_id=?",(artifact.report_id,)) is None: raise ContractViolation("export references missing report")
        return self._save_learning_record(table="report_exports",id_column="export_id",record=artifact,event_key=artifact.export_id,columns=("export_id","event_key","report_id","format","status","content_hash","error_code","payload_hash","payload_json","created_at","schema_version"),values=(artifact.export_id,artifact.export_id,artifact.report_id,artifact.format.value,artifact.status.value,artifact.content_hash,artifact.error_code))

    def list_report_history_rows(self, query: ReportHistoryQuery):
        sql="SELECT report_id,payload_json,archived FROM report_snapshots WHERE 1=1"; params=[]
        if not query.include_archived: sql+=" AND archived=0"
        if query.market: sql+=" AND market=?"; params.append(query.market.value)
        if query.report_kind: sql+=" AND report_kind=?"; params.append(query.report_kind.value)
        if query.instrument: sql+=" AND instrument_key=?"; params.append(query.instrument.stable_key)
        if query.analysis_mode: sql+=" AND analysis_mode=?"; params.append(query.analysis_mode.value)
        sql+=" ORDER BY as_of DESC,report_id DESC LIMIT ? OFFSET ?"; params.extend((query.page_size,(query.page-1)*query.page_size))
        return tuple(self._fetchall(sql,tuple(params)))

    def get_report_document_payload(self, report_id: str) -> dict | None:
        """历史页面获得冻结 JSON，不触发任何重新分析或外部查询。"""
        row=self._fetchone("SELECT payload_json,archived,document_hash FROM report_snapshots WHERE report_id=?",(report_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"])
        if stable_hash({"report_id":payload["report_id"],"sections":payload["sections"],"refs":payload["source_artifact_refs"],"renderer":payload["renderer_version"]}) != row["document_hash"]: raise ContractViolation("stored report document hash mismatch")
        payload["archived"]=bool(row["archived"])
        return payload

    @staticmethod
    def _report_document_from_payload(payload: dict) -> ReportDocument:
        """反序列化只允许 V2-11 的固定展示合同，避免历史页回算业务对象。"""
        def instrument(value):
            return None if value is None else InstrumentId(value["code"],Market(value["market"]),Exchange(value["exchange"]))
        def row(value):
            return ReportTableRow(value["row_id"],tuple(value["cells"]),value.get("severity"),tuple(value["source_artifact_refs"]))
        def table(value):
            return ReportTable(value["table_id"],value["title"],tuple(value["columns"]),tuple(row(item) for item in value["rows"]),value.get("empty_state"),value.get("interpretation"))
        def chart(value):
            return ChartSpec(value["chart_id"],ChartKind(value["chart_kind"]),value["title"],value["x_axis"],value["y_axis"],tuple((name,tuple((x,float(y)) for x,y in points)) for name,points in value["series"]),tuple((x,float(y)) for x,y in value["baseline"]),int(value["sample_count"]),None if value.get("sample_range") is None else tuple(value["sample_range"]),value["interpretation"],value.get("empty_state"))
        def block(value):
            kind=ReportBlockKind(value["kind"]); raw=value["payload"]
            return ReportBlock(kind,table(raw) if kind is ReportBlockKind.TABLE else chart(raw) if kind is ReportBlockKind.CHART else raw,tuple(value["source_artifact_refs"]))
        def section(value):
            return ReportSection(value["section_id"],value["title"],value["purpose"],value.get("severity"),tuple(block(item) for item in value["blocks"]))
        glossary=tuple(MetricDefinition(item["metric_key"],item["display_name"],item["plain_language_definition"],item["preferred_direction"],item["minimum_sample_guidance"],item.get("unit")) for item in payload["glossary_entries"])
        return ReportDocument(payload["report_id"],ReportKind(payload["report_kind"]),Market(payload["market"]),instrument(payload.get("instrument")),DecisionMode(payload["analysis_mode"]),_parse_datetime(payload["as_of"]),payload["title"],payload["subtitle"],payload["summary"],tuple(section(item) for item in payload["sections"]),glossary,tuple(payload["source_artifact_refs"]),int(payload["schema_version"]),payload["renderer_version"],_parse_datetime(payload["generated_at"]))

    def get_report_document(self, report_id: str) -> ReportDocument | None:
        payload=self.get_report_document_payload(report_id)
        if payload is None:return None
        return self._report_document_from_payload(payload)

    def get_report_snapshot(self, report_id: str) -> ReportSnapshot | None:
        row=self._fetchone("SELECT r.*,(SELECT f.rating FROM report_feedback f WHERE f.report_id=r.report_id ORDER BY f.created_at DESC,f.feedback_id DESC LIMIT 1) AS latest_rating FROM report_snapshots r WHERE r.report_id=?",(report_id,))
        if row is None:return None
        document=self.get_report_document(report_id)
        assert document is not None
        expected={"event_key":document.report_id,"document_hash":document.document_hash,"report_kind":document.report_kind.value,"market":document.market.value,"instrument_key":document.instrument.stable_key if document.instrument else None,"analysis_mode":document.analysis_mode.value,"as_of":utc_iso(document.as_of),"source_refs_json":canonical_json(document.source_artifact_refs),"renderer_version":document.renderer_version,"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()): raise ContractViolation("stored report snapshot columns do not match payload")
        return ReportSnapshot(document.report_id,row["payload_json"],row["document_hash"],document.report_kind,document.market,document.instrument,document.analysis_mode,document.as_of,document.source_artifact_refs,document.renderer_version,document.subtitle,bool(row["archived"]),_parse_datetime(row["created_at"]),None if row["latest_rating"] is None else int(row["latest_rating"]))

    def list_report_history(self, query: ReportHistoryQuery) -> ReportHistoryPage:
        where=["1=1"]; params=[]
        if not query.include_archived: where.append("r.archived=0")
        if query.market: where.append("r.market=?");params.append(query.market.value)
        if query.report_kind: where.append("r.report_kind=?");params.append(query.report_kind.value)
        if query.instrument: where.append("r.instrument_key=?");params.append(query.instrument.stable_key)
        if query.analysis_mode: where.append("r.analysis_mode=?");params.append(query.analysis_mode.value)
        if query.history_period: where.append("json_extract(r.payload_json,'$.subtitle')=?");params.append(query.history_period)
        if query.date_from: where.append("r.as_of>=?");params.append(utc_iso(query.date_from))
        if query.date_to: where.append("r.as_of<=?");params.append(utc_iso(query.date_to))
        rating="(SELECT f.rating FROM report_feedback f WHERE f.report_id=r.report_id ORDER BY f.created_at DESC,f.feedback_id DESC LIMIT 1)"
        if query.minimum_rating is not None: where.append(f"COALESCE({rating},0)>=?");params.append(query.minimum_rating)
        clause=" AND ".join(where)
        total=int(self._fetchone(f"SELECT COUNT(*) AS count FROM report_snapshots r WHERE {clause}",tuple(params))["count"])
        rows=self._fetchall(f"SELECT r.report_id FROM report_snapshots r WHERE {clause} ORDER BY r.as_of DESC,r.report_id DESC LIMIT ? OFFSET ?",tuple(params+[query.page_size,(query.page-1)*query.page_size]))
        items=tuple(self.get_report_snapshot(row["report_id"]) for row in rows)
        return ReportHistoryPage(query,items,total,query.page*query.page_size<total)

    def list_report_feedback(self, report_id: str) -> tuple[ReportFeedback,...]:
        rows=self._fetchall("SELECT payload_json FROM report_feedback WHERE report_id=? ORDER BY created_at,feedback_id",(report_id,))
        return tuple(ReportFeedback(item["feedback_id"],item["report_id"],int(item["rating"]),item.get("note"),_parse_datetime(item["created_at"])) for item in (json.loads(row["payload_json"]) for row in rows))

    def save_maturity_evidence(self, evidence: MaturityEvidence) -> ForecastWriteResult:
        columns=("evidence_id","event_key","instrument_key","target_session_date","status","revision","supersedes_id","origin_session_date","payload_hash","payload_json","generated_at","schema_version")
        values=(evidence.evidence_id,evidence.evidence_id,evidence.instrument.stable_key,evidence.target_session_date.isoformat(),evidence.status.value,evidence.revision,evidence.supersedes_evidence_id,evidence.origin_session_date.isoformat())
        with self._transaction() as connection:
            result=self._save_learning_record(table="maturity_evidence",id_column="evidence_id",record=evidence,event_key=evidence.evidence_id,columns=columns,values=values,instrument_key=evidence.instrument.stable_key,connection=connection)
            if result.conflicts or evidence.supersedes_evidence_id is None: return result
            row=connection.execute("SELECT payload_json FROM maturity_evidence WHERE evidence_id=?",(evidence.supersedes_evidence_id,)).fetchone()
            if row is None: raise ContractViolation("maturity revision references missing predecessor")
            prior=self._maturity_from_payload(json.loads(row["payload_json"]))
            if (
                prior.instrument != evidence.instrument
                or prior.origin_session_date != evidence.origin_session_date
                or prior.target_session_date != evidence.target_session_date
                or prior.revision + 1 != evidence.revision
                or prior.status is OutcomeStatus.SUPERSEDED
            ):
                raise ContractViolation("maturity revision does not extend the active predecessor")
            from learning.maturity import MaturityResolver
            superseded=MaturityResolver().supersede(prior,generated_at=evidence.generated_at)
            payload=canonical_json(superseded); payload_hash=stable_hash(_without_generated_at(json.loads(payload)))
            connection.execute("UPDATE maturity_evidence SET status=?, payload_hash=?, payload_json=?, generated_at=? WHERE evidence_id=?",(superseded.status.value,payload_hash,payload,utc_iso(superseded.generated_at),superseded.evidence_id))
            return result

    @staticmethod
    def _maturity_from_payload(payload: dict) -> MaturityEvidence:
        raw=payload["instrument"]; instrument=InstrumentId(raw["code"],Market(raw["market"]),Exchange(raw["exchange"]))
        money=lambda name: Decimal(payload[name]) if payload[name] is not None else None
        return MaturityEvidence(payload["evidence_id"],instrument,date.fromisoformat(payload["origin_session_date"]),date.fromisoformat(payload["target_session_date"]),payload["reference_adjustment_mode"],Decimal(payload["reference_price"]),payload["target_bar_key"],money("target_price"),money("actual_return"),ForecastDirection(payload["actual_direction"]) if payload["actual_direction"] else None,Decimal(payload["flat_band"]),payload["bar_source"],payload["bar_payload_hash"],_parse_datetime(payload["bar_fetched_at"]) if payload["bar_fetched_at"] else None,_parse_datetime(payload["available_at"]) if payload["available_at"] else None,_parse_datetime(payload["evaluated_at"]),OutcomeStatus(payload["status"]),LearningEvidenceGrade(payload["evidence_grade"]),int(payload["revision"]),payload["supersedes_evidence_id"],tuple(payload["reason_codes"]),_parse_datetime(payload["generated_at"]))

    def get_maturity_evidence(self, evidence_id: str) -> MaturityEvidence | None:
        row=self._fetchone("SELECT * FROM maturity_evidence WHERE evidence_id=?",(evidence_id,))
        if row is None:return None
        value=self._maturity_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.evidence_id,"instrument_key":value.instrument.stable_key,"origin_session_date":value.origin_session_date.isoformat(),"target_session_date":value.target_session_date.isoformat(),"status":value.status.value,"revision":value.revision,"supersedes_id":value.supersedes_evidence_id,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()): raise ContractViolation("stored maturity evidence columns do not match payload")
        return value

    def list_active_maturity_evidence(self, instrument: InstrumentId) -> tuple[MaturityEvidence,...]:
        """每个 origin/target 只返回最新 revision，旧修订不会双计入 ledger。"""
        rows=self._fetchall("""SELECT m.evidence_id FROM maturity_evidence m
            WHERE m.instrument_key=? AND m.revision=(SELECT MAX(x.revision) FROM maturity_evidence x
            WHERE x.instrument_key=m.instrument_key AND x.origin_session_date=m.origin_session_date
            AND x.target_session_date=m.target_session_date)
            AND m.status!='superseded' ORDER BY m.origin_session_date,m.target_session_date,m.evidence_id""",(instrument.stable_key,))
        return tuple(self.get_maturity_evidence(row["evidence_id"]) for row in rows)

    def save_forecast_outcome(self, outcome: ForecastOutcome) -> ForecastWriteResult:
        return self._save_learning_record(table="forecast_outcomes",id_column="forecast_outcome_id",record=outcome,event_key=outcome.forecast_outcome_id,columns=("forecast_outcome_id","event_key","instrument_key","horizon","evidence_origin","status","maturity_evidence_id","payload_hash","payload_json","generated_at","schema_version"),values=(outcome.forecast_outcome_id,outcome.forecast_outcome_id,outcome.instrument.stable_key,outcome.horizon,outcome.evidence_origin.value,outcome.status.value,outcome.maturity_evidence_id),instrument_key=outcome.instrument.stable_key)

    @staticmethod
    def _forecast_outcome_from_payload(payload:dict)->ForecastOutcome:
        raw=payload["instrument"]; instrument=InstrumentId(raw["code"],Market(raw["market"]),Exchange(raw["exchange"])); probabilities=None if payload["probabilities"] is None else DirectionProbabilities(**payload["probabilities"])
        money=lambda key: Decimal(payload[key]) if payload[key] is not None else None
        return ForecastOutcome(payload["forecast_outcome_id"],payload["forecast_event_key"],instrument,date.fromisoformat(payload["origin_session_date"]),date.fromisoformat(payload["target_session_date"]),int(payload["horizon"]),ForecastScope(payload["model_scope"]),payload["scope_key"],payload["model_family"],payload["model_version"],payload["feature_set_id"],payload["model_input_hash"],payload["training_data_hash"],EvidenceOrigin(payload["evidence_origin"]),payload["maturity_evidence_id"],ForecastDirection(payload["predicted_direction"]) if payload["predicted_direction"] else None,probabilities,payload["predicted_p10"],payload["predicted_p50"],payload["predicted_p90"],ForecastDirection(payload["actual_direction"]) if payload["actual_direction"] else None,money("actual_return"),money("actual_price"),payload["direction_correct"],payload["event_brier"],payload["event_log_loss"],payload["interval_hit"],payload["absolute_return_error"],payload["market_regime_key"],OutcomeStatus(payload["status"]),LearningEvidenceGrade(payload["evidence_grade"]),tuple(payload["reason_codes"]),_parse_datetime(payload["evaluated_at"]),_parse_datetime(payload["generated_at"]))

    def get_forecast_outcome(self,outcome_id:str)->ForecastOutcome|None:
        row=self._fetchone("SELECT * FROM forecast_outcomes WHERE forecast_outcome_id=?",(outcome_id,))
        if row is None:return None
        value=self._forecast_outcome_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.forecast_outcome_id,"instrument_key":value.instrument.stable_key,"horizon":value.horizon,"evidence_origin":value.evidence_origin.value,"status":value.status.value,"maturity_evidence_id":value.maturity_evidence_id,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored forecast outcome columns do not match payload")
        return value

    def list_forecast_outcomes(self, instrument: InstrumentId, *, origin: EvidenceOrigin | None=None) -> tuple[ForecastOutcome,...]:
        sql="SELECT forecast_outcome_id FROM forecast_outcomes WHERE instrument_key=?"; values=[instrument.stable_key]
        if origin is not None: sql+=" AND evidence_origin=?"; values.append(origin.value)
        sql+=" ORDER BY generated_at,forecast_outcome_id"
        return tuple(self.get_forecast_outcome(row["forecast_outcome_id"]) for row in self._fetchall(sql,tuple(values)))

    def save_scenario_outcome(self, outcome: ScenarioOutcome) -> ForecastWriteResult:
        return self._save_learning_record(table="scenario_outcomes",id_column="scenario_outcome_id",record=outcome,event_key=outcome.scenario_outcome_id,columns=("scenario_outcome_id","event_key","instrument_key","status","payload_hash","payload_json","generated_at","schema_version"),values=(outcome.scenario_outcome_id,outcome.scenario_outcome_id,outcome.instrument.stable_key,outcome.status.value),instrument_key=outcome.instrument.stable_key)

    @staticmethod
    def _scenario_outcome_from_payload(payload: dict) -> ScenarioOutcome:
        raw=payload["instrument"]; instrument=InstrumentId(raw["code"],Market(raw["market"]),Exchange(raw["exchange"]))
        return ScenarioOutcome(payload["scenario_outcome_id"],payload["scenario_id"],instrument,tuple(payload["forecast_outcome_ids"]),payload["expected_bias"],payload["realized_bias"],payload["policy_version"],EvidenceOrigin(payload["evidence_origin"]),OutcomeStatus(payload["status"]),LearningEvidenceGrade(payload["evidence_grade"]),tuple(payload["reason_codes"]),_parse_datetime(payload["evaluated_at"]),_parse_datetime(payload["generated_at"]))

    def get_scenario_outcome(self, outcome_id: str) -> ScenarioOutcome | None:
        row=self._fetchone("SELECT * FROM scenario_outcomes WHERE scenario_outcome_id=?",(outcome_id,))
        if row is None:return None
        value=self._scenario_outcome_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.scenario_outcome_id,"instrument_key":value.instrument.stable_key,"status":value.status.value,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored scenario outcome columns do not match payload")
        return value

    def save_strategy_outcome(self, outcome: StrategyOutcome) -> ForecastWriteResult:
        return self._save_learning_record(table="strategy_outcomes",id_column="strategy_outcome_id",record=outcome,event_key=outcome.strategy_outcome_id,columns=("strategy_outcome_id","event_key","instrument_key","plan_id","status","payload_hash","payload_json","generated_at","schema_version"),values=(outcome.strategy_outcome_id,outcome.strategy_outcome_id,outcome.instrument.stable_key,outcome.plan_id,outcome.status.value),instrument_key=outcome.instrument.stable_key)

    @staticmethod
    def _strategy_outcome_from_payload(payload:dict)->StrategyOutcome:
        raw=payload["instrument"]; instrument=InstrumentId(raw["code"],Market(raw["market"]),Exchange(raw["exchange"])); money=lambda key: Decimal(payload[key]) if payload[key] is not None else None
        return StrategyOutcome(payload["strategy_outcome_id"],payload["plan_id"],payload["scenario_id"],payload["decision_id"],instrument,payload["action"],payload["family"],payload["strategy_id"],payload["strategy_version"],payload["parameter_hash"],payload["profile"],EvidenceOrigin(payload["evidence_origin"]),int(payload["evaluation_horizon"]),date.fromisoformat(payload["target_session_date"]),payload["trigger_state"],_parse_datetime(payload["trigger_at"]) if payload["trigger_at"] else None,payload["fill_outcome"],money("fill_price"),Decimal(payload["filled_shares"]),money("gross_return"),money("net_return"),money("benchmark_return"),money("excess_return"),money("mae"),money("mfe"),LearningEvidenceGrade(payload["execution_evidence_grade"]),OutcomeStatus(payload["status"]),tuple(payload["reason_codes"]),_parse_datetime(payload["generated_at"]),_parse_datetime(payload["valid_from"]) if payload.get("valid_from") else None,_parse_datetime(payload["expires_at"]) if payload.get("expires_at") else None,payload.get("exit_type"),_parse_datetime(payload["exit_at"]) if payload.get("exit_at") else None,money("exit_price"),payload.get("holding_sessions"),money("commission"),money("tax"),money("slippage"),money("exit_avoided_loss"),money("exit_opportunity_cost"),money("exit_quality"),payload.get("entry_fill_id"),payload.get("exit_fill_id"),payload.get("market_regime_key"),_parse_datetime(payload["evaluated_at"]) if payload.get("evaluated_at") else None)

    def get_strategy_outcome(self,outcome_id:str)->StrategyOutcome|None:
        row=self._fetchone("SELECT * FROM strategy_outcomes WHERE strategy_outcome_id=?",(outcome_id,))
        if row is None:return None
        value=self._strategy_outcome_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.strategy_outcome_id,"instrument_key":value.instrument.stable_key,"plan_id":value.plan_id,"status":value.status.value,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored strategy outcome columns do not match payload")
        return value

    def list_strategy_outcomes(self, instrument: InstrumentId) -> tuple[StrategyOutcome,...]:
        return tuple(self.get_strategy_outcome(row["strategy_outcome_id"]) for row in self._fetchall("SELECT strategy_outcome_id FROM strategy_outcomes WHERE instrument_key=? ORDER BY generated_at,strategy_outcome_id",(instrument.stable_key,)))

    def save_joint_outcome(self, outcome: JointOutcome) -> ForecastWriteResult:
        return self._save_learning_record(table="joint_outcomes",id_column="joint_outcome_id",record=outcome,event_key=outcome.joint_outcome_id,columns=("joint_outcome_id","event_key","market","profile","outcome_kind","status","payload_hash","payload_json","generated_at","schema_version"),values=(outcome.joint_outcome_id,outcome.joint_outcome_id,outcome.market.value,outcome.profile,outcome.outcome_kind.value,outcome.status.value),instrument_key=None)

    @staticmethod
    def _joint_outcome_from_payload(payload:dict)->JointOutcome:
        money=lambda key: Decimal(payload[key]) if payload[key] is not None else None
        window=payload.get("replay_window")
        return JointOutcome(payload["joint_outcome_id"],JointOutcomeKind(payload["outcome_kind"]),payload["portfolio_bundle_id"],payload["profile"],payload["batch_id"],payload["account_hash"],payload["valuation_id"],Market(payload["market"]),payload["currency"],tuple(payload["ordered_allocation_ids"]),tuple(payload["intent_ids"]),tuple(payload["execution_run_ids"]),EvidenceOrigin(payload["evidence_origin"]),Decimal(payload["starting_equity"]),Decimal(payload["ending_equity"]),Decimal(payload["net_cash_flow"]),Decimal(payload["time_weighted_return"]),money("benchmark_return"),money("alpha"),Decimal(payload["max_drawdown"]),money("volatility"),money("sharpe"),money("calmar"),Decimal(payload["realized_friction"]),money("planned_loss"),money("realized_loss"),int(payload["entry_count"]),int(payload["exit_count"]),int(payload["rejected_count"]),OutcomeStatus(payload["status"]),LearningEvidenceGrade(payload["evidence_grade"]),tuple(payload["reason_codes"]),_parse_datetime(payload["generated_at"]),None if window is None else (date.fromisoformat(window[0]),date.fromisoformat(window[1])),money("risk_contribution"),money("execution_contribution"),money("portfolio_contribution"))

    def get_joint_outcome(self,outcome_id:str)->JointOutcome|None:
        row=self._fetchone("SELECT * FROM joint_outcomes WHERE joint_outcome_id=?",(outcome_id,))
        if row is None:return None
        value=self._joint_outcome_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.joint_outcome_id,"market":value.market.value,"profile":value.profile,"outcome_kind":value.outcome_kind.value,"status":value.status.value,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored joint outcome columns do not match payload")
        return value

    def list_joint_outcomes(self, market: Market, *, profile: str | None=None) -> tuple[JointOutcome,...]:
        sql="SELECT joint_outcome_id FROM joint_outcomes WHERE market=?"; values=[market.value]
        if profile is not None: sql+=" AND profile=?"; values.append(profile)
        sql+=" ORDER BY generated_at,joint_outcome_id"
        return tuple(self.get_joint_outcome(row["joint_outcome_id"]) for row in self._fetchall(sql,tuple(values)))

    def save_learning_run(self, run: LearningRun) -> ForecastWriteResult:
        return self._save_learning_record(table="learning_replay_runs",id_column="run_id",record=run,event_key=run.run_id,columns=("run_id","event_key","market","status","payload_hash","payload_json","generated_at","schema_version"),values=(run.run_id,run.run_id,run.market.value,run.status.value),instrument_key=None)

    @staticmethod
    def _learning_run_from_payload(payload: dict) -> LearningRun:
        return LearningRun(payload["run_id"],Market(payload["market"]),payload["scope_key"],_parse_datetime(payload["cutoff_at"]),payload["task_kind"],payload["candidate_set_hash"],LearningRunStatus(payload["status"]),payload["cancel_reason"],payload["result_hash"],_parse_datetime(payload["generated_at"]))

    def get_learning_run(self, run_id: str) -> LearningRun | None:
        row=self._fetchone("SELECT * FROM learning_replay_runs WHERE run_id=?",(run_id,))
        if row is None:return None
        value=self._learning_run_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.run_id,"market":value.market.value,"status":value.status.value,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored learning run columns do not match payload")
        return value

    def save_learning_metric_snapshot(self, snapshot: LearningMetricSnapshot) -> ForecastWriteResult:
        return self._save_learning_record(table="learning_metric_snapshots",id_column="snapshot_id",record=snapshot,event_key=snapshot.snapshot_id,columns=("snapshot_id","event_key","ledger_kind","scope_key","payload_hash","payload_json","generated_at","schema_version"),values=(snapshot.snapshot_id,snapshot.snapshot_id,snapshot.ledger_kind.value,snapshot.scope_key),instrument_key=None)

    def get_learning_metric_snapshot(self, snapshot_id: str) -> LearningMetricSnapshot | None:
        row=self._fetchone("SELECT * FROM learning_metric_snapshots WHERE snapshot_id=?",(snapshot_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"])
        value=LearningMetricSnapshot(payload["snapshot_id"],LedgerKind(payload["ledger_kind"]),payload["scope_key"],_parse_datetime(payload["data_cutoff_at"]),int(payload["sample_count"]),tuple(tuple(item) for item in payload["metrics"]),_parse_datetime(payload["generated_at"]))
        expected={"event_key":value.snapshot_id,"ledger_kind":value.ledger_kind.value,"scope_key":value.scope_key,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored learning metric columns do not match payload")
        return value

    def list_latest_learning_metric_snapshots(self, scope_prefix: str) -> tuple[LearningMetricSnapshot, ...]:
        """Return the newest immutable snapshot for each matching ledger scope."""
        rows = self._fetchall(
            """SELECT snapshot_id FROM learning_metric_snapshots
               WHERE scope_key LIKE ? ORDER BY generated_at, snapshot_id""",
            (scope_prefix + "%",),
        )
        latest = {}
        for row in rows:
            value = self.get_learning_metric_snapshot(row["snapshot_id"])
            latest[(value.ledger_kind, value.scope_key)] = value
        return tuple(sorted(
            latest.values(), key=lambda item: (item.ledger_kind.value, item.scope_key),
        ))

    def list_historical_evaluation_records(self, market: Market) -> tuple[tuple[object, ...], tuple[object, ...]]:
        """Load all frozen ledgers for the read-only historical evaluation page."""
        prefix=f"{market.value}:"
        forecast=tuple(
            self.get_forecast_outcome(row["forecast_outcome_id"])
            for row in self._fetchall("SELECT forecast_outcome_id FROM forecast_outcomes WHERE instrument_key LIKE ? ORDER BY generated_at",(prefix+"%",))
        )
        strategy=tuple(
            self.get_strategy_outcome(row["strategy_outcome_id"])
            for row in self._fetchall("SELECT strategy_outcome_id FROM strategy_outcomes WHERE instrument_key LIKE ? ORDER BY generated_at",(prefix+"%",))
        )
        joint=self.list_joint_outcomes(market)
        hypotheses=tuple(
            self.get_hypothesis_outcome(row["outcome_id"])
            for row in self._fetchall("SELECT outcome_id FROM hypothesis_outcomes WHERE instrument_key LIKE ? ORDER BY generated_at",(prefix+"%",))
        )
        learning_metrics=tuple(
            self.get_learning_metric_snapshot(row["snapshot_id"])
            for row in self._fetchall("SELECT snapshot_id FROM learning_metric_snapshots ORDER BY generated_at")
        )
        research_metrics=tuple(
            self.get_research_metric_snapshot(row["snapshot_id"])
            for row in self._fetchall("SELECT snapshot_id FROM research_metric_snapshots WHERE market=? ORDER BY generated_at",(market.value,))
        )
        return (*forecast,*strategy,*joint,*hypotheses),(*learning_metrics,*research_metrics)

    def save_learning_fold(self, run_id: str, fold, *, generated_at: datetime) -> ForecastWriteResult:
        """保存可重跑 OOF 折定义；运行重启后仍使用原冻结窗口和版本选择。"""
        from learning.replay import FoldDefinition
        if not isinstance(fold,FoldDefinition): raise ContractViolation("learning fold must use FoldDefinition")
        record=SimpleNamespaceFoldRecord(run_id,fold,generated_at)
        return self._save_learning_record(table="learning_folds",id_column="fold_id",record=record,event_key=fold.fold_id,columns=("fold_id","event_key","run_id","market","test_start","payload_hash","payload_json","generated_at","schema_version"),values=(fold.fold_id,fold.fold_id,run_id,fold.market.value,fold.test_start.isoformat()),instrument_key=None)

    def get_learning_fold(self, fold_id: str):
        from learning.replay import FoldDefinition
        row=self._fetchone("SELECT * FROM learning_folds WHERE fold_id=?",(fold_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"]); fold=payload["fold"]
        value=FoldDefinition(fold["fold_id"],Market(fold["market"]),fold["scope"],fold["scope_key"],date.fromisoformat(fold["train_start"]),date.fromisoformat(fold["train_end"]),date.fromisoformat(fold["embargo_start"]),date.fromisoformat(fold["embargo_end"]),date.fromisoformat(fold["test_start"]),date.fromisoformat(fold["test_end"]),date.fromisoformat(fold["data_cutoff_at"]),fold["training_event_hash"],tuple(fold.get("selected_forecast_versions",())),tuple(fold.get("selected_strategy_parameter_hashes",())),fold.get("risk_policy_version", ""),fold.get("execution_policy_version", ""),fold.get("portfolio_policy_version", ""))
        if row["event_key"]!=value.fold_id or row["market"]!=value.market.value or row["test_start"]!=value.test_start.isoformat(): raise ContractViolation("stored learning fold columns do not match payload")
        return value

    def reserve_learning_run(self, run: LearningRun) -> LearningRun:
        """同 scope/cutoff/task/candidate 集合只保留一个活动任务身份。"""
        with self._transaction() as connection:
            row=connection.execute("SELECT payload_json FROM learning_replay_runs WHERE market=? AND status IN ('pending','running')",(run.market.value,)).fetchall()
            for item in row:
                payload=json.loads(item["payload_json"])
                if payload["scope_key"]==run.scope_key and payload["cutoff_at"]==utc_iso(run.cutoff_at) and payload["task_kind"]==run.task_kind and payload["candidate_set_hash"]==run.candidate_set_hash:
                    return LearningRun(payload["run_id"],Market(payload["market"]),payload["scope_key"],_parse_datetime(payload["cutoff_at"]),payload["task_kind"],payload["candidate_set_hash"],LearningRunStatus(payload["status"]),payload["cancel_reason"],payload["result_hash"],_parse_datetime(payload["generated_at"]))
            self._save_learning_record(table="learning_replay_runs",id_column="run_id",record=run,event_key=run.run_id,columns=("run_id","event_key","market","status","payload_hash","payload_json","generated_at","schema_version"),values=(run.run_id,run.run_id,run.market.value,run.status.value),connection=connection)
            return run

    def save_plan_evidence_snapshot(self, evidence: PlanEvidenceSnapshot) -> ForecastWriteResult:
        """V2-6 消费的学习投影独立持久化，禁止覆盖历史 outcome。"""
        return self._save_learning_record(table="plan_evidence_snapshots",id_column="evidence_id",record=evidence,event_key=evidence.evidence_id,columns=("evidence_id","event_key","instrument_key","strategy_id","parameter_hash","profile","payload_hash","payload_json","generated_at","schema_version"),values=(evidence.evidence_id,evidence.evidence_id,evidence.instrument.stable_key,evidence.strategy_id,evidence.parameter_hash,evidence.profile.value if evidence.profile else None),instrument_key=evidence.instrument.stable_key)

    @staticmethod
    def _plan_evidence_from_payload(payload: dict) -> PlanEvidenceSnapshot:
        raw=payload["instrument"]; instrument=InstrumentId(raw["code"],Market(raw["market"]),Exchange(raw["exchange"]))
        return PlanEvidenceSnapshot(payload["evidence_id"],instrument,payload["strategy_id"],payload["strategy_version"],payload["parameter_hash"],RiskProfile(payload["profile"]) if payload["profile"] else None,int(payload["sample_count"]),int(payload["oof_sample_count"]),payload["expected_net_return"],payload["confidence_low"],payload["confidence_high"],payload["win_rate"],payload["max_adverse_excursion"],EvidenceStatus(payload["status"]),payload["source_ledger_version"],_parse_datetime(payload["data_cutoff_at"]),_parse_datetime(payload["evaluated_at"]),_parse_datetime(payload["generated_at"]))

    def get_plan_evidence_snapshot(self, evidence_id: str) -> PlanEvidenceSnapshot | None:
        row=self._fetchone("SELECT * FROM plan_evidence_snapshots WHERE evidence_id=?",(evidence_id,))
        if row is None:return None
        value=self._plan_evidence_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.evidence_id,"instrument_key":value.instrument.stable_key,"strategy_id":value.strategy_id,"parameter_hash":value.parameter_hash,"profile":value.profile.value if value.profile else None,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored plan evidence columns do not match payload")
        return value

    def save_learning_candidate(self, candidate: LearningCandidateVersion) -> ForecastWriteResult:
        if candidate.lifecycle is not CandidateLifecycle.CANDIDATE:
            raise ContractViolation("new learning candidates must enter through candidate lifecycle")
        return self._save_learning_record(table="learning_candidate_versions",id_column="candidate_id",record=candidate,event_key=candidate.candidate_id,columns=("candidate_id","event_key","market","kind","scope","scope_key","lifecycle","payload_hash","payload_json","generated_at","schema_version"),values=(candidate.candidate_id,candidate.candidate_id,candidate.market.value,candidate.kind.value,candidate.scope.value,candidate.scope_key,candidate.lifecycle.value),instrument_key=None)

    @staticmethod
    def _learning_candidate_from_payload(payload: dict) -> LearningCandidateVersion:
        return LearningCandidateVersion(payload["candidate_id"],CandidateKind(payload["kind"]),CandidateScope(payload["scope"]),payload["scope_key"],Market(payload["market"]),payload["profile"],payload["base_version"],payload["parameter_hash"],payload["search_space_hash"],CandidateLifecycle(payload["lifecycle"]),EvidenceOrigin(payload["evidence_origin"]),_parse_datetime(payload["created_at"]),_parse_datetime(payload["generated_at"]),tuple(payload["reason_codes"]),payload.get("projection_key",""))

    def get_learning_candidate(self,candidate_id:str)->LearningCandidateVersion|None:
        row=self._fetchone("SELECT * FROM learning_candidate_versions WHERE candidate_id=?",(candidate_id,))
        if row is None:return None
        value=self._learning_candidate_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.candidate_id,"market":value.market.value,"kind":value.kind.value,"scope":value.scope.value,"scope_key":value.scope_key,"lifecycle":value.lifecycle.value,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored learning candidate columns do not match payload")
        return value

    def promote_learning_candidate(self, candidate: LearningCandidateVersion, event: PromotionEvent) -> ForecastWriteResult:
        """候选事件与唯一生产投影在同一事务中切换，禁止双 Champion。"""
        if event.candidate_id!=candidate.candidate_id or event.projection_key!=candidate.projection_key:
            raise ContractViolation("promotion references another candidate or projection")
        if event.previous_candidate_id is None:
            raise ContractViolation("promotion requires the previous lifecycle version")
        with self._transaction() as connection:
            previous_row=connection.execute("SELECT payload_json FROM learning_candidate_versions WHERE candidate_id=?",(event.previous_candidate_id,)).fetchone()
            if previous_row is None:
                raise ContractViolation("promotion previous candidate does not exist")
            previous=self._learning_candidate_from_payload(json.loads(previous_row["payload_json"]))
            from learning.lifecycle import next_lifecycle
            expected_lifecycle=next_lifecycle(previous.lifecycle,event.decision)
            same_lineage=(
                previous.kind==candidate.kind and previous.scope==candidate.scope and previous.scope_key==candidate.scope_key
                and previous.market==candidate.market and previous.profile==candidate.profile
                and previous.base_version==candidate.base_version and previous.parameter_hash==candidate.parameter_hash
                and previous.search_space_hash==candidate.search_space_hash and previous.evidence_origin==candidate.evidence_origin
                and previous.projection_key==candidate.projection_key
            )
            if not same_lineage or expected_lifecycle is previous.lifecycle or candidate.lifecycle is not expected_lifecycle:
                raise ContractViolation("promotion does not follow the registered lifecycle")
            guarded_decisions={
                PromotionDecision.PROMOTE_TO_CHALLENGER,
                PromotionDecision.PROMOTE_TO_SHADOW,
                PromotionDecision.PROMOTE_TO_CHAMPION,
            }
            if event.decision in guarded_decisions and not event.hard_guardrails_ok:
                raise ContractViolation("promotion cannot bypass hard guardrails")
            if candidate.lifecycle is CandidateLifecycle.CHAMPION and event.evidence_sample_count < 20:
                raise ContractViolation("champion promotion requires shadow evidence")
            if event.decision in {PromotionDecision.ROLLBACK,PromotionDecision.SUSPEND_NEW_RISK}:
                active=connection.execute(
                    "SELECT candidate_id FROM learning_deployments WHERE projection_key=?",
                    (event.projection_key,),
                ).fetchone()
                if active is None or active["candidate_id"]!=previous.candidate_id:
                    raise ContractViolation("drift action must target the currently deployed champion")
            deployment_target=None
            if event.decision is PromotionDecision.PROMOTE_TO_CHAMPION:
                deployment_target=candidate
            elif event.decision is PromotionDecision.ROLLBACK:
                target_row=connection.execute(
                    "SELECT payload_json FROM learning_candidate_versions WHERE candidate_id=?",
                    (event.deployment_candidate_id,),
                ).fetchone()
                if target_row is None:
                    raise ContractViolation("rollback deployment target does not exist")
                deployment_target=self._learning_candidate_from_payload(json.loads(target_row["payload_json"]))
                if (
                    deployment_target.lifecycle is not CandidateLifecycle.CHAMPION
                    or deployment_target.projection_key!=candidate.projection_key
                    or deployment_target.market is not candidate.market
                ):
                    raise ContractViolation("rollback target is not a healthy champion for this projection")
            payload=canonical_json(event); hashed=stable_hash(_without_generated_at(json.loads(payload)))
            existing=connection.execute("SELECT payload_hash FROM learning_promotion_events WHERE promotion_id=? OR event_key=?",(event.promotion_id,event.promotion_id)).fetchone()
            if existing and existing["payload_hash"]!=hashed:
                connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)",("learning_promotion_conflict",None,None,"CONFLICTING_LEARNING_RECORD",payload,utc_iso(datetime.now(timezone.utc))))
                return ForecastWriteResult(0,0,1)
            result=self._save_learning_record(table="learning_candidate_versions",id_column="candidate_id",record=candidate,event_key=candidate.candidate_id,columns=("candidate_id","event_key","market","kind","scope","scope_key","lifecycle","payload_hash","payload_json","generated_at","schema_version"),values=(candidate.candidate_id,candidate.candidate_id,candidate.market.value,candidate.kind.value,candidate.scope.value,candidate.scope_key,candidate.lifecycle.value),connection=connection)
            if result.conflicts:
                # 候选版本冲突时也绝不能写入 promotion 或改变部署投影。
                return result
            if existing is None:
                connection.execute("INSERT INTO learning_promotion_events(promotion_id,event_key,candidate_id,projection_key,decision,payload_hash,payload_json,generated_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?)",(event.promotion_id,event.promotion_id,event.candidate_id,event.projection_key,event.decision.value,hashed,payload,utc_iso(event.generated_at),1))
            if deployment_target is not None:
                # 一个 projection 只有一个 Champion；旧部署仅被替换，不会被删除。
                connection.execute("INSERT INTO learning_deployments(projection_key,candidate_id,promotion_id,updated_at) VALUES (?,?,?,?) ON CONFLICT(projection_key) DO UPDATE SET candidate_id=excluded.candidate_id,promotion_id=excluded.promotion_id,updated_at=excluded.updated_at",(event.projection_key,deployment_target.candidate_id,event.promotion_id,utc_iso(event.decided_at)))
            elif event.decision is PromotionDecision.SUSPEND_NEW_RISK:
                connection.execute("DELETE FROM learning_deployments WHERE projection_key=?",(event.projection_key,))
            return result

    def get_learning_deployment(self, projection_key: str) -> tuple[LearningCandidateVersion, str] | None:
        row=self._fetchone("SELECT candidate_id,promotion_id FROM learning_deployments WHERE projection_key=?",(projection_key,))
        if row is None:return None
        candidate=self.get_learning_candidate(row["candidate_id"])
        if candidate is None: raise ContractViolation("learning deployment references missing candidate")
        return candidate,row["promotion_id"]

    def save_research_result(self, context: ResearchContext, response: RawResearchResponse, hypotheses: tuple[ResearchHypothesis,...], validations: tuple[HypothesisValidation,...], links: tuple[HypothesisCandidateLink,...], *, candidates: tuple[LearningCandidateVersion,...]=()) -> None:
        """研究上下文、假设、验证和候选链接必须同一事务闭合保存。"""
        if {item.hypothesis_id for item in validations}!={item.hypothesis_id for item in hypotheses} or {item.hypothesis_id for item in links}!={item.hypothesis_id for item in hypotheses}: raise ContractViolation("research result references are incomplete")
        if any(item.context_id!=context.context_id for item in hypotheses) or any(item.context_id!=context.context_id for item in validations): raise ContractViolation("research result context mismatch")
        if response.context_id!=context.context_id or any(item.response_id!=response.response_id for item in hypotheses): raise ContractViolation("research result response mismatch")
        fact_ids={item.fact_id for item in context.manifest.facts}
        if any(item.instrument is not None and item.instrument not in context.manifest.instruments for item in hypotheses) or any(not set(item.evidence_refs).issubset(fact_ids) for item in hypotheses): raise ContractViolation("research hypothesis references foreign context facts")
        hypotheses_by_id={item.hypothesis_id:item for item in hypotheses}
        candidates_by_id={item.candidate_id:item for item in candidates}
        linked_candidate_ids={item.candidate_id for item in links if item.candidate_id}
        if not set(candidates_by_id).issubset(linked_candidate_ids):
            raise ContractViolation("research result contains an unlinked learning candidate")
        if any(item.candidate_id and item.candidate_id not in candidates_by_id and self.get_learning_candidate(item.candidate_id) is None for item in links): raise ContractViolation("research link references missing learning candidate")
        for link in links:
            if not link.candidate_id:
                continue
            candidate=candidates_by_id.get(link.candidate_id) or self.get_learning_candidate(link.candidate_id)
            hypothesis=hypotheses_by_id[link.hypothesis_id]
            if candidate.market is not context.market:
                raise ContractViolation("research candidate market does not match context")
            if candidate.scope is CandidateScope.STOCK and (hypothesis.instrument is None or candidate.scope_key!=hypothesis.instrument.stable_key):
                raise ContractViolation("research stock candidate scope does not match hypothesis")
            if candidate.scope is CandidateScope.MARKET and candidate.scope_key!=context.market.value:
                raise ContractViolation("research market candidate scope does not match context")
            if candidate.scope is CandidateScope.INDUSTRY:
                industries={
                    str(fact.value) for fact in context.manifest.facts
                    if fact.instrument==hypothesis.instrument and fact.key=="feature.context.industry" and fact.status=="available"
                }
                if candidate.scope_key not in industries:
                    raise ContractViolation("research industry candidate lacks matching frozen fact")
        response_payload=canonical_json(response); response_hash=stable_hash(_without_generated_at(json.loads(response_payload)))
        response_existing=self._fetchone("SELECT payload_hash FROM llm_research_invocations WHERE response_id=? OR (request_id=? AND revision=?)",(response.response_id,response.request_id,response.revision))
        if response_existing is not None and response_existing["payload_hash"]!=response_hash:
            self._quarantine_research_conflict("research_response_conflict",None,"CONFLICTING_RESEARCH_RESPONSE",response_payload,response.received_at)
            raise ContractViolation("research response conflicts with stored revision")
        records=[
            ("research_contexts","context_id",context,context.context_id,None),
            *(("learning_candidate_versions","candidate_id",item,item.candidate_id,None) for item in candidates),
            *(("research_hypotheses","hypothesis_id",item,item.hypothesis_id,item.instrument.stable_key if item.instrument else None) for item in hypotheses),
            *(("hypothesis_validations","validation_id",item,item.validation_id,None) for item in validations),
            *(("hypothesis_candidate_links","link_id",item,item.link_id,None) for item in links),
        ]
        for table,id_column,record,event_key,instrument_key in records:
            self._preflight_research_record(table,id_column,record,event_key,instrument_key)
        with self._transaction() as connection:
            payload=response_payload; hashed=response_hash
            existing=connection.execute("SELECT payload_hash FROM llm_research_invocations WHERE response_id=? OR (request_id=? AND revision=?)",(response.response_id,response.request_id,response.revision)).fetchone()
            if existing is None: connection.execute("INSERT INTO llm_research_invocations(response_id,request_id,context_id,revision,provider_name,model_name,prompt_version,prompt_hash,content_hash,status,finish_reason,payload_hash,payload_json,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(response.response_id,response.request_id,response.context_id,response.revision,response.provider_name,response.model_name,response.prompt_version,response.prompt_hash,response.content_hash,response.invocation_status.value,response.finish_reason,hashed,payload,utc_iso(response.received_at)))
            elif existing["payload_hash"]!=hashed: raise ContractViolation("research response conflicts with stored revision")
            self._require_clean_research_write(self._save_learning_record(table="research_contexts",id_column="context_id",record=context,event_key=context.context_id,columns=("context_id","event_key","scope","market","cutoff_at","payload_hash","payload_json","generated_at","schema_version"),values=(context.context_id,context.context_id,context.scope.value,context.market.value,utc_iso(context.cutoff_at)),connection=connection))
            for candidate in candidates:
                result=self._save_learning_record(table="learning_candidate_versions",id_column="candidate_id",record=candidate,event_key=candidate.candidate_id,columns=("candidate_id","event_key","market","kind","scope","scope_key","lifecycle","projection_key","payload_hash","payload_json","generated_at","schema_version"),values=(candidate.candidate_id,candidate.candidate_id,candidate.market.value,candidate.kind.value,candidate.scope.value,candidate.scope_key,candidate.lifecycle.value,candidate.projection_key),connection=connection)
                if result.conflicts: raise ContractViolation("research candidate conflicts with stored candidate")
            for item in hypotheses:
                self._require_clean_research_write(self._save_learning_record(table="research_hypotheses",id_column="hypothesis_id",record=item,event_key=item.hypothesis_id,columns=("hypothesis_id","event_key","response_id","context_id","instrument_key","kind","business_key","payload_hash","payload_json","generated_at","schema_version"),values=(item.hypothesis_id,item.hypothesis_id,item.response_id,item.context_id,item.instrument.stable_key if item.instrument else None,item.kind.value,item.business_key),instrument_key=item.instrument.stable_key if item.instrument else None,connection=connection))
            for item in validations:
                self._require_clean_research_write(self._save_learning_record(table="hypothesis_validations",id_column="validation_id",record=item,event_key=item.validation_id,columns=("validation_id","event_key","hypothesis_id","context_id","status","validator_version","payload_hash","payload_json","generated_at","schema_version"),values=(item.validation_id,item.validation_id,item.hypothesis_id,item.context_id,item.status.value,item.validator_version),connection=connection))
            for item in links:
                self._require_clean_research_write(self._save_learning_record(table="hypothesis_candidate_links",id_column="link_id",record=item,event_key=item.link_id,columns=("link_id","event_key","hypothesis_id","candidate_id","eligibility","mapping_version","payload_hash","payload_json","generated_at","schema_version"),values=(item.link_id,item.link_id,item.hypothesis_id,item.candidate_id,item.eligibility.value,item.mapping_registry_version),connection=connection))

    @staticmethod
    def _require_clean_research_write(result: ForecastWriteResult) -> None:
        if result.conflicts:
            raise ContractViolation("research record conflicts with canonical stored record")

    def _preflight_research_record(self,table,id_column,record,event_key,instrument_key):
        payload=canonical_json(record)
        hashed=stable_hash(_without_generated_at(json.loads(payload)))
        row=self._fetchone(f"SELECT payload_hash FROM {table} WHERE {id_column}=? OR event_key=?",(getattr(record,id_column),event_key))
        if row is not None and row["payload_hash"]!=hashed:
            self._quarantine_research_conflict("research_record_conflict",instrument_key,"CONFLICTING_RESEARCH_RECORD",payload,getattr(record,"generated_at",getattr(record,"created_at",datetime.now(timezone.utc))))
            raise ContractViolation("research record conflicts with canonical stored record")

    def _quarantine_research_conflict(self,record_type,instrument_key,reason,payload,created_at):
        with self._transaction() as connection:
            connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)",(record_type,instrument_key,None,reason,payload,utc_iso(created_at)))

    def save_hypothesis_outcome(self, outcome: HypothesisOutcome) -> ForecastWriteResult:
        hypothesis=self.get_research_hypothesis(outcome.hypothesis_id)
        if hypothesis is None:
            raise ContractViolation("research outcome references missing hypothesis")
        if hypothesis.instrument!=outcome.instrument:
            raise ContractViolation("research outcome instrument does not match hypothesis")
        payload=dict(hypothesis.payload)
        if outcome.horizon is not None and outcome.horizon not in payload.get("horizons",()):
            raise ContractViolation("research outcome horizon does not match hypothesis")
        if outcome.linked_maturity_evidence_id:
            maturity=self.get_maturity_evidence(outcome.linked_maturity_evidence_id)
            if maturity is None or maturity.instrument!=outcome.instrument or maturity.origin_session_date!=outcome.origin_session_date or maturity.target_session_date!=outcome.target_session_date:
                raise ContractViolation("research outcome maturity evidence mismatch")
        if outcome.linked_forecast_outcome_id:
            forecast=self.get_forecast_outcome(outcome.linked_forecast_outcome_id)
            if forecast is None or forecast.instrument!=outcome.instrument or forecast.origin_session_date!=outcome.origin_session_date or forecast.target_session_date!=outcome.target_session_date or forecast.horizon!=outcome.horizon or forecast.maturity_evidence_id!=outcome.linked_maturity_evidence_id:
                raise ContractViolation("research outcome forecast evidence mismatch")
        if outcome.linked_candidate_id and self.get_learning_candidate(outcome.linked_candidate_id) is None:
            raise ContractViolation("research outcome candidate is missing")
        if outcome.linked_candidate_id:
            linked=self._fetchone(
                "SELECT 1 FROM hypothesis_candidate_links WHERE hypothesis_id=? AND candidate_id=?",
                (outcome.hypothesis_id,outcome.linked_candidate_id),
            )
            if linked is None:
                raise ContractViolation("research outcome candidate is not linked to hypothesis")
        for promotion_id in outcome.linked_promotion_ids:
            row=self._fetchone("SELECT candidate_id FROM learning_promotion_events WHERE promotion_id=?",(promotion_id,))
            if row is None or row["candidate_id"]!=outcome.linked_candidate_id:
                raise ContractViolation("research outcome promotion evidence mismatch")
        return self._save_learning_record(table="hypothesis_outcomes",id_column="outcome_id",record=outcome,event_key=outcome.outcome_id,columns=("outcome_id","event_key","hypothesis_id","instrument_key","horizon","status","payload_hash","payload_json","generated_at","schema_version"),values=(outcome.outcome_id,outcome.outcome_id,outcome.hypothesis_id,outcome.instrument.stable_key,outcome.horizon,outcome.status.value),instrument_key=outcome.instrument.stable_key)

    def save_research_metric_snapshot(self, snapshot: ResearchMetricSnapshot) -> ForecastWriteResult:
        return self._save_learning_record(table="research_metric_snapshots",id_column="snapshot_id",record=snapshot,event_key=snapshot.snapshot_id,columns=("snapshot_id","event_key","market","scope_key","cutoff_at","payload_hash","payload_json","generated_at","schema_version"),values=(snapshot.snapshot_id,snapshot.snapshot_id,snapshot.market.value,snapshot.scope_key,utc_iso(snapshot.cutoff_at)),instrument_key=None)

    def get_hypothesis_outcome(self, outcome_id: str) -> HypothesisOutcome | None:
        row=self._fetchone("SELECT * FROM hypothesis_outcomes WHERE outcome_id=?",(outcome_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"])
        value=HypothesisOutcome(payload["outcome_id"],payload["hypothesis_id"],payload["observation_event_key"],self._research_instrument(payload["instrument"]),date.fromisoformat(payload["origin_session_date"]),date.fromisoformat(payload["target_session_date"]) if payload["target_session_date"] else None,payload["horizon"],HypothesisValidationStatus(payload["trigger_status"]),payload["expected_direction"],payload["actual_direction"],payload["actual_return"],payload["direction_correct"],payload["linked_maturity_evidence_id"],payload["linked_forecast_outcome_id"],payload["linked_candidate_id"],tuple(payload["linked_promotion_ids"]),HypothesisOutcomeStatus(payload["status"]),payload["evidence_grade"],_parse_datetime(payload["evaluated_at"]),_parse_datetime(payload["generated_at"]))
        expected={"event_key":value.outcome_id,"hypothesis_id":value.hypothesis_id,"instrument_key":value.instrument.stable_key,"horizon":value.horizon,"status":value.status.value,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored research outcome columns do not match payload")
        return value

    def get_research_metric_snapshot(self, snapshot_id: str) -> ResearchMetricSnapshot | None:
        row=self._fetchone("SELECT * FROM research_metric_snapshots WHERE snapshot_id=?",(snapshot_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"])
        value=ResearchMetricSnapshot(payload["snapshot_id"],Market(payload["market"]),payload["scope_key"],_parse_datetime(payload["cutoff_at"]),tuple((item[0],item[1]) for item in payload["metrics"]),_parse_datetime(payload["generated_at"]),tuple((item[0],item[1]) for item in payload.get("dimensions",())))
        expected={"event_key":value.snapshot_id,"market":value.market.value,"scope_key":value.scope_key,"cutoff_at":utc_iso(value.cutoff_at),"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored research metric columns do not match payload")
        return value

    def save_research_response(self, response: RawResearchResponse) -> ForecastWriteResult:
        """response revision 只追加；content hash 相同也按 request/revision 审计。"""
        payload=canonical_json(response); hashed=stable_hash(_without_generated_at(json.loads(payload)))
        with self._transaction() as connection:
            row=connection.execute("SELECT payload_hash FROM llm_research_invocations WHERE response_id=? OR (request_id=? AND revision=?)",(response.response_id,response.request_id,response.revision)).fetchone()
            if row is not None:
                if row["payload_hash"]==hashed:return ForecastWriteResult(0,1,0)
                connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)",("research_response_conflict",None,None,"CONFLICTING_RESEARCH_RESPONSE",payload,utc_iso(response.received_at)))
                return ForecastWriteResult(0,0,1)
            connection.execute("INSERT INTO llm_research_invocations(response_id,request_id,context_id,revision,provider_name,model_name,prompt_version,prompt_hash,content_hash,status,finish_reason,payload_hash,payload_json,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(response.response_id,response.request_id,response.context_id,response.revision,response.provider_name,response.model_name,response.prompt_version,response.prompt_hash,response.content_hash,response.invocation_status.value,response.finish_reason,hashed,payload,utc_iso(response.received_at)))
            return ForecastWriteResult(1,0,0)

    def get_research_response(self, response_id: str) -> RawResearchResponse | None:
        row=self._fetchone("SELECT * FROM llm_research_invocations WHERE response_id=?",(response_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"])
        value=RawResearchResponse(payload["response_id"],payload["request_id"],payload["context_id"],int(payload["revision"]),payload["provider_name"],payload["model_name"],payload["content"],payload["content_hash"],payload["finish_reason"],payload["invocation_status"],_parse_datetime(payload["received_at"]),payload["prompt_version"],payload["prompt_hash"],payload.get("provider_request_id"),payload.get("token_usage"))
        expected={"request_id":value.request_id,"context_id":value.context_id,"revision":value.revision,"provider_name":value.provider_name,"model_name":value.model_name,"prompt_version":value.prompt_version,"prompt_hash":value.prompt_hash,"content_hash":value.content_hash,"status":value.invocation_status.value,"finish_reason":value.finish_reason,"received_at":utc_iso(value.received_at)}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored research response columns do not match payload")
        return value

    @staticmethod
    def _research_instrument(payload: dict | None) -> InstrumentId | None:
        if payload is None:return None
        return InstrumentId(payload["code"],Market(payload["market"]),Exchange(payload["exchange"]))

    @classmethod
    def _research_context_from_payload(cls, payload: dict) -> ResearchContext:
        manifest_payload=payload["manifest"]
        facts=tuple(ResearchFact(item["fact_id"],cls._research_instrument(item["instrument"]),item["key"],item["value"],item["value_type"],item["unit"],item["status"],_parse_datetime(item["available_at"]),tuple(item["source_refs"]),item["source_payload_hash"]) for item in manifest_payload["facts"])
        manifest=ResearchFactManifest(manifest_payload["manifest_id"],ResearchScope(manifest_payload["scope"]),Market(manifest_payload["market"]),_parse_datetime(manifest_payload["cutoff_at"]),tuple(cls._research_instrument(item) for item in manifest_payload["instruments"]),facts,tuple(manifest_payload["artifact_refs"]),int(manifest_payload["schema_version"]),_parse_datetime(manifest_payload["generated_at"]))
        roles=tuple((cls._research_instrument(item[0]),item[1]) for item in payload["instrument_roles"])
        return ResearchContext(payload["context_id"],ResearchScope(payload["scope"]),Market(payload["market"]),payload["mode"],_parse_datetime(payload["cutoff_at"]),manifest,roles,tuple(payload["forecast_event_keys"]),tuple(payload["scenario_ids"]),tuple(payload["strategy_bundle_ids"]),tuple(payload["risk_bundle_ids"]),payload["portfolio_bundle_id"],tuple(payload["learning_snapshot_ids"]),payload["prompt_input_version"],_parse_datetime(payload["generated_at"]))

    def get_research_context(self, context_id: str) -> ResearchContext | None:
        row=self._fetchone("SELECT * FROM research_contexts WHERE context_id=?",(context_id,))
        if row is None:return None
        value=self._research_context_from_payload(json.loads(row["payload_json"]))
        expected={"event_key":value.context_id,"scope":value.scope.value,"market":value.market.value,"cutoff_at":utc_iso(value.cutoff_at),"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored research context columns do not match payload")
        return value

    def get_research_hypothesis(self, hypothesis_id: str) -> ResearchHypothesis | None:
        row=self._fetchone("SELECT * FROM research_hypotheses WHERE hypothesis_id=?",(hypothesis_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"])
        restored=[]
        for key,item in payload["payload"]:
            restored.append((key,self._execution_expression(item) if key=="condition_expression" and item is not None else item))
        value=ResearchHypothesis(payload["hypothesis_id"],payload["business_key"],payload["response_id"],payload["context_id"],self._research_instrument(payload["instrument"]),HypothesisKind(payload["kind"]),payload["title"],payload["thesis"],tuple(payload["evidence_refs"]),tuple(restored),HypothesisNovelty(payload["novelty"]),_parse_datetime(payload["generated_at"]))
        expected={"event_key":value.hypothesis_id,"response_id":value.response_id,"context_id":value.context_id,"instrument_key":value.instrument.stable_key if value.instrument else None,"kind":value.kind.value,"business_key":value.business_key,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored research hypothesis columns do not match payload")
        return value

    def get_hypothesis_validation(self, validation_id: str) -> HypothesisValidation | None:
        row=self._fetchone("SELECT * FROM hypothesis_validations WHERE validation_id=?",(validation_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"])
        condition=None
        if payload.get("condition_evaluation") is not None:
            item=payload["condition_evaluation"]
            condition=ConditionEvaluation(item["condition_id"],ConditionResult(item["result"]),tuple(ObservedValue(value["key"],value["value"],value["status"],_parse_datetime(value["available_at"]) if value["available_at"] else None) for value in item["observed_values"]),tuple(item["missing_features"]),_parse_datetime(item["evaluated_at"]))
        value=HypothesisValidation(payload["validation_id"],payload["hypothesis_id"],payload["context_id"],HypothesisValidationStatus(payload["status"]),tuple(payload["observed_fact_ids"]),tuple(payload["missing_fact_ids"]),tuple(payload["conflicting_fact_ids"]),tuple(payload["linked_artifact_ids"]),CandidateEligibility(payload["candidate_eligibility"]),payload["validator_version"],tuple(payload["reason_codes"]),_parse_datetime(payload["evaluated_at"]),_parse_datetime(payload["generated_at"]),condition)
        expected={"event_key":value.validation_id,"hypothesis_id":value.hypothesis_id,"context_id":value.context_id,"status":value.status.value,"validator_version":value.validator_version,"generated_at":utc_iso(value.generated_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored research validation columns do not match payload")
        return value

    def get_hypothesis_candidate_link(self, link_id: str) -> HypothesisCandidateLink | None:
        row=self._fetchone("SELECT * FROM hypothesis_candidate_links WHERE link_id=?",(link_id,))
        if row is None:return None
        payload=json.loads(row["payload_json"])
        value=HypothesisCandidateLink(payload["link_id"],payload["hypothesis_id"],payload["candidate_id"],CandidateEligibility(payload["eligibility"]),payload["mapping_registry_version"],payload["mapping_key"],tuple(payload["rejection_reasons"]),_parse_datetime(payload["created_at"]))
        expected={"event_key":value.link_id,"hypothesis_id":value.hypothesis_id,"candidate_id":value.candidate_id,"eligibility":value.eligibility.value,"mapping_version":value.mapping_registry_version,"generated_at":utc_iso(value.created_at),"schema_version":1}
        if any(row[key]!=item for key,item in expected.items()):raise ContractViolation("stored research candidate link columns do not match payload")
        return value

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

    def search_stock_metadata(self, market: Market, query: str, *, limit: int = 20) -> tuple[StockMetadata, ...]:
        """本地优先检索；结果按 code 稳定排序，避免每次输入导致 UI 抖动。"""
        needle = str(query or "").strip().upper()
        if not needle: return ()
        rows = self._fetchall("SELECT * FROM stock_metadata WHERE market=? AND (UPPER(code) LIKE ? OR UPPER(name) LIKE ?) ORDER BY code LIMIT ?", (market.value, f"%{needle}%", f"%{needle}%", max(1, min(20, limit))))
        return tuple(StockMetadata(InstrumentId(row["code"], Market(row["market"]), Exchange(row["exchange"])), row["name"], row["industry"], row["description"], date.fromisoformat(row["listing_date"]) if row["listing_date"] else None, row["source"], _parse_datetime(row["fetched_at"]), row["schema_version"]) for row in rows)

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

    # ------------------------------------------------------------------
    # V2-12 迁移与运行时记录
    # ------------------------------------------------------------------
    def _save_v212_record(self, table: str, id_column: str, record_id: str, event_key: str,
                          payload: object, columns: tuple[str, ...], values: tuple[object, ...],
                          *, connection: sqlite3.Connection | None = None) -> ForecastWriteResult:
        """统一写入 V2-12 审计表，重复 payload 幂等，冲突进入 quarantine。"""
        if connection is None:
            with self._transaction() as active:
                return self._save_v212_record(table, id_column, record_id, event_key, payload, columns, values, connection=active)
        encoded = canonical_json(payload)
        digest = stable_hash(_without_generated_at(json.loads(encoded)))
        row = connection.execute(f"SELECT payload_hash FROM {table} WHERE {id_column}=? OR event_key=?", (record_id, event_key)).fetchone()
        if row is not None:
            if row["payload_hash"] == digest:
                return ForecastWriteResult(0, 1, 0)
            connection.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                               (f"{table}_conflict", None, None, "CONFLICTING_V2_12_RECORD", encoded, utc_iso(datetime.now(timezone.utc))))
            return ForecastWriteResult(0, 0, 1)
        if len(values) > len(columns):
            raise ContractViolation(f"{table} V2-12 column/value mismatch")
        # 调用方只需提供业务列；审计 hash/payload/schema 列由这里统一补齐。
        row_values = list(values) + [None] * (len(columns) - len(values))
        for name, replacement in (("payload_hash", digest), ("payload_json", encoded),
                                  ("generated_at", utc_iso(datetime.now(timezone.utc))), ("schema_version", 17)):
            if name in columns:
                row_values[columns.index(name)] = replacement
        connection.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row_values))
        return ForecastWriteResult(1, 0, 0)

    def save_migration_run(self, run: MigrationRun, *, plan: MigrationPlan | None = None) -> ForecastWriteResult:
        payload = {"run": run, "plan": plan}
        preflight = plan.preflight_hash if plan else ""
        encoded=canonical_json(payload); digest=stable_hash(_without_generated_at(json.loads(encoded)))
        existing=self._fetchone("SELECT payload_hash FROM legacy_migration_runs WHERE run_id=?",(run.run_id,))
        if existing is not None:
            if existing["payload_hash"]==digest: return ForecastWriteResult(0,1,0)
            # run 状态是迁移生命周期的可追加投影，完成/失败状态推进不能被当作事实冲突。
            with self._transaction() as connection:
                connection.execute("UPDATE legacy_migration_runs SET status=?,backup_path=?,reason_codes_json=?,payload_hash=?,payload_json=?,completed_at=?,generated_at=? WHERE run_id=?",(run.status.value,run.backup_path,canonical_json(run.reason_codes),digest,encoded,utc_iso(run.completed_at) if run.completed_at else None,utc_iso(datetime.now(timezone.utc)),run.run_id))
            return ForecastWriteResult(0,1,0)
        result = self._save_v212_record("legacy_migration_runs", "run_id", run.run_id, run.run_id, payload,
                ("run_id","event_key","plan_id","source_path","source_fingerprint","migration_version","status","preflight_hash","backup_path","reason_codes_json","payload_hash","payload_json","started_at","completed_at","generated_at","schema_version"),
                (run.run_id,run.run_id,run.plan_id,plan.source_path if plan else "",run.source_fingerprint,run.migration_version,run.status.value,preflight,run.backup_path,canonical_json(run.reason_codes),"", "",utc_iso(run.started_at),utc_iso(run.completed_at) if run.completed_at else None,utc_iso(run.started_at)))
        return result

    def save_migration_item(self, item: MigrationItem) -> ForecastWriteResult:
        return self._save_v212_record("legacy_migration_items", "item_id", item.item_id, item.item_id, item,
                ("item_id","event_key","run_id","source_table","source_key","target_kind","status","reason_codes_json","payload_hash","payload_json","created_at","schema_version"),
                (item.item_id,item.item_id,item.run_id,item.source_table,item.source_key,item.target_kind,item.status.value,canonical_json(item.reason_codes),"","",utc_iso(item.created_at)))

    def get_migration_run(self, run_id: str) -> MigrationRun | None:
        row = self._fetchone("SELECT payload_json FROM legacy_migration_runs WHERE run_id=?", (run_id,))
        if row is None: return None
        payload=json.loads(row["payload_json"])["run"]
        payload["started_at"]=_parse_datetime(payload["started_at"]); payload["completed_at"]=_parse_datetime(payload["completed_at"]) if payload.get("completed_at") else None
        return MigrationRun(**payload)

    def find_completed_migration(self, source_fingerprint: str, migration_version: int) -> MigrationRun | None:
        row=self._fetchone(
            """SELECT run_id FROM legacy_migration_runs
               WHERE source_fingerprint=? AND migration_version=?
                 AND status IN ('completed','completed_with_quarantine')
               ORDER BY completed_at DESC LIMIT 1""",
            (source_fingerprint,migration_version),
        )
        return None if row is None else self.get_migration_run(row["run_id"])

    def list_migration_items(self, run_id: str) -> tuple[MigrationItem, ...]:
        rows = self._fetchall("SELECT payload_json FROM legacy_migration_items WHERE run_id=? ORDER BY created_at,item_id", (run_id,))
        values=[]
        for row in rows:
            payload=json.loads(row["payload_json"]); payload["created_at"]=_parse_datetime(payload["created_at"]); values.append(MigrationItem(**payload))
        return tuple(values)

    def save_legacy_report_archive(self, archive: Mapping[str, object]) -> ForecastWriteResult:
        created = archive.get("created_at") or datetime.now(timezone.utc)
        if isinstance(created, str): created = _parse_datetime(created)
        aid = str(archive["archive_id"])
        return self._save_v212_record("legacy_report_archives", "archive_id", aid, aid, archive,
                ("archive_id","event_key","run_id","source_fingerprint","source_id","market","code","title","content","path","rating","created_at","payload_hash","payload_json","schema_version"),
                (aid,aid,str(archive["run_id"]),str(archive["source_fingerprint"]),str(archive["source_id"]),archive.get("market"),archive.get("code"),archive.get("title"),archive.get("content"),archive.get("path"),archive.get("rating"),utc_iso(created)))

    def save_legacy_evidence_archive(self, archive: Mapping[str, object]) -> ForecastWriteResult:
        created = archive.get("created_at") or datetime.now(timezone.utc)
        if isinstance(created, str): created = _parse_datetime(created)
        aid = str(archive["archive_id"])
        return self._save_v212_record("legacy_evidence_archives", "archive_id", aid, aid, archive,
                ("archive_id","event_key","run_id","source_table","source_id","market","code","evidence_kind","reason_codes_json","payload_hash","payload_json","created_at","schema_version"),
                (aid,aid,str(archive["run_id"]),str(archive["source_table"]),str(archive["source_id"]),archive.get("market"),archive.get("code"),str(archive["evidence_kind"]),canonical_json(archive.get("reason_codes", ())),"","",utc_iso(created)))

    def save_instrument_alias(self, alias: Mapping[str, object]) -> ForecastWriteResult:
        created = alias.get("created_at") or datetime.now(timezone.utc)
        if isinstance(created, str): created = _parse_datetime(created)
        aid = str(alias["alias_id"])
        return self._save_v212_record("instrument_aliases", "alias_id", aid, aid, alias,
                ("alias_id","event_key","market","legacy_code","canonical_instrument_key","status","source","created_at","payload_hash","payload_json","schema_version"),
                (aid,aid,str(alias["market"]),str(alias["legacy_code"]),str(alias["canonical_instrument_key"]),str(alias["status"]),str(alias.get("source","legacy")),utc_iso(created)))

    def save_analysis_run(self, result: AnalysisRunResult, *, report_kind: str, market: str,
                          instrument_key: str | None, mode: str, history_period: str) -> ForecastWriteResult:
        payload = {"result": result, "report_kind": report_kind, "market": market, "instrument_key": instrument_key, "mode": mode, "history_period": history_period}
        encoded=canonical_json(payload); digest=stable_hash(_without_generated_at(json.loads(encoded)))
        existing=self._fetchone("SELECT payload_hash FROM analysis_runs WHERE run_id=?",(result.run_id,))
        if existing is not None:
            if existing["payload_hash"]==digest: return ForecastWriteResult(0,1,0)
            with self._transaction() as connection:
                connection.execute("UPDATE analysis_runs SET status=?,deterministic_report_id=?,research_report_id=?,background_task_ids_json=?,source_refs_json=?,reason_codes_json=?,completed_at=?,payload_hash=?,payload_json=? WHERE run_id=?",(result.status.value,result.deterministic_report_id,result.research_report_id,canonical_json(result.background_task_ids),canonical_json(result.source_artifact_refs),canonical_json(result.reason_codes),utc_iso(result.completed_at) if result.completed_at else None,digest,encoded,result.run_id))
            return ForecastWriteResult(0,1,0)
        return self._save_v212_record("analysis_runs", "run_id", result.run_id, result.run_id, payload,
                ("run_id","event_key","command_id","report_kind","market","instrument_key","mode","history_period","status","deterministic_report_id","research_report_id","background_task_ids_json","source_refs_json","reason_codes_json","started_at","completed_at","payload_hash","payload_json","schema_version"),
                (result.run_id,result.run_id,result.command_id,report_kind,market,instrument_key,mode,history_period,result.status.value,result.deterministic_report_id,result.research_report_id,canonical_json(result.background_task_ids),canonical_json(result.source_artifact_refs),canonical_json(result.reason_codes),utc_iso(result.started_at),utc_iso(result.completed_at) if result.completed_at else None,"",""))

    def get_analysis_run(self, run_id: str) -> AnalysisRunResult | None:
        row = self._fetchone("SELECT payload_json FROM analysis_runs WHERE run_id=?", (run_id,))
        if row is None: return None
        payload=json.loads(row["payload_json"])["result"]
        payload["started_at"]=_parse_datetime(payload["started_at"]); payload["completed_at"]=_parse_datetime(payload["completed_at"]) if payload.get("completed_at") else None
        payload["status"]=AnalysisRunStatus(payload["status"])
        return AnalysisRunResult(**payload)

    def save_report_revision_link(self, link: ReportRevisionLink) -> ForecastWriteResult:
        base=self.get_report_document(link.base_report_id)
        revised=self.get_report_document(link.revised_report_id)
        if base is None or revised is None:
            raise ContractViolation("report revision must reference stored reports")
        if report_revision_invariant(base)!=link.invariant_section_hash or report_revision_invariant(revised)!=link.invariant_section_hash:
            raise ContractViolation("report revision invariant hash mismatch")
        return self._save_v212_record("report_revision_links", "link_id", link.link_id, link.link_id, link,
                ("link_id","event_key","base_report_id","revised_report_id","revision_kind","invariant_section_hash","payload_hash","payload_json","created_at","schema_version"),
                (link.link_id,link.link_id,link.base_report_id,link.revised_report_id,link.revision_kind.value,link.invariant_section_hash,"","",utc_iso(link.created_at)))

    def get_report_revision_link(self, link_id: str) -> ReportRevisionLink | None:
        row = self._fetchone("SELECT payload_json FROM report_revision_links WHERE link_id=?", (link_id,))
        if row is None: return None
        payload=json.loads(row["payload_json"])
        if "value" in payload: payload=payload["value"]
        payload["created_at"]=_parse_datetime(payload["created_at"])
        return ReportRevisionLink(**payload)

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
