"""V2-12 迁移执行器：备份、指纹复核、单事务写入和失败回滚。"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import shutil

from tradehelper_v2.contracts.account import AccountSnapshot, PositionSnapshot
from tradehelper_v2.contracts.enums import Exchange, Market
from tradehelper_v2.contracts.market_data import InstrumentId, canonical_json, stable_hash, utc_iso
from tradehelper_v2.contracts.migration import MigrationPlan, MigrationRun
from tradehelper_v2.contracts.runtime import MigrationItemStatus, MigrationRunStatus
from tradehelper_v2.contracts.presentation import WatchlistSnapshot
from tradehelper_v2.data.repository import SQLiteRepository
from .legacy_reader import LegacyReader

class MigrationExecutionError(RuntimeError):
    """迁移未能安全完成；调用方应保留 V1 并检查 quarantine。"""

class MigrationExecutor:
    def __init__(self, reader: LegacyReader, repository: SQLiteRepository, *, clock=None):
        self.reader=reader; self.repository=repository; self.clock=clock or (lambda: datetime.now(timezone.utc))

    def backup(self, destination: Path | str) -> Path:
        if not self.reader.source.exists: raise MigrationExecutionError("V1 source is missing")
        target=Path(destination); target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.reader.source.path, target)
        return target

    def execute(self, plan: MigrationPlan, *, confirm: bool=False, backup_path: Path | str | None=None) -> MigrationRun:
        if not confirm: raise MigrationExecutionError("migration requires explicit confirmation")
        if self.reader.source.fingerprint() != plan.source_fingerprint:
            raise MigrationExecutionError("MIGRATION_SOURCE_CHANGED_AFTER_PREFLIGHT")
        completed=self.repository.find_completed_migration(plan.source_fingerprint, plan.migration_version)
        if completed is not None:
            return completed
        backup = self.backup(backup_path or (self.repository.database_path.parent / "migration-backup" / f"{plan.source_fingerprint}.db"))
        started=self.clock(); run=MigrationRun(stable_hash({"plan":plan.plan_id,"source":plan.source_fingerprint,"version":plan.migration_version}),plan.plan_id,plan.source_fingerprint,plan.migration_version,MigrationRunStatus.RUNNING,str(backup),started,None,())
        # migration run record is written before the data transaction for observability.
        self.repository.save_migration_run(run, plan=plan)
        try:
            with self.repository._transaction() as db:
                self._write_items(db, plan, run)
            status=MigrationRunStatus.COMPLETED_WITH_QUARANTINE if any(i.status is MigrationItemStatus.QUARANTINED for i in plan.items) else MigrationRunStatus.COMPLETED
            finished=self.clock(); completed=MigrationRun(run.run_id,run.plan_id,run.source_fingerprint,run.migration_version,status,run.backup_path,run.started_at,finished,tuple(sorted({reason for i in plan.items for reason in i.reason_codes})))
            self.repository.save_migration_run(completed, plan=plan)
            return completed
        except Exception as exc:
            # _transaction 已经 rollback；失败记录单独提交，不能污染正式事实。
            failed=MigrationRun(run.run_id,run.plan_id,run.source_fingerprint,run.migration_version,MigrationRunStatus.FAILED,run.backup_path,run.started_at,self.clock(),("MIGRATION_TRANSACTION_ROLLED_BACK",))
            self.repository.save_migration_run(failed, plan=plan)
            raise MigrationExecutionError(str(exc)) from exc

    @staticmethod
    def _value(row, *names, default=None):
        for name in names:
            if name in row and row[name] is not None: return row[name]
        return default

    def _write_items(self, db, plan: MigrationPlan, run: MigrationRun):
        # 先登记每一项；正式写入再按依赖顺序执行，不能让哈希排序决定
        # “账户现金”还是“持仓”先落库，否则持仓可能先创建现金为 0 的快照。
        for item in plan.items:
            payload=dict(item.payload)
            encoded=canonical_json(item)
            digest=stable_hash(item)
            db.execute("INSERT OR IGNORE INTO legacy_migration_items(item_id,event_key,run_id,source_table,source_key,target_kind,status,reason_codes_json,payload_hash,payload_json,created_at,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (item.item_id,item.item_id,run.run_id,item.source_table,item.source_key,item.target_kind,item.status.value,json.dumps(item.reason_codes),digest,encoded,utc_iso(item.created_at),17))
            if item.status in {MigrationItemStatus.QUARANTINED, MigrationItemStatus.REJECTED}:
                db.execute("INSERT INTO quarantine_records(record_type,instrument_key,trading_date,reason,payload_json,created_at) VALUES(?,?,?,?,?,?)",("legacy_migration_item",None,None,";".join(item.reason_codes) or "MIGRATION_ITEM_REJECTED",encoded,utc_iso(self.clock())))
        priority={"account_snapshot":0,"account_snapshot_position":1,"watchlist":2,"stock_metadata_clue":3}
        migrated=sorted(
            (item for item in plan.items if item.status is MigrationItemStatus.MIGRATED),
            key=lambda item:(priority.get(item.target_kind,10),item.item_id),
        )
        for item in migrated:
            payload=dict(item.payload)
            if item.target_kind == "account_snapshot":
                self._write_account_row(db, payload, plan)
            elif item.target_kind == "account_snapshot_position":
                self._write_position(db, payload, plan)
            elif item.target_kind == "watchlist":
                self._write_alias(db, payload, plan, status="watchlist")
            elif item.target_kind == "stock_metadata_clue":
                self._write_alias(db, payload, plan, status="metadata_clue")
            elif item.target_kind == "legacy_report_archive":
                self._write_report_archive(db, payload, plan, run)
            elif item.target_kind == "legacy_evidence_archive":
                self._write_evidence_archive(db, payload, plan, run, item)
        # archived_only 项目同样要留审计证据，但永远不进入 daily/news/fundamental 正式表。
        for item in plan.items:
            if item.status is MigrationItemStatus.ARCHIVED_ONLY:
                payload=dict(item.payload)
                self._write_report_archive(db,payload,plan,run) if item.target_kind == "legacy_report_archive" else self._write_evidence_archive(db,payload,plan,run,item)
        # 关注列表是不可变快照；成员先去重，并由计划状态保证不含已持仓股票。
        watch=[]
        for item in plan.items:
            if item.source_table != "watchlist" or item.status is not MigrationItemStatus.MIGRATED: continue
            row=dict(item.payload); code=str(self._value(row,"code","symbol","ticker",default="")).strip().upper(); market=Market.A if code.isdigit() and len(code)==6 else Market.US
            try: watch.append(InstrumentId.from_code(code,market))
            except Exception: pass
        for market in Market:
            instruments=tuple(sorted(set(item for item in watch if item.market is market),key=lambda item:item.stable_key))
            if not instruments: continue
            snapshot=WatchlistSnapshot(stable_hash({"market":market,"instruments":instruments,"created":plan.created_at}),market,instruments,plan.created_at); payload=canonical_json(snapshot); digest=stable_hash(snapshot)
            db.execute("INSERT OR IGNORE INTO watchlist_snapshots(watchlist_id,event_key,market,payload_hash,payload_json,created_at,schema_version) VALUES(?,?,?,?,?,?,?)",(snapshot.watchlist_id,snapshot.watchlist_id,market.value,digest,payload,utc_iso(plan.created_at),17))
            db.executemany("INSERT OR IGNORE INTO watchlist_snapshot_members(watchlist_id,instrument_key,position) VALUES(?,?,?)",[(snapshot.watchlist_id,item.stable_key,index) for index,item in enumerate(instruments)])

    def _write_account_row(self, db, row, plan):
        # 计划创建时点是快照业务身份的一部分；重启重跑不会因当前时间不同而生成第二份账户。
        captured=plan.created_at; a=self._dec(self._value(row,"a_balance","cash_a","cny","cash",default=0)); us=self._dec(self._value(row,"us_balance","cash_us","usd",default=0))
        for market,cash,currency in ((Market.A,a,"CNY"),(Market.US,us,"USD")):
            db.execute("INSERT OR IGNORE INTO account_snapshots(market,currency,cash,captured_at,schema_version) VALUES(?,?,?,?,?)",(market.value,currency,str(cash),utc_iso(captured),17))
    def _write_alias(self, db, row, plan, *, status):
        code=str(self._value(row,"code","symbol","ticker",default="")).strip().upper()
        if not code: raise MigrationExecutionError("cannot migrate an empty instrument alias")
        market=Market.A if code.isdigit() and len(code)==6 else Market.US
        try: inst=InstrumentId.from_code(code,market)
        except Exception as exc: raise MigrationExecutionError(f"invalid instrument alias {code}") from exc
        aid=stable_hash({"source":plan.source_fingerprint,"code":code,"status":status})
        payload={"alias_id":aid,"legacy_code":code,"market":market.value,"canonical_instrument_key":inst.stable_key,"status":status,"source":"v1"}
        encoded=json.dumps(payload,sort_keys=True,separators=(",",":")); db.execute("INSERT OR IGNORE INTO instrument_aliases(alias_id,event_key,market,legacy_code,canonical_instrument_key,status,source,created_at,payload_hash,payload_json,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(aid,aid,market.value,code,inst.stable_key,status,"v1",utc_iso(self.clock()),stable_hash(payload),encoded,17))
    def _write_position(self, db, row, plan):
        code=str(self._value(row,"code","symbol","ticker",default="")).strip().upper()
        market=Market.A if code.isdigit() and len(code)==6 else Market.US
        try:
            instrument=InstrumentId.from_code(code,market)
            shares=self._dec(self._value(row,"shares","quantity","qty",default=0)); cost=self._dec(self._value(row,"cost_price","cost",default=0))
            if shares<=0 or cost<0: raise MigrationExecutionError("invalid position survived migration planning")
            snapshot=db.execute("SELECT id FROM account_snapshots WHERE market=? AND captured_at=?",(market.value,utc_iso(plan.created_at))).fetchone()
            if snapshot is None:
                db.execute("INSERT OR IGNORE INTO account_snapshots(market,currency,cash,captured_at,schema_version) VALUES(?,?,?,?,?)",(market.value,"CNY" if market is Market.A else "USD","0",utc_iso(plan.created_at),17))
                snapshot=db.execute("SELECT id FROM account_snapshots WHERE market=? AND captured_at=?",(market.value,utc_iso(plan.created_at))).fetchone()
            db.execute("INSERT OR IGNORE INTO account_positions(account_snapshot_id,instrument_key,code,market,exchange,shares,cost_price,captured_at) VALUES(?,?,?,?,?,?,?,?)",(snapshot["id"],instrument.stable_key,instrument.code,instrument.market.value,instrument.exchange.value,str(shares),str(cost),utc_iso(plan.created_at)))
        except MigrationExecutionError:
            raise
        except Exception as exc:
            raise MigrationExecutionError(f"failed to migrate position {code or '<empty>'}: {exc}") from exc
    def _write_report_archive(self, db,row,plan,run):
        aid=stable_hash({"run":run.run_id,"source":str(self._value(row,"id","report_id",default="")),"kind":"report"}); payload=dict(row); encoded=json.dumps(payload,ensure_ascii=False,default=str,sort_keys=True,separators=(",",":"))
        db.execute("INSERT OR IGNORE INTO legacy_report_archives(archive_id,event_key,run_id,source_fingerprint,source_id,market,code,title,content,path,rating,created_at,payload_hash,payload_json,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(aid,aid,run.run_id,plan.source_fingerprint,str(self._value(row,"id","report_id",default=aid)),self._value(row,"market"),self._value(row,"code"),self._value(row,"title"),self._value(row,"content","body"),self._value(row,"path"),self._value(row,"rating"),utc_iso(self.clock()),stable_hash(payload),encoded,17))
    def _write_evidence_archive(self, db,row,plan,run,item):
        aid=stable_hash({"run":run.run_id,"table":item.source_table,"key":item.source_key}); payload=dict(row); encoded=json.dumps(payload,ensure_ascii=False,default=str,sort_keys=True,separators=(",",":"))
        db.execute("INSERT OR IGNORE INTO legacy_evidence_archives(archive_id,event_key,run_id,source_table,source_id,market,code,evidence_kind,reason_codes_json,payload_hash,payload_json,created_at,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(aid,aid,run.run_id,item.source_table,item.source_key,self._value(row,"market"),self._value(row,"code","symbol"),item.target_kind,json.dumps(item.reason_codes),stable_hash(payload),encoded,utc_iso(self.clock()),17))
    @staticmethod
    def _dec(value):
        try: return Decimal(str(value or 0))
        except (InvalidOperation, ValueError): return Decimal("0")
