from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from tests.v2.test_v212_production_e2e import (
    _container, test_portfolio_real_chain_all_markets_and_modes,
    test_single_stock_real_chain_all_markets_and_modes,
)
from application.analysis import AnalysisApplication
from config.settings import V2Settings
from contracts import (
    AccountSnapshot, AnalysisTaskProgress, DecisionMode, InstrumentId, Market,
    WatchlistSnapshot, stable_hash,
)
from data.repository import SQLiteRepository
from release.smoke import _fixture


def test_RL70_tab1_tab3_market_and_mode_matrix_runs_all_twelve_cells(tmp_path):
    cells=[]
    for market in Market:
        for mode in DecisionMode:
            test_single_stock_real_chain_all_markets_and_modes(tmp_path/f"single-{market.value}-{mode.value}",market,mode)
            cells.append(("tab1",market,mode))
            test_portfolio_real_chain_all_markets_and_modes(tmp_path/f"portfolio-{market.value}-{mode.value}",market,mode)
            cells.append(("tab3",market,mode))
    assert len(cells)==12


def test_RL71_cached_tab1_p95_is_under_five_seconds(tmp_path):
    now=datetime(2026,7,16,tzinfo=timezone.utc); repo=SQLiteRepository(tmp_path/"v2.db")
    repo.save_account_snapshot(AccountSnapshot(Market.US,"USD",1000,(),now))
    class Pipeline:
        def single_stock(self,command,on_progress=None): return _fixture(command.requested_at)
    app=AnalysisApplication(repo,Pipeline(),clock=lambda:now); command=app._single_command({"market":"US","symbol":"AAPL","mode":"eod"})
    try:
        app.start_single(command)
        values=[]
        for _ in range(20):
            started=perf_counter(); app.start_single(command); values.append(perf_counter()-started)
        assert sorted(values)[18]<5
    finally: repo.close()


def test_RL72_cold_analysis_emits_typed_stage_progress_until_completion(tmp_path):
    container,_,_,_,_=_container(tmp_path,Market.US,DecisionMode.EOD); progress=[]
    try:
        task=container.analysis.start_single({"market":"US","symbol":"AAPL","mode":"eod","history_period":"6m"},on_progress=progress.append)
        container.analysis._futures[task].result(timeout=15)
        assert progress and all(isinstance(item,AnalysisTaskProgress) for item in progress)
        assert progress[0].status.value=="queued" and progress[-1].status.value=="completed"
    finally: container.close()


def test_RL73_cached_ten_stock_tab3_p95_is_under_twenty_seconds(tmp_path):
    now=datetime(2026,7,16,tzinfo=timezone.utc); repo=SQLiteRepository(tmp_path/"v2.db")
    account=AccountSnapshot(Market.US,"USD",10000,(),now); repo.save_account_snapshot(account)
    instruments=tuple(InstrumentId.from_code(f"T{index}",Market.US,"XNAS") for index in range(10))
    watch=WatchlistSnapshot(stable_hash({"market":Market.US,"instruments":instruments,"created":now}),Market.US,instruments,now); repo.save_watchlist_snapshot(watch)
    class Pipeline:
        def portfolio(self,command,on_progress=None): return _fixture(command.requested_at)
    app=AnalysisApplication(repo,Pipeline(),clock=lambda:now); command=app._portfolio_command({"market":"US","mode":"eod"})
    try:
        app.start_portfolio(command)
        values=[]
        for _ in range(20):
            started=perf_counter(); app.start_portfolio(command); values.append(perf_counter()-started)
        assert sorted(values)[18]<20
    finally: repo.close()


def test_RL74_concurrent_repository_reads_and_serialized_writes_do_not_lock(tmp_path):
    repo=SQLiteRepository(tmp_path/"v2.db"); start=datetime(2026,7,16,tzinfo=timezone.utc)
    def work(index):
        repo.save_account_snapshot(AccountSnapshot(Market.US,"USD",Decimal(index),(),start+timedelta(seconds=index)))
        return repo.get_latest_account_snapshot(Market.US)
    try:
        with ThreadPoolExecutor(8) as executor: values=tuple(executor.map(work,range(1,41)))
        assert len(values)==40 and repo.get_latest_account_snapshot(Market.US).cash==Decimal("40")
    finally: repo.close()


def test_RL75_v1_business_directories_have_exited_the_v2_runtime_surface():
    source=Path("tradehelper.spec").read_text(encoding="utf-8")
    assert all(f'"{name}"' in source for name in ("alpha","backtest","core","services"))
    assert all(f'"{name}"' not in source for name in ("config","data","strategies","ui"))
    assert not any(
        f"from {name}" in Path("main.py").read_text(encoding="utf-8")
        for name in ("alpha","backtest","core","report","services")
    )


def test_RL76_dependency_locks_are_pinned_for_clean_python_312_install():
    runtime=Path("requirements-lock.txt").read_text(encoding="utf-8").splitlines()
    assert runtime and all(not line.strip() or line.startswith("#") or "==" in line for line in runtime)
    assert "python-version: '3.12'" in Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")


def test_RL79_final_reports_answer_action_conditions_risk_history_and_forecast_for_both_markets(tmp_path):
    for market in Market:
        container,_,_,_,_=_container(tmp_path/market.value,market,DecisionMode.EOD)
        try:
            task=container.analysis.start_single({"market":market.value,"symbol":"AAPL" if market is Market.US else "600519","mode":"eod","history_period":"6m"})
            document=container.analysis._futures[task].result(timeout=15)
            assert {
                "action_summary","facts","forecast","operation_report",
                "strategy_performance","research","history",
            }=={item.section_id for item in document.sections}
        finally: container.close()
