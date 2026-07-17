"""确定性报告与后台研究解耦；LLM 超时/失败只影响 revision，不影响主报告。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import Executor
from threading import Lock
from tradehelper_v2.contracts.runtime import ReportRevisionLink, RevisionKind, report_revision_invariant
from tradehelper_v2.contracts.market_data import stable_hash
from tradehelper_v2.learning.maturity import MaturityResolver

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
                if revised is None: return BackgroundTaskResult(task_id,"failed",None,"RESEARCH_UNAVAILABLE")
                invariant=report_revision_invariant(base_document)
                if report_revision_invariant(revised)!=invariant:
                    return BackgroundTaskResult(task_id,"failed",None,"RESEARCH_CHANGED_INVARIANT_SECTION")
                self.repository.save_report_document(revised)
                link=ReportRevisionLink(stable_hash({"base":base_document.report_id,"revised":revised.report_id,"kind":RevisionKind.RESEARCH_ENRICHED,"invariant":invariant}),base_document.report_id,revised.report_id,RevisionKind.RESEARCH_ENRICHED,invariant,self.clock())
                self.repository.save_report_revision_link(link)
                return BackgroundTaskResult(task_id,"completed",revised.report_id,None)
            except Exception:
                return BackgroundTaskResult(task_id,"failed",None,"RESEARCH_FAILED")
        with self._lock:
            existing=self._futures.get(task_id)
            if existing is not None and not existing.cancelled(): return task_id
            self._futures[task_id]=self.executor.submit(work)
        return task_id
    def result(self, task_id, timeout=None):
        with self._lock: future=self._futures.get(task_id)
        return None if future is None else future.result(timeout)


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
            evaluated_at=self.clock(); saved=0
            previous_values=self.repository.list_active_maturity_evidence(instrument)
            for forecast in self.repository.list_forecast_results(instrument):
                previous=next((item for item in previous_values if item.origin_session_date==forecast.origin_session_date and item.target_session_date==(forecast.target_session_date or forecast.origin_session_date)),None)
                if previous is not None and previous.status.value=="matured": continue
                if previous is not None and previous.status.value=="pending" and forecast.target_session_date is not None and evaluated_at.date()<=forecast.target_session_date: continue
                evidence=MaturityResolver().resolve(forecast,bars,evaluated_at=evaluated_at,previous=previous,listing_date=listing_date)
                self.repository.save_maturity_evidence(evidence)
                outcome=self.learning_engine.evaluate_forecast(forecast,bars,evaluated_at=evaluated_at,previous_evidence=previous,listing_date=listing_date)
                self.repository.save_forecast_outcome(outcome); saved+=1
            return saved
        with self._lock:
            existing=self._futures.get(task_id)
            if existing is not None and not existing.cancelled(): return task_id
            self._futures[task_id]=self.executor.submit(work)
        return task_id

    def result(self, task_id, timeout=None):
        with self._lock: future=self._futures.get(task_id)
        return None if future is None else future.result(timeout)
