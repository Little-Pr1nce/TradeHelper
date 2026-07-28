from __future__ import annotations

from decimal import Decimal
import hashlib
import sqlite3

import pytest

from config.settings import V2Settings
from contracts import Market
from data.repository import SQLiteRepository
from migration import LegacyReader, MigrationExecutor, MigrationPlanner
from migration.config import merge_empty_settings
from migration.executor import MigrationExecutionError


def _source(tmp_path):
    path=tmp_path/"legacy.db"
    db=sqlite3.connect(path)
    db.execute("CREATE TABLE account_balance(a_balance TEXT,us_balance TEXT)")
    db.execute("INSERT INTO account_balance VALUES('100.10','200.20')")
    db.execute("CREATE TABLE holdings(code TEXT,shares TEXT,cost_price TEXT)")
    db.execute("INSERT INTO holdings VALUES('600519','100','1500.25')")
    db.execute("INSERT INTO holdings VALUES('AAPL','2.5','190.125')")
    db.execute("CREATE TABLE watchlist(code TEXT)")
    db.execute("INSERT INTO watchlist VALUES('600519')")
    db.execute("INSERT INTO watchlist VALUES('AMD')")
    db.commit(); db.close()
    return path


def _execute(path, target):
    repo=SQLiteRepository(target)
    reader=LegacyReader(path)
    plan=MigrationPlanner(reader).build()
    run=MigrationExecutor(reader,repo).execute(plan,confirm=True)
    return repo,plan,run


def test_RL10_preflight_is_read_only_and_preserves_v1_hash_and_mtime(tmp_path):
    path=_source(tmp_path)
    before=(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns)
    LegacyReader(path).preflight()
    assert before==(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns)


def test_RL11_missing_v1_source_produces_stable_new_user_plan(tmp_path):
    plan=MigrationPlanner(LegacyReader(tmp_path/"missing.db")).build()
    assert len(plan.items)==1
    assert plan.items[0].reason_codes==("MIGRATION_SOURCE_MISSING",)


def test_RL12_nonempty_v2_settings_win_and_v1_only_fills_blanks(tmp_path):
    merged=merge_empty_settings(
        {"work_dir":str(tmp_path),"llm_model":"v2-model","stock_token_us":""},
        {"llm_model":"legacy-model","stock_token_us":"legacy-token"},
    )
    assert merged["llm_model"]=="v2-model"
    assert merged["stock_token_us"]=="legacy-token"


def test_RL13_migrated_secrets_are_absent_from_public_outputs(tmp_path):
    settings=V2Settings.from_mapping({"work_dir":str(tmp_path),"llm_api_key":"top-secret","stock_token_us":"stock-secret"})
    assert "top-secret" not in str(settings.to_public_mapping())
    assert "stock-secret" not in str(settings.to_public_mapping())
    plan=MigrationPlanner(LegacyReader(_source(tmp_path))).build()
    assert "top-secret" not in repr(plan) and "stock-secret" not in repr(plan)


def test_RL14_balances_and_positions_split_into_a_and_us_accounts(tmp_path):
    repo,_,_=_execute(_source(tmp_path),tmp_path/"v2.db")
    try:
        a=repo.get_latest_account_snapshot(Market.A); us=repo.get_latest_account_snapshot(Market.US)
        assert a.cash==Decimal("100.10") and {item.instrument.code for item in a.positions}=={"600519"}
        assert us.cash==Decimal("200.20") and {item.instrument.code for item in us.positions}=={"AAPL"}
    finally:
        repo.close()


def test_RL15_cash_shares_and_cost_keep_decimal_precision(tmp_path):
    repo,_,_=_execute(_source(tmp_path),tmp_path/"v2.db")
    try:
        us=repo.get_latest_account_snapshot(Market.US)
        assert us.cash==Decimal("200.20")
        assert us.positions[0].shares==Decimal("2.5")
        assert us.positions[0].cost_price==Decimal("190.125")
    finally:
        repo.close()


def test_RL16_invalid_instrument_shares_and_cost_are_quarantined(tmp_path):
    path=tmp_path/"bad.db"; db=sqlite3.connect(path)
    db.execute("CREATE TABLE holdings(code,shares,cost_price)")
    db.executemany("INSERT INTO holdings VALUES(?,?,?)",(("",1,1),("AAPL",-1,1),("AMD",1,-2)))
    db.commit(); db.close()
    plan=MigrationPlanner(LegacyReader(path)).build()
    assert len(plan.items)==3 and all(item.status.value=="quarantined" for item in plan.items)


def test_RL17_held_symbol_is_removed_from_watchlist_with_reason(tmp_path):
    plan=MigrationPlanner(LegacyReader(_source(tmp_path))).build()
    held_watch=next(item for item in plan.items if item.source_table=="watchlist" and dict(item.payload)["code"]=="600519")
    assert held_watch.reason_codes==("MIGRATION_HELD_REMOVED_FROM_WATCHLIST",)


def test_RL18_same_source_fingerprint_does_not_duplicate_writes(tmp_path):
    path=_source(tmp_path); repo,plan,first=_execute(path,tmp_path/"v2.db")
    try:
        second=MigrationExecutor(LegacyReader(path),repo).execute(plan,confirm=True)
        assert second.run_id==first.run_id
        assert repo._connection.execute("SELECT COUNT(*) FROM legacy_migration_items").fetchone()[0]==len(plan.items)
        assert repo._connection.execute("SELECT COUNT(*) FROM account_positions").fetchone()[0]==2
    finally:
        repo.close()


def test_migrated_watchlist_survives_repository_restart(tmp_path):
    target=tmp_path/"v2.db"
    repo,_,_=_execute(_source(tmp_path),target)
    repo.close()
    reopened=SQLiteRepository(target)
    try:
        watch=reopened.latest_watchlist_snapshot(Market.US)
        assert watch is not None
        assert tuple(item.code for item in watch.instruments)==("AMD",)
        assert reopened._connection.execute(
            "SELECT schema_version FROM watchlist_snapshots WHERE watchlist_id=?",
            (watch.watchlist_id,),
        ).fetchone()[0]==1
    finally:
        reopened.close()


def test_RL19_failed_migration_rolls_back_business_rows_and_preserves_v1(tmp_path,monkeypatch):
    path=_source(tmp_path); before=hashlib.sha256(path.read_bytes()).hexdigest()
    repo=SQLiteRepository(tmp_path/"v2.db"); reader=LegacyReader(path); plan=MigrationPlanner(reader).build(); executor=MigrationExecutor(reader,repo)
    def fail(db,*_):
        db.execute("INSERT INTO account_snapshots(market,currency,cash,captured_at,schema_version) VALUES('US','USD','999','2026-01-01T00:00:00+00:00',17)")
        raise RuntimeError("forced failure")
    monkeypatch.setattr(executor,"_write_items",fail)
    try:
        with pytest.raises(MigrationExecutionError): executor.execute(plan,confirm=True)
        assert repo._connection.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]==0
        assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    finally:
        repo.close()
