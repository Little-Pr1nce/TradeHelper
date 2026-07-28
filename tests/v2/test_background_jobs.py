from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from time import perf_counter, sleep
from types import SimpleNamespace

import pytest

from tests.v2.test_v212_production_e2e import test_background_learning_records_four_horizons_idempotently
from application.analysis import AnalysisApplication, RuntimeAnalysisPipeline
from application.background import BackgroundLearningService, BackgroundResearchService
from config.settings import V2Settings
from contracts import (
    AccountSnapshot, AdjustmentMode, AnalysisRunStatus, CanonicalBar, DecisionMode,
    Exchange, InstrumentId, Market,
    ReportDocument, ReportSection, ResearchRunStatus, SingleStockAnalysisCommand, stable_hash,
)
from contracts.runtime import report_revision_invariant
from data.repository import SQLiteRepository
from release.smoke import _fixture
from runtime import build_runtime_container
from presentation.report_builder import PortfolioReportBuilder
from learning import LearningEngine, MaturityResolver
from presentation_helpers import portfolio_presentation
from test_learning_smoke import _forecast


def _rebuild(base, *, sections=None, summary=None):
    values={name:getattr(base,name) for name in base.__dataclass_fields__ if name not in {"report_id","generated_at"}}
    values["sections"]=sections or base.sections; values["summary"]=summary or base.summary
    identity={"kind":values["report_kind"],"market":values["market"],"instrument":values["instrument"],"mode":values["analysis_mode"],"as_of":values["as_of"],"title":values["title"],"subtitle":values["subtitle"],"summary":values["summary"],"sections":values["sections"],"glossary":values["glossary_entries"],"refs":values["source_artifact_refs"],"schema":values["schema_version"],"renderer":values["renderer_version"]}
    return ReportDocument(stable_hash(identity),**values,generated_at=datetime.now(timezone.utc))


def _analysis(tmp_path,pipeline):
    repo=SQLiteRepository(tmp_path/"v2.db"); now=datetime(2026,7,16,tzinfo=timezone.utc)
    repo.save_account_snapshot(AccountSnapshot(Market.US,"USD",1000,(),now))
    instrument=InstrumentId("AAPL",Market.US,Exchange.XNAS)
    identity={"instrument":instrument,"mode":DecisionMode.EOD,"history":"3m","requested_at":now,"account":stable_hash(repo.get_latest_account_snapshot(Market.US)),"force_refresh":False}
    command=SingleStockAnalysisCommand(stable_hash(identity),instrument,DecisionMode.EOD,"3m",now,identity["account"])
    return repo,AnalysisApplication(repo,pipeline,clock=lambda:now),command


def test_RL50_deterministic_report_does_not_wait_for_llm():
    base=_fixture(datetime.now(timezone.utc)); release=False
    def slow(_):
        sleep(.2); return None
    with ThreadPoolExecutor(1) as executor:
        service=BackgroundResearchService(object(),executor); started=perf_counter(); service.submit(base,slow)
        assert perf_counter()-started<.1 and base.report_id


def test_RL51_research_revision_can_change_only_research_sections(tmp_path):
    repo=SQLiteRepository(tmp_path/"v2.db"); base=_fixture(datetime.now(timezone.utc)); repo.save_report_document(base)
    research=ReportSection("research_notes","研究员观察","仅观察",None,base.sections[0].blocks)
    revised=_rebuild(base,sections=(*base.sections,research))
    with ThreadPoolExecutor(1) as executor:
        service=BackgroundResearchService(repo,executor); task=service.submit(base,lambda _:revised); result=service.result(task,2)
    try:
        assert result.status=="completed"
        assert report_revision_invariant(base)==report_revision_invariant(revised)
    finally: repo.close()


def test_RL52_invalid_llm_revision_keeps_the_base_report(tmp_path):
    repo=SQLiteRepository(tmp_path/"v2.db"); base=_fixture(datetime.now(timezone.utc)); repo.save_report_document(base)
    invalid=_rebuild(base,summary="LLM rewrote deterministic advice")
    with ThreadPoolExecutor(1) as executor:
        service=BackgroundResearchService(repo,executor); task=service.submit(base,lambda _:invalid); result=service.result(task,2)
    try:
        assert result.reason_code=="RESEARCH_CHANGED_INVARIANT_SECTION"
        assert repo.get_report_document(base.report_id)==base
    finally: repo.close()


def test_RL53_matured_data_updates_the_forecast_ledger_idempotently(tmp_path):
    test_background_learning_records_four_horizons_idempotently(tmp_path)


def test_RL54_deep_learning_executor_is_single_threaded(tmp_path):
    container=build_runtime_container(V2Settings.from_mapping({"work_dir":str(tmp_path)}))
    try: assert container.learning_executor._max_workers==1
    finally: container.close()


def test_RL55_rate_and_background_state_have_durable_repository_tables(tmp_path):
    repo=SQLiteRepository(tmp_path/"v2.db")
    try:
        names={row[0] for row in repo._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"provider_refresh_queue","analysis_runs","maturity_evidence"}<=names
    finally: repo.close()


def test_RL56_pipeline_failure_records_explicit_failure_without_fake_report(tmp_path):
    class Failing:
        def single_stock(self,*_,**__): raise RuntimeError("provider unavailable")
    repo,app,command=_analysis(tmp_path,Failing())
    try:
        with pytest.raises(RuntimeError): app.start_single(command)
        run=repo.get_analysis_run(stable_hash({"command":command.command_id}))
        assert run.status is AnalysisRunStatus.FAILED and run.reason_codes==("ANALYSIS_FAILED",)
    finally: repo.close()


def test_RL57_crash_before_report_persistence_leaves_no_partial_history(tmp_path):
    class Failing:
        def single_stock(self,*_,**__): raise RuntimeError("before report")
    repo,app,command=_analysis(tmp_path,Failing())
    try:
        with pytest.raises(RuntimeError): app.start_single(command)
        assert repo._connection.execute("SELECT COUNT(*) FROM report_snapshots").fetchone()[0]==0
    finally: repo.close()


def test_RL58_ui_callback_crash_after_persistence_keeps_recoverable_report(tmp_path):
    class Pipeline:
        def single_stock(self,command,on_progress=None): return _fixture(command.requested_at)
    repo,app,command=_analysis(tmp_path,Pipeline())
    try:
        document=app.start_single(command,on_complete=lambda _:(_ for _ in ()).throw(RuntimeError("ui crash")))
        assert repo.get_report_document(document.report_id)==document
        assert repo.get_analysis_run(stable_hash({"command":command.command_id})).status is AnalysisRunStatus.COMPLETED
    finally: repo.close()


def test_RL59_completed_command_retry_reuses_report_and_does_not_reissue(tmp_path):
    class Pipeline:
        calls=0
        def single_stock(self,command,on_progress=None): self.calls+=1; return _fixture(command.requested_at)
    pipeline=Pipeline(); repo,app,command=_analysis(tmp_path,pipeline)
    try:
        first=app.start_single(command); second=app.start_single(command)
        assert first.report_id==second.report_id and pipeline.calls==1
        assert repo._connection.execute("SELECT COUNT(*) FROM report_snapshots").fetchone()[0]==1
    finally: repo.close()


def test_background_learning_uses_new_revision_for_later_forecasts_in_same_batch(
    us_instrument, now,
):
    forecast = _forecast(us_instrument, now)
    old_bar = CanonicalBar(
        us_instrument,
        forecast.target_session_date,
        100,
        102,
        99,
        101,
        1000,
        AdjustmentMode.FRONT_ADJUSTED,
        "fixture-old",
        now,
    )
    prior = MaturityResolver().resolve(forecast, (old_bar,), evaluated_at=now)
    bar = CanonicalBar(
        us_instrument,
        forecast.target_session_date,
        100,
        103,
        99,
        102,
        1000,
        AdjustmentMode.FRONT_ADJUSTED,
        "fixture",
        now,
    )

    class Repository:
        def __init__(self):
            self.saved_evidence = []
            self.saved_outcomes = []

        def list_active_maturity_evidence(self, _instrument):
            return (prior,)

        def list_forecast_results(self, _instrument):
            return (forecast, forecast)

        def save_maturity_evidence(self, evidence):
            self.saved_evidence.append(evidence)

        def save_forecast_outcome(self, outcome):
            self.saved_outcomes.append(outcome)

    repository = Repository()
    with ThreadPoolExecutor(1) as executor:
        service = BackgroundLearningService(
            repository,
            LearningEngine(),
            executor,
            clock=lambda: now,
        )
        task_id = service.submit(us_instrument, (bar,))
        assert service.result(task_id, 2) == 1

    assert len(repository.saved_evidence) == 1
    assert repository.saved_evidence[0].revision == 2
    assert len(repository.saved_outcomes) == 1


def test_portfolio_research_uses_stable_chunks_and_returns_visible_empty_result(now, calendar):
    analysis_at=now.replace(hour=14)
    instruments=tuple(InstrumentId.from_code(code,Market.US) for code in (
        "AAPL","AMD","AVGO","FCX","GLW","LITE","MU","NVDA","SNDK","SPCX","WDC",
    ))
    presentation=portfolio_presentation(instruments,now=analysis_at,calendar=calendar)
    base=PortfolioReportBuilder().build(presentation)
    calls=[]
    class Engine:
        def run_chunks(self, **values):
            calls.append(values["invocations"])
            return {
                "status":ResearchRunStatus.COMPLETED,"reason":None,"responses":(),
                "hypotheses":(),"validations":(),"links":(),"candidates":(),
                "attempted_chunks":2,"completed_chunks":2,"failure_reasons":(),
            }
    container=SimpleNamespace(
        settings=SimpleNamespace(llm_base_url="https://example.invalid",llm_api_key="test",llm_model="fixture"),
        research_engine=Engine(),repository=SimpleNamespace(save_research_result=lambda *args,**kwargs:None),
    )
    pipeline=RuntimeAnalysisPipeline(container)
    pipeline._research_inputs[base.report_id]=presentation
    revised=pipeline._research_revision(base)
    assert len(calls)==1 and [len(item[0]) for item in calls[0]]==[10,1]
    assert "实际调用 2 个分片，成功 2 个" in next(section for section in revised.sections if section.section_id=="research").blocks[0].payload.rows[0].cells[1]
    table=next(section for section in revised.sections if section.section_id=="research").blocks[0].payload
    assert "调用完成" in table.rows[0].cells[1]
    assert "没有提出" in table.rows[0].cells[1]
