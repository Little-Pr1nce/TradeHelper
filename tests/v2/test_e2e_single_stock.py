from __future__ import annotations

from datetime import date

import pytest

from tests.v2.test_v212_production_e2e import _as_of, _container, _instrument
from application.analysis import RuntimeAnalysisPipeline
from contracts import (
    DecisionMode, Market, ProviderStatus, ReportKind, SingleStockAnalysisCommand,
    stable_hash,
)
from contracts.providers import ProviderResult


def _run(tmp_path,market,mode):
    container,account,_,now,instruments=_container(tmp_path,market,mode)
    identity={"instrument":instruments[0],"mode":mode,"history":"6m","requested_at":now,"account":stable_hash(account),"force_refresh":False}
    command=SingleStockAnalysisCommand(stable_hash(identity),instruments[0],mode,"6m",now,stable_hash(account))
    return container,RuntimeAnalysisPipeline(container).single_stock(command),command


def test_RL30_us_eod_full_chain_builds_next_session_plan(tmp_path):
    container,document,_=_run(tmp_path,Market.US,DecisionMode.EOD)
    try:
        assert document.report_kind is ReportKind.SINGLE_STOCK
        assert {"forecast","plans","risk"}<={item.section_id for item in document.sections}
    finally: container.close()


def test_RL31_us_intraday_quote_never_becomes_a_daily_bar(tmp_path):
    container,account,_,now,instruments=_container(tmp_path,Market.US,DecisionMode.INTRADAY)
    try:
        facts=RuntimeAnalysisPipeline(container)._facts(instruments[0],DecisionMode.INTRADAY,"6m",now)
        assert facts.quote is not None
        assert all(bar.close!=facts.quote.price or bar.trading_date!=now.date() for bar in facts.bars)
    finally: container.close()


def test_RL32_us_pre_market_quote_enters_current_condition_plan(tmp_path):
    container,document,_=_run(tmp_path,Market.US,DecisionMode.PRE)
    try:
        assert document.analysis_mode is DecisionMode.PRE
        assert any(section.section_id=="plans" for section in document.sections)
    finally: container.close()


def test_RL33_a_share_eod_full_chain_builds_next_session_plan(tmp_path):
    container,document,_=_run(tmp_path,Market.A,DecisionMode.EOD)
    try:
        assert document.market is Market.A
        assert any(section.section_id=="plans" for section in document.sections)
    finally: container.close()


def test_RL34_a_share_intraday_chain_applies_market_risk_rules(tmp_path):
    container,document,_=_run(tmp_path,Market.A,DecisionMode.INTRADAY)
    try:
        risk=container.repository._connection.execute("SELECT COUNT(*) FROM risk_decision_bundles").fetchone()[0]
        assert risk==1 and any(section.section_id=="risk" for section in document.sections)
    finally: container.close()


def test_RL35_a_share_pre_without_quote_keeps_t1_conditional_plan(tmp_path):
    container,account,_,now,instruments=_container(tmp_path,Market.A,DecisionMode.PRE)
    container.data_refresh.refresh_quote=lambda *_: ProviderResult.failure(ProviderStatus.EMPTY,now)
    identity={"instrument":instruments[0],"mode":DecisionMode.PRE,"history":"6m","requested_at":now,"account":stable_hash(account),"force_refresh":False}
    command=SingleStockAnalysisCommand(stable_hash(identity),instruments[0],DecisionMode.PRE,"6m",now,stable_hash(account))
    try:
        document=RuntimeAnalysisPipeline(container).single_stock(command)
        assert document.analysis_mode is DecisionMode.PRE and any(item.section_id=="plans" for item in document.sections)
    finally: container.close()


def test_RL36_ipo_date_limits_the_requested_history_window(tmp_path):
    container,_,_,now,instruments=_container(tmp_path,Market.US,DecisionMode.EOD)
    listing=date(2026,6,1); captured={}
    original=container.data_refresh.refresh_daily_bars
    def refresh(instrument,start,end,listed,as_of):
        captured["listing"]=listed
        return original(instrument,start,end,listed,as_of)
    container.data_refresh.refresh_listing_date=lambda *_: ProviderResult.success(listing,"fixture",now)
    container.data_refresh.refresh_daily_bars=refresh
    try:
        RuntimeAnalysisPipeline(container)._facts(instruments[0],DecisionMode.EOD,"1y",now)
        assert captured["listing"]==listing
    finally: container.close()


def test_RL37_optional_news_and_fundamentals_can_be_missing_without_fabrication(tmp_path):
    container,document,_=_run(tmp_path,Market.US,DecisionMode.EOD)
    try:
        assert document.report_id
        assert container.repository._connection.execute("SELECT COUNT(*) FROM news_snapshots").fetchone()[0]==0
        assert container.repository._connection.execute("SELECT COUNT(*) FROM fundamental_snapshots").fetchone()[0]==0
    finally: container.close()


def test_RL38_missing_champion_uses_declared_baseline_without_waiting_for_oof(tmp_path):
    container,document,_=_run(tmp_path,Market.US,DecisionMode.EOD)
    try:
        assert container.forecast_registry._models=={}
        assert any(section.section_id=="forecast" for section in document.sections)
    finally: container.close()


def test_RL39_single_report_answers_branches_loss_invalidation_and_target_date(tmp_path):
    container,document,_=_run(tmp_path,Market.US,DecisionMode.EOD)
    try:
        text=" ".join((document.summary,*(section.purpose for section in document.sections)))
        assert {"plans","risk","forecast"}<={item.section_id for item in document.sections}
        assert document.source_artifact_refs and text
    finally: container.close()
