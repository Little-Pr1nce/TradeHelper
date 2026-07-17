from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event

import pytest

from portfolio_helpers import portfolio_batch, portfolio_batch_many, rebuild_batch, correlation_for
from strategy_helpers import position
from tests.v2.test_v212_production_e2e import _container
from tradehelper_v2.application.analysis import RuntimeAnalysisPipeline
from tradehelper_v2.contracts import (
    AnalysisRunStatus, AnalysisStage, DailyBarsRequest, DecisionMode, InstrumentId,
    Market, PlanAction, PortfolioAnalysisCommand, ProviderStatus, stable_hash,
)
from tradehelper_v2.contracts.providers import ProviderResult
from tradehelper_v2.data.cache import DataCache
from tradehelper_v2.data.service import DataProviders, DataRefreshService
from tradehelper_v2.portfolio import PortfolioDecisionEngine


def _command(account,watch,now,mode=DecisionMode.EOD):
    identity={"market":account.market,"mode":mode,"history":"6m","requested_at":now,"account":stable_hash(account),"watchlist":watch.watchlist_id,"force_refresh":False}
    return PortfolioAnalysisCommand(stable_hash(identity),account.market,mode,"6m",now,stable_hash(account),watch.watchlist_id)


def test_RL40_eleven_us_daily_requests_finish_without_tickflow_window_wait(us_instrument,bar_factory,now,calendar):
    calls=[]; instruments=tuple(InstrumentId.from_code(f"T{index}",Market.US,"XNAS") for index in range(11))
    def nasdaq(instrument,_,__):
        calls.append(instrument.code)
        return ProviderResult.success((bar_factory(instrument,date(2026,7,9)),),"nasdaq",now)
    service=DataRefreshService(DataProviders(nasdaq_daily=nasdaq),calendar,DataCache())
    result=service.refresh_daily_bars_batch(tuple(DailyBarsRequest(item,date(2026,7,9),date(2026,7,9)) for item in instruments),now)
    assert len(calls)==11 and not result.pending_retry_at


def test_RL41_eleventh_a_share_is_pending_and_recoverable(a_instrument,bar_factory,now,calendar):
    calls=[]; instruments=tuple(InstrumentId.from_code(f"{600000+index:06d}",Market.A) for index in range(11))
    def tickflow(instrument,_,__):
        calls.append(instrument.code)
        return ProviderResult.success((bar_factory(instrument,date(2026,7,9)),),"tickflow",now)
    service=DataRefreshService(DataProviders(tickflow_daily=tickflow),calendar,DataCache())
    result=service.refresh_daily_bars_batch(tuple(DailyBarsRequest(item,date(2026,7,9),date(2026,7,9)) for item in instruments),now)
    assert len(calls)==10
    assert result.results[instruments[-1]].status is ProviderStatus.RATE_LIMITED
    assert result.pending_retry_at[instruments[-1]]==now+timedelta(minutes=1)


def test_RL42_frozen_valuation_closes_from_real_cash_and_positions(tmp_path):
    container,account,_,now,instruments=_container(tmp_path,Market.US,DecisionMode.EOD)
    try:
        pipeline=RuntimeAnalysisPipeline(container)
        facts=pipeline._facts(instruments[0],DecisionMode.EOD,"6m",now)
        valuation=pipeline._valuation(account,{instruments[0]:facts},now)
        assert valuation.equity==valuation.cash+valuation.invested_value
        assert valuation.invested_value/valuation.equity<=Decimal("1")
    finally: container.close()


def test_RL43_failed_watch_item_degrades_only_that_item_but_failed_holding_blocks(tmp_path):
    container,account,watch,now,instruments=_container(tmp_path,Market.US,DecisionMode.EOD)
    pipeline=RuntimeAnalysisPipeline(container); command=_command(account,watch,now)
    watch_bars=container.data_refresh.values[instruments[1]]
    container.data_refresh.values[instruments[1]]=()
    try:
        document=pipeline.portfolio(command)
        assert document.report_id
        container.data_refresh.values[instruments[1]]=watch_bars
        container.data_refresh.values[instruments[0]]=()
        with pytest.raises(RuntimeError,match="HOLDING_ANALYSIS_UNAVAILABLE"):
            pipeline.portfolio(command)
    finally: container.close()


def test_RL44_protective_exit_is_ranked_before_new_risk(us_instrument,now):
    result=PortfolioDecisionEngine().decide(portfolio_batch(us_instrument,position=position(us_instrument)),now).conservative
    first=next(item for item in result.allocations if item.allocation_id==result.holding_priority_allocation_ids[0])
    assert first.action in {PlanAction.SELL,PlanAction.REDUCE}
    assert "PORTFOLIO_PROTECTIVE_EXIT_PRIORITY" in first.reason_codes


def test_RL45_replacement_is_research_only_and_requires_reanalysis(us_instrument,now):
    second=InstrumentId.from_code("MSFT",Market.US,"XNAS"); third=InstrumentId.from_code("NVDA",Market.US,"XNAS")
    batch=portfolio_batch_many((us_instrument,second,third),positions=(position(us_instrument),),cash=Decimal("500"))
    batch=rebuild_batch(batch,correlation_snapshot=correlation_for((us_instrument,second,third)))
    replacements=PortfolioDecisionEngine().decide(batch,now).aggressive.replacement_candidates
    assert replacements and all(item.reanalysis_required for item in replacements)


def test_RL46_holdings_and_watchlist_are_disjoint_immutable_snapshots(tmp_path):
    container,account,watch,_,_=_container(tmp_path,Market.US,DecisionMode.EOD)
    try:
        assert {item.instrument for item in account.positions}.isdisjoint(watch.instruments)
        with pytest.raises((AttributeError,TypeError)):
            setattr(watch,"instruments",(*watch.instruments,account.positions[0].instrument))
    finally: container.close()


def test_RL47_cancelling_portfolio_task_saves_no_partial_report(tmp_path):
    container,_,_,_,_=_container(tmp_path,Market.US,DecisionMode.EOD); started=Event(); release=Event()
    class SlowPipeline:
        def portfolio(self,command,on_progress=None):
            started.set(); release.wait(2); on_progress(AnalysisStage.RESOLVE_SUBJECT,None,"resume")
    container.analysis.pipeline=SlowPipeline()
    try:
        task_id=container.analysis.start_portfolio({"market":"US","mode":"eod","history_period":"6m"})
        assert started.wait(1) and container.analysis.cancel(task_id)
        release.set()
        with pytest.raises(RuntimeError,match="ANALYSIS_CANCELLED"):
            container.analysis._futures[task_id].result(timeout=3)
        assert container.repository._connection.execute("SELECT COUNT(*) FROM report_snapshots").fetchone()[0]==0
        run=container.repository.get_analysis_run(stable_hash({"command":task_id}))
        assert run.status is AnalysisRunStatus.CANCELLED
    finally: container.close()


def test_RL48_tab1_and_tab3_share_cache_but_each_refreshes_facts(tmp_path):
    container,_,_,now,instruments=_container(tmp_path,Market.US,DecisionMode.EOD); calls=0
    original=container.data_refresh.refresh_metadata
    def refresh(*args):
        nonlocal calls; calls+=1; return original(*args)
    container.data_refresh.refresh_metadata=refresh
    try:
        pipeline=RuntimeAnalysisPipeline(container)
        pipeline._facts(instruments[0],DecisionMode.EOD,"6m",now)
        pipeline._facts(instruments[0],DecisionMode.EOD,"6m",now)
        assert calls==2 and pipeline.container.data_refresh is container.data_refresh
    finally: container.close()


def test_RL49_portfolio_chain_never_constructs_simulated_one_hundred_thousand_capital():
    source=Path("tradehelper_v2/application/analysis.py").read_text(encoding="utf-8")
    assert "100000" not in source and "initial_capital" not in source
