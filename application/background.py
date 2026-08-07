"""确定性报告与后台研究解耦；LLM 超时/失败只影响 revision，不影响主报告。"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import Executor
from threading import Lock
from contracts.runtime import ReportRevisionLink, RevisionKind, report_revision_invariant
from contracts.market_data import stable_hash
from contracts import ForecastScope, ValidationStatus
from forecast.trainer import training_data_hash
from learning.maturity import MaturityResolver

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class BackgroundTaskResult:
    task_id: str
    status: str
    research_report_id: str | None
    reason_code: str | None

class BackgroundResearchService:
    def __init__(self, repository, executor: Executor, *, clock=None):
        self.repository=repository; self.executor=executor; self.clock=clock or (lambda:datetime.now(timezone.utc)); self._futures={}; self._lock=Lock()
    def submit(self, base_document, research_builder):
        task_id=stable_hash({"base":base_document.report_id,"kind":"research"})
        with self._lock:
            existing=self._futures.get(task_id)
            if existing is not None and not existing.cancelled():
                return task_id
        def work():
            try:
                revised=research_builder(base_document)
                if revised is None:
                    logger.warning("Background research unavailable task_id=%s report_id=%s", task_id, base_document.report_id)
                    return BackgroundTaskResult(task_id,"failed",None,"RESEARCH_UNAVAILABLE")
                invariant=report_revision_invariant(base_document)
                if report_revision_invariant(revised)!=invariant:
                    logger.error("Background research changed invariant section task_id=%s", task_id)
                    return BackgroundTaskResult(task_id,"failed",None,"RESEARCH_CHANGED_INVARIANT_SECTION")
                self.repository.save_report_document(revised)
                link=ReportRevisionLink(stable_hash({"base":base_document.report_id,"revised":revised.report_id,"kind":RevisionKind.RESEARCH_ENRICHED,"invariant":invariant}),base_document.report_id,revised.report_id,RevisionKind.RESEARCH_ENRICHED,invariant,self.clock())
                self.repository.save_report_revision_link(link)
                logger.info("Background research completed task_id=%s revised_report_id=%s", task_id, revised.report_id)
                return BackgroundTaskResult(task_id,"completed",revised.report_id,None)
            except Exception:
                logger.exception("Background research failed task_id=%s report_id=%s", task_id, base_document.report_id)
                return BackgroundTaskResult(task_id,"failed",None,"RESEARCH_FAILED")
        with self._lock:
            existing=self._futures.get(task_id)
            if existing is not None and not existing.cancelled(): return task_id
            self._futures[task_id]=self.executor.submit(work)
        return task_id
    def result(self, task_id, timeout=None):
        with self._lock: future=self._futures.get(task_id)
        return None if future is None else future.result(timeout)

    def add_done_callback(self, task_id, callback):
        with self._lock:
            future=self._futures.get(task_id)
        if future is None:
            return False
        def completed(value):
            try:
                callback(value.result())
            except Exception:
                logger.exception("Background research completion callback failed task_id=%s", task_id)
        future.add_done_callback(completed)
        return True


class BackgroundLearningService:
    """Mature issued forecasts from newly refreshed completed daily bars."""
    def __init__(self, repository, learning_engine, executor: Executor, *, clock=None):
        self.repository=repository; self.learning_engine=learning_engine; self.executor=executor
        self.clock=clock or (lambda:datetime.now(timezone.utc)); self._futures={}; self._lock=Lock()

    def submit(self, instrument, bars, *, listing_date=None):
        latest=bars[-1].trading_date.isoformat() if bars else "empty"
        task_id=stable_hash({"kind":"forecast_maturity","instrument":instrument,"latest":latest})
        with self._lock:
            existing=self._futures.get(task_id)
            if existing is not None and not existing.cancelled(): return task_id
        def work():
            try:
                evaluated_at=self.clock(); saved=0
                previous_values=self.repository.list_active_maturity_evidence(instrument)
                previous_by_window = {
                    (item.origin_session_date, item.target_session_date): item
                    for item in previous_values
                }
                for forecast in self.repository.list_forecast_results(instrument):
                    maturity_key = (
                        forecast.origin_session_date,
                        forecast.target_session_date or forecast.origin_session_date,
                    )
                    previous = previous_by_window.get(maturity_key)
                    if previous is not None and previous.status.value=="pending" and forecast.target_session_date is not None and evaluated_at.date()<=forecast.target_session_date: continue
                    evidence=MaturityResolver().resolve(forecast,bars,evaluated_at=evaluated_at,previous=previous,listing_date=listing_date)
                    if (
                        previous is not None
                        and previous.status.value == "matured"
                        and evidence.evidence_id == previous.evidence_id
                    ):
                        continue
                    self.repository.save_maturity_evidence(evidence)
                    # Several issued forecasts may share one origin/target fact.
                    # Once a revision is persisted, every later item in this batch
                    # must extend or reuse that revision instead of the stale
                    # snapshot captured before the loop.
                    previous_by_window[maturity_key] = evidence
                    outcome=self.learning_engine.evaluate_forecast(forecast,bars,evaluated_at=evaluated_at,previous_evidence=evidence,listing_date=listing_date)
                    self.repository.save_forecast_outcome(outcome); saved+=1
                logger.info("Background learning completed task_id=%s instrument=%s outcomes=%d", task_id, instrument.stable_key, saved)
                return saved
            except Exception:
                logger.exception("Background learning failed task_id=%s instrument=%s", task_id, instrument.stable_key)
                raise
        with self._lock:
            existing=self._futures.get(task_id)
            if existing is not None and not existing.cancelled(): return task_id
            self._futures[task_id]=self.executor.submit(work)
        return task_id

    def result(self, task_id, timeout=None):
        with self._lock: future=self._futures.get(task_id)
        return None if future is None else future.result(timeout)


class BackgroundForecastTrainingService:
    """Run stock-bound OOF off the UI thread and atomically promote winners."""

    def __init__(self, repository, trainer, registry, executor: Executor):
        self.repository = repository
        self.trainer = trainer
        self.registry = registry
        self.executor = executor
        self._futures = {}
        self._lock = Lock()

    def submit(self, instrument, samples, *, panel_samples=()):
        materialized = tuple(samples)
        panel_materialized = tuple(panel_samples)
        task_id = stable_hash({
            "kind": "forecast_oof",
            "instrument": instrument.stable_key,
            "training_data_hash": training_data_hash(materialized),
            "panel_data_hash": training_data_hash(panel_materialized) if panel_materialized else None,
        })
        with self._lock:
            existing = self._futures.get(task_id)
            if existing is not None and not existing.cancelled():
                return task_id

        def work():
            statuses = {}
            try:
                data_hash = stable_hash({
                    "target": training_data_hash(materialized),
                    "panel": training_data_hash(panel_materialized) if panel_materialized else None,
                })
                for horizon in (1, 3, 5, 10):
                    prior_registered = self.registry.champion(
                        market=instrument.market, scope=ForecastScope.STOCK,
                        scope_key=instrument.stable_key, horizon=horizon,
                    )
                    outcome = self.trainer.evaluate(
                        materialized, scope=ForecastScope.STOCK,
                        scope_key=instrument.stable_key, horizon=horizon,
                        panel_samples=panel_materialized,
                    )
                    statuses[horizon] = outcome.status.value
                    record_validation = getattr(self.registry, "record_validation", None)
                    if record_validation is not None:
                        record_validation(
                            market=instrument.market, scope_key=instrument.stable_key,
                            horizon=horizon, status=outcome.status, reason=outcome.reason,
                        )
                    save_evaluation = getattr(self.repository, "save_forecast_candidate_evaluation", None)
                    save_validation = getattr(self.repository, "save_forecast_validation_summary", None)
                    if save_validation is not None:
                        save_validation(
                            market=instrument.market, scope_key=instrument.stable_key,
                            horizon=horizon, status=outcome.status, reason=outcome.reason,
                            data_hash=data_hash, created_at=datetime.now(timezone.utc),
                        )
                    if save_evaluation is not None:
                        created_at = datetime.now(timezone.utc)
                        for evaluation in outcome.evaluations:
                            for phase, metrics, baseline in (
                                ("selection", evaluation.selection, evaluation.baseline_selection),
                                ("confirmation", evaluation.confirmation, evaluation.baseline_confirmation),
                            ):
                                if metrics is None or baseline is None:
                                    continue
                                payload = {
                                    "instrument_key": instrument.stable_key,
                                    "horizon": horizon,
                                    "spec_id": evaluation.spec.spec_id,
                                    "feature_set_id": evaluation.spec.feature_set_id,
                                    "status": evaluation.status.value,
                                    "candidate": {
                                        "brier": metrics.multiclass_brier,
                                        "log_loss": metrics.log_loss,
                                        "ece": metrics.expected_calibration_error,
                                        "interval_coverage": metrics.interval_coverage,
                                        "accuracy": metrics.accuracy,
                                        "sample_count": metrics.sample_count,
                                    },
                                    "baseline": {
                                        "brier": baseline.multiclass_brier,
                                        "log_loss": baseline.log_loss,
                                        "ece": baseline.expected_calibration_error,
                                        "interval_coverage": baseline.interval_coverage,
                                        "accuracy": baseline.accuracy,
                                        "sample_count": baseline.sample_count,
                                    },
                                }
                                save_evaluation(
                                    market=instrument.market, scope=ForecastScope.STOCK,
                                    scope_key=instrument.stable_key, horizon=horizon,
                                    spec_id=evaluation.spec.spec_id, phase=phase,
                                    data_hash=data_hash, payload=payload,
                                    created_at=created_at,
                                )
                    if outcome.champion is not None and outcome.champion_model is not None:
                        self.repository.promote_forecast_model(outcome.champion)
                        self.registry.promote(outcome.champion, outcome.champion_model)
                    elif outcome.status in {
                        ValidationStatus.EVALUATED_NOT_BETTER,
                        ValidationStatus.CALIBRATION_FAILED,
                        ValidationStatus.DRIFTED,
                    }:
                        retire = getattr(self.repository, "retire_forecast_champion", None)
                        if retire is not None and prior_registered is not None:
                            retired_version = retire(
                                market=instrument.market, scope=ForecastScope.STOCK,
                                scope_key=instrument.stable_key, horizon=horizon,
                                expected_version=prior_registered.version.version,
                            )
                            if retired_version is not None:
                                self.registry.retire(
                                    market=instrument.market, scope=ForecastScope.STOCK,
                                    scope_key=instrument.stable_key, horizon=horizon,
                                    expected_version=retired_version,
                                )
                logger.info(
                    "Background forecast OOF completed task_id=%s instrument=%s statuses=%s",
                    task_id, instrument.stable_key, statuses,
                )
                return statuses
            except Exception:
                logger.exception(
                    "Background forecast OOF failed task_id=%s instrument=%s",
                    task_id, instrument.stable_key,
                )
                raise

        with self._lock:
            existing = self._futures.get(task_id)
            if existing is not None and not existing.cancelled():
                return task_id
            self._futures[task_id] = self.executor.submit(work)
        return task_id

    def result(self, task_id, timeout=None):
        with self._lock:
            future = self._futures.get(task_id)
        return None if future is None else future.result(timeout)


class BackgroundStrategyReplayService:
    """Build stock-bound reconstructed strategy evidence off the UI thread."""

    def __init__(self, repository, replayer, executor: Executor):
        self.repository = repository
        self.replayer = replayer
        self.executor = executor
        self._futures = {}
        self._lock = Lock()

    def submit(self, instrument, bars, samples, *, listing_date=None):
        bar_identity = tuple(
            (item.trading_date, item.open, item.high, item.low, item.close, item.volume,
             item.adjustment_mode.value, item.source, item.corporate_action_version)
            for item in bars
        )
        task_id = stable_hash({
            "kind": "stock_strategy_joint_oof_v2",
            "instrument": instrument.stable_key,
            "bars": stable_hash(bar_identity),
            "samples": training_data_hash(tuple(samples)),
        })
        with self._lock:
            existing = self._futures.get(task_id)
            if existing is not None and not existing.cancelled():
                return task_id

        def work():
            try:
                result = self.replayer.run(
                    instrument, tuple(bars), tuple(samples), listing_date=listing_date,
                )
                for outcome in result.outcomes:
                    self.repository.save_strategy_outcome(outcome)
                for outcome in result.joint_outcomes:
                    self.repository.save_joint_outcome(outcome)
                for snapshot in result.metric_snapshots:
                    self.repository.save_learning_metric_snapshot(snapshot)
                logger.info(
                    "Background strategy OOF completed task_id=%s instrument=%s folds=%d origins=%d outcomes=%d filled=%d joint=%d statuses=%s",
                    task_id, instrument.stable_key, result.fold_count,
                    result.tested_origins, len(result.outcomes), result.filled_count,
                    len(result.joint_outcomes), result.validation_statuses,
                )
                return result
            except Exception:
                logger.exception(
                    "Background strategy OOF failed task_id=%s instrument=%s",
                    task_id, instrument.stable_key,
                )
                raise

        with self._lock:
            existing = self._futures.get(task_id)
            if existing is not None and not existing.cancelled():
                return task_id
            self._futures[task_id] = self.executor.submit(work)
        return task_id

    def result(self, task_id, timeout=None):
        with self._lock:
            future = self._futures.get(task_id)
        return None if future is None else future.result(timeout)
