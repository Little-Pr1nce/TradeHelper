"""生成可确认的迁移计划；计划只读，不改变任何数据库。"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Mapping

from contracts.migration import MigrationItem, MigrationPlan
from contracts.runtime import MigrationItemStatus
from contracts.market_data import stable_hash
from contracts.market_data import InstrumentId
from contracts.enums import Market
from .legacy_reader import LegacyReader

class MigrationPlanner:
    VERSION = 17
    def __init__(self, reader: LegacyReader, *, clock=None):
        self.reader = reader; self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _item(self, run_id: str, table: str, key: str, target: str, status: MigrationItemStatus, payload: Mapping[str, Any], reasons=()):
        return MigrationItem(stable_hash({"run":run_id,"table":table,"key":key,"target":target,"status":status,"reasons":tuple(sorted(reasons)),"payload":tuple(sorted(payload.items()))}), run_id, table, key, target, status, tuple(reasons), tuple(sorted(payload.items())), self.clock())

    def build(self) -> MigrationPlan:
        preflight = self.reader.preflight(self.clock()); fp = self.reader.source.fingerprint(); run_id = stable_hash({"source":fp,"version":self.VERSION})
        source_info=self.reader.source.stat_fingerprint()
        created_at=(
            datetime.fromtimestamp(source_info["mtime_ns"] / 1_000_000_000, tz=timezone.utc)
            if source_info.get("mtime_ns") is not None else datetime(1970,1,1,tzinfo=timezone.utc)
        )
        original_clock=self.clock
        self.clock=lambda:created_at
        items = []
        if not preflight.source_exists:
            items.append(self._item(run_id, "__source__", "missing", "none", MigrationItemStatus.QUARANTINED, {"path":str(self.reader.source.path)}, ("MIGRATION_SOURCE_MISSING",)))
        else:
            for row in self.reader.rows("account_balance"):
                key = str(row.get("id", row.get("date", row.get("created_at", len(items)))))
                try:
                    cash_values=[float(row.get(name,0) or 0) for name in ("a_balance","us_balance","cash_a","cash_us","cash") if name in row]
                    valid_cash=all(value>=0 for value in cash_values) and all(value==value and value not in (float("inf"),float("-inf")) for value in cash_values)
                except (TypeError,ValueError): valid_cash=False
                items.append(self._item(run_id,"account_balance",key,"account_snapshot",MigrationItemStatus.MIGRATED if valid_cash else MigrationItemStatus.QUARANTINED,row,() if valid_cash else ("MIGRATION_INVALID_CASH",)))
            held = set()
            for index,row in enumerate(self.reader.rows("holdings")):
                code = str(row.get("code", "")).strip().upper()
                key = str(row.get("id") or code or f"row-{index}")
                raw_shares = row.get("shares", row.get("quantity", row.get("qty", 0))); raw_cost = row.get("cost_price", row.get("cost", 0))
                try:
                    market=Market.A if code.isdigit() and len(code)==6 else Market.US
                    InstrumentId.from_code(code,market)
                    valid = bool(code) and float(raw_shares) > 0 and float(raw_cost) >= 0
                except (TypeError, ValueError): valid = False
                status = MigrationItemStatus.MIGRATED if valid else MigrationItemStatus.QUARANTINED
                reasons = () if valid else ("MIGRATION_INVALID_INSTRUMENT", "MIGRATION_INVALID_SHARES")
                if valid: held.add(code)
                items.append(self._item(run_id,"holdings",key,"account_snapshot_position",status,row,reasons))
            for index, row in enumerate(self.reader.rows("watchlist")):
                code = str(row.get("code", row.get("symbol", ""))).strip().upper(); key = str(row.get("id") or code or f"row-{index}")
                if code in held:
                    items.append(self._item(run_id,"watchlist",key,"watchlist",MigrationItemStatus.QUARANTINED,row,("MIGRATION_HELD_REMOVED_FROM_WATCHLIST",)))
                else:
                    try:
                        market=Market.A if code.isdigit() and len(code)==6 else Market.US
                        InstrumentId.from_code(code,market)
                    except (TypeError,ValueError):
                        items.append(self._item(run_id,"watchlist",key,"watchlist",MigrationItemStatus.QUARANTINED,row,("MIGRATION_INVALID_INSTRUMENT",)))
                    else:
                        items.append(self._item(run_id,"watchlist",key,"watchlist",MigrationItemStatus.MIGRATED,row))
            for table in self.reader.METADATA_TABLES:
                for index, row in enumerate(self.reader.rows(table)):
                    items.append(self._item(run_id,table,str(row.get("id",row.get("code",index))),"stock_metadata_clue",MigrationItemStatus.MIGRATED,row))
            for table in self.reader.REPORT_TABLES:
                for index, row in enumerate(self.reader.rows(table)):
                    items.append(self._item(run_id,table,str(row.get("id",index)),"legacy_report_archive",MigrationItemStatus.ARCHIVED_ONLY,row))
            listing_dates={str(row.get("code","")).strip().upper():str(row.get("listing_date"))[:10] for row in self.reader.rows("stocks") if row.get("listing_date")}
            for table in self.reader.EVIDENCE_TABLES:
                for index, row in enumerate(self.reader.rows(table)):
                    code=str(row.get("code",row.get("symbol",""))).strip().upper(); raw_date=str(row.get("date",row.get("trading_date","")))[:10]
                    reasons=("MIGRATION_PRE_LISTING_BAR_REJECTED",) if table in {"price_history","intraday_price_history"} and code in listing_dates and raw_date and raw_date < listing_dates[code] else ("MIGRATION_LEGACY_EVIDENCE_UNTRUSTED",)
                    items.append(self._item(run_id,table,str(row.get("id",index)),"legacy_evidence_archive",MigrationItemStatus.ARCHIVED_ONLY,row,reasons))
        preflight_hash=stable_hash({"source_path":preflight.source_path,"source_exists":preflight.source_exists,"schema":preflight.source_schema_detected,"counts":preflight.table_counts,"migratable":preflight.migratable_counts,"conflicts":preflight.conflict_counts,"warnings":preflight.warnings,"read_only":preflight.read_only})
        plan=MigrationPlan(stable_hash({"source":str(self.reader.source.path),"fingerprint":fp,"version":self.VERSION,"preflight":preflight_hash,"items":tuple(sorted(items,key=lambda x:x.item_id))}), str(self.reader.source.path), fp, self.VERSION, preflight_hash, tuple(items), created_at)
        self.clock=original_clock
        return plan
