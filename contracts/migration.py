"""V1 -> V2 迁移计划合同；迁移状态和项目不可变、可审计。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from .market_data import ContractViolation, ensure_utc, stable_hash
from .runtime import MigrationItemStatus, MigrationRunStatus, MIGRATION_REASON_CODES

@dataclass(frozen=True,slots=True)
class MigrationItem:
    item_id:str; run_id:str; source_table:str; source_key:str; target_kind:str; status:MigrationItemStatus; reason_codes:tuple[str,...]; payload:tuple[tuple[str,object],...]; created_at:datetime
    def __post_init__(self):
        created=ensure_utc(self.created_at,"migration item created_at");status=self.status if isinstance(self.status,MigrationItemStatus) else MigrationItemStatus(str(self.status))
        if not self.item_id or not self.run_id or not self.source_table or not self.source_key or not self.target_kind:raise ContractViolation("invalid migration item")
        unknown=set(self.reason_codes)-set(MIGRATION_REASON_CODES)
        if unknown: raise ContractViolation(f"unknown migration reason codes: {sorted(unknown)}")
        expected=stable_hash({"run":self.run_id,"table":self.source_table,"key":self.source_key,"target":self.target_kind,"status":status,"reasons":tuple(sorted(set(self.reason_codes))),"payload":tuple(self.payload)})
        if self.item_id!=expected:raise ContractViolation("migration item identity mismatch")
        object.__setattr__(self,"status",status);object.__setattr__(self,"reason_codes",tuple(sorted(set(self.reason_codes))));object.__setattr__(self,"payload",tuple(sorted(self.payload)));object.__setattr__(self,"created_at",created)

@dataclass(frozen=True,slots=True)
class MigrationPlan:
    plan_id:str; source_path:str; source_fingerprint:str; migration_version:int; preflight_hash:str; items:tuple[MigrationItem,...]; created_at:datetime
    def __post_init__(self):
        created=ensure_utc(self.created_at,"migration plan created_at");items=tuple(sorted(self.items,key=lambda x:x.item_id))
        if len(self.source_fingerprint)!=64 or self.migration_version<1:raise ContractViolation("invalid migration plan")
        expected=stable_hash({"source":self.source_path,"fingerprint":self.source_fingerprint,"version":self.migration_version,"preflight":self.preflight_hash,"items":items})
        if self.plan_id!=expected:raise ContractViolation("migration plan identity mismatch")
        object.__setattr__(self,"items",items);object.__setattr__(self,"created_at",created)

@dataclass(frozen=True,slots=True)
class MigrationRun:
    run_id:str; plan_id:str; source_fingerprint:str; migration_version:int; status:MigrationRunStatus; backup_path:str|None; started_at:datetime; completed_at:datetime|None; reason_codes:tuple[str,...]
    def __post_init__(self):
        status=self.status if isinstance(self.status,MigrationRunStatus) else MigrationRunStatus(str(self.status));started=ensure_utc(self.started_at,"migration started_at");completed=None if self.completed_at is None else ensure_utc(self.completed_at,"migration completed_at")
        if completed and completed<started:raise ContractViolation("migration completed before start")
        expected=stable_hash({"plan":self.plan_id,"source":self.source_fingerprint,"version":self.migration_version})
        if self.run_id!=expected:raise ContractViolation("migration run identity mismatch")
        object.__setattr__(self,"status",status);object.__setattr__(self,"started_at",started);object.__setattr__(self,"completed_at",completed);object.__setattr__(self,"reason_codes",tuple(sorted(set(self.reason_codes))))
