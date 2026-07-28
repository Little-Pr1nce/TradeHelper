from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

from config.settings import V2Settings
from data.repository import SQLiteRepository
from migration import LegacyReader, MigrationExecutor, MigrationPlanner
from runtime.lifecycle import start_runtime


def _source(tmp_path):
    path=tmp_path/"legacy.db"; db=sqlite3.connect(path)
    statements=(
        ("CREATE TABLE stocks(code,name,listing_date)",("AAPL","Apple","2020-01-01")),
        ("CREATE TABLE price_history(code,date,close)",("AAPL","2019-12-31",1)),
        ("CREATE TABLE intraday_price_history(code,date,price)",("AAPL","2020-01-02",2)),
        ("CREATE TABLE news_sentiment(code,title,sentiment)",("AAPL","old","positive")),
        ("CREATE TABLE reports(id,title,content)",(1,"old","legacy report")),
        ("CREATE TABLE prediction_log(code,prediction)",("AAPL","up")),
        ("CREATE TABLE trade_plan_log(code,action)",("AAPL","buy")),
        ("CREATE TABLE joint_oof_runs(code,score)",("AAPL",1)),
        ("CREATE TABLE per_stock_params(code,value)",("AAPL","x")),
        ("CREATE TABLE bt_variant_cache(code,value)",("AAPL","x")),
    )
    for ddl,row in statements:
        db.execute(ddl); db.execute(f"INSERT INTO {ddl.split()[2].split('(')[0]} VALUES({','.join('?' for _ in row)})",row)
    db.commit(); db.close(); return path


def _run(tmp_path):
    path=_source(tmp_path); repo=SQLiteRepository(tmp_path/"tradehelper_v2.db"); reader=LegacyReader(path)
    plan=MigrationPlanner(reader).build(); run=MigrationExecutor(reader,repo).execute(plan,confirm=True)
    return path,repo,plan,run


def test_RL20_legacy_price_history_never_enters_v2_daily_bars(tmp_path):
    _,repo,_,_=_run(tmp_path)
    try: assert repo._connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]==0
    finally: repo.close()


def test_RL21_legacy_intraday_rows_never_enter_daily_or_quote_facts(tmp_path):
    _,repo,_,_=_run(tmp_path)
    try:
        assert repo._connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]==0
        assert repo._connection.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0]==0
    finally: repo.close()


def test_RL22_legacy_news_cache_never_enters_formal_news_or_fundamentals(tmp_path):
    _,repo,_,_=_run(tmp_path)
    try:
        assert repo._connection.execute("SELECT COUNT(*) FROM news_snapshots").fetchone()[0]==0
        assert repo._connection.execute("SELECT COUNT(*) FROM fundamental_snapshots").fetchone()[0]==0
    finally: repo.close()


def test_RL23_legacy_report_is_read_only_archive_with_original_content(tmp_path):
    _,repo,_,_=_run(tmp_path)
    try:
        row=repo._connection.execute("SELECT content FROM legacy_report_archives").fetchone()
        assert row[0]=="legacy report"
        assert repo._connection.execute("SELECT COUNT(*) FROM report_snapshots").fetchone()[0]==0
    finally: repo.close()


def test_RL24_old_forecast_strategy_and_joint_rows_do_not_enter_three_ledgers(tmp_path):
    _,repo,_,_=_run(tmp_path)
    try:
        assert repo._connection.execute("SELECT COUNT(*) FROM forecast_outcomes").fetchone()[0]==0
        assert repo._connection.execute("SELECT COUNT(*) FROM strategy_outcomes").fetchone()[0]==0
        assert repo._connection.execute("SELECT COUNT(*) FROM joint_outcomes").fetchone()[0]==0
    finally: repo.close()


def test_RL25_old_parameters_and_backtests_do_not_create_candidates_or_champions(tmp_path):
    _,repo,_,_=_run(tmp_path)
    try:
        assert repo._connection.execute("SELECT COUNT(*) FROM learning_candidate_versions").fetchone()[0]==0
        assert repo._connection.execute("SELECT COUNT(*) FROM forecast_model_versions WHERE lifecycle='champion'").fetchone()[0]==0
    finally: repo.close()


def test_RL26_pre_listing_bar_is_counted_and_rejected(tmp_path):
    _,repo,plan,_=_run(tmp_path)
    try:
        item=next(item for item in plan.items if item.source_table=="price_history")
        assert "MIGRATION_PRE_LISTING_BAR_REJECTED" in item.reason_codes
        assert repo._connection.execute("SELECT COUNT(*) FROM legacy_evidence_archives WHERE source_table='price_history'").fetchone()[0]==1
    finally: repo.close()


def test_RL27_unknown_us_exchange_creates_alias_without_mutating_market_history(tmp_path):
    _,repo,_,_=_run(tmp_path)
    try:
        alias=repo._connection.execute("SELECT canonical_instrument_key FROM instrument_aliases WHERE legacy_code='AAPL'").fetchone()
        assert alias is not None and "US" in alias[0]
        assert repo._connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]==0
    finally: repo.close()


def test_RL28_completed_migration_is_not_prompted_again_after_restart(tmp_path):
    path,repo,_,_=_run(tmp_path); repo.close()
    lifecycle=start_runtime(V2Settings.from_mapping({"work_dir":str(tmp_path)}),migration_source=path)
    try:
        assert lifecycle.container.migration_status=="completed"
        assert lifecycle.migration_source is None
    finally: lifecycle.close()


def test_RL29_backup_manifest_contains_path_size_and_sha256(tmp_path):
    path,repo,_,run=_run(tmp_path)
    try:
        backup=Path(run.backup_path); manifest=LegacyReader(backup).source.backup_manifest()
        assert manifest["path"]==str(backup.resolve())
        assert manifest["size"]==path.stat().st_size
        assert manifest["sha256"]==hashlib.sha256(path.read_bytes()).hexdigest()
    finally: repo.close()
