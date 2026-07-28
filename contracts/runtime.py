"""V2-12 运行时合同：命令身份、运行结果、健康检查和报告修订链接。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .enums import DecisionMode, Market
from .market_data import ContractViolation, InstrumentId, ensure_utc, stable_hash

class _StringEnum(str, Enum):
    def __str__(self): return self.value

class AnalysisRunStatus(_StringEnum):
    QUEUED="queued"; RUNNING="running"; DETERMINISTIC_COMPLETED="deterministic_completed"; BACKGROUND_PENDING="background_pending"; COMPLETED="completed"; CANCELLED="cancelled"; FAILED="failed"
class RevisionKind(_StringEnum):
    RESEARCH_ENRICHED="research_enriched"
class MigrationRunStatus(_StringEnum):
    PLANNED="planned"; AWAITING_CONFIRMATION="awaiting_confirmation"; RUNNING="running"; COMPLETED="completed"; COMPLETED_WITH_QUARANTINE="completed_with_quarantine"; FAILED="failed"; CANCELLED="cancelled"
class MigrationItemStatus(_StringEnum):
    MIGRATED="migrated"; ARCHIVED_ONLY="archived_only"; SKIPPED_DUPLICATE="skipped_duplicate"; QUARANTINED="quarantined"; REJECTED="rejected"

MIGRATION_REASON_CODES=frozenset("""
MIGRATION_SOURCE_MISSING MIGRATION_SCHEMA_UNKNOWN MIGRATION_SOURCE_CHANGED_AFTER_PREFLIGHT
MIGRATION_INVALID_MARKET MIGRATION_INVALID_INSTRUMENT MIGRATION_EXCHANGE_UNRESOLVED
MIGRATION_INVALID_SHARES MIGRATION_INVALID_COST MIGRATION_INVALID_CASH
MIGRATION_HELD_REMOVED_FROM_WATCHLIST MIGRATION_TARGET_ALREADY_NEWER
MIGRATION_LEGACY_EVIDENCE_UNTRUSTED MIGRATION_PRE_LISTING_BAR_REJECTED
MIGRATION_REALTIME_BAR_REJECTED MIGRATION_TRANSACTION_ROLLED_BACK
""".split())

def _enum(kind,value,name):
    try:return value if isinstance(value,kind) else kind(str(value))
    except ValueError as exc:raise ContractViolation(f"invalid {name}: {value}") from exc

@dataclass(frozen=True,slots=True)
class SingleStockAnalysisCommand:
    command_id:str; instrument:InstrumentId; mode:DecisionMode; history_period:str; requested_at:datetime; account_snapshot_id:str|None=None; force_refresh:bool=False
    def __post_init__(self):
        mode=_enum(DecisionMode,self.mode,"analysis mode"); requested=ensure_utc(self.requested_at,"requested_at")
        if not self.history_period:raise ContractViolation("history period is required")
        expected=stable_hash({"instrument":self.instrument,"mode":mode,"history":self.history_period,"requested_at":requested,"account":self.account_snapshot_id,"force_refresh":self.force_refresh})
        if self.command_id!=expected:raise ContractViolation("single stock command identity mismatch")
        object.__setattr__(self,"mode",mode);object.__setattr__(self,"requested_at",requested)

@dataclass(frozen=True,slots=True)
class PortfolioAnalysisCommand:
    command_id:str; market:Market; mode:DecisionMode; history_period:str; requested_at:datetime; account_snapshot_id:str|None; watchlist_snapshot_id:str|None; force_refresh:bool=False
    def __post_init__(self):
        market=_enum(Market,self.market,"portfolio market");mode=_enum(DecisionMode,self.mode,"analysis mode");requested=ensure_utc(self.requested_at,"requested_at")
        if not self.history_period:raise ContractViolation("history period is required")
        expected=stable_hash({"market":market,"mode":mode,"history":self.history_period,"requested_at":requested,"account":self.account_snapshot_id,"watchlist":self.watchlist_snapshot_id,"force_refresh":self.force_refresh})
        if self.command_id!=expected:raise ContractViolation("portfolio command identity mismatch")
        object.__setattr__(self,"market",market);object.__setattr__(self,"mode",mode);object.__setattr__(self,"requested_at",requested)

@dataclass(frozen=True,slots=True)
class AnalysisRunResult:
    run_id:str; command_id:str; status:AnalysisRunStatus; deterministic_report_id:str|None; research_report_id:str|None; background_task_ids:tuple[str,...]; source_artifact_refs:tuple[str,...]; reason_codes:tuple[str,...]; started_at:datetime; completed_at:datetime|None
    def __post_init__(self):
        status=_enum(AnalysisRunStatus,self.status,"run status");started=ensure_utc(self.started_at,"run started_at");completed=None if self.completed_at is None else ensure_utc(self.completed_at,"run completed_at")
        if completed and completed<started:raise ContractViolation("run completed before start")
        if status in {AnalysisRunStatus.DETERMINISTIC_COMPLETED,AnalysisRunStatus.COMPLETED,AnalysisRunStatus.BACKGROUND_PENDING} and not self.deterministic_report_id:raise ContractViolation("completed run needs deterministic report")
        if status is AnalysisRunStatus.COMPLETED and completed is None:raise ContractViolation("completed run needs completed_at")
        object.__setattr__(self,"status",status);object.__setattr__(self,"started_at",started);object.__setattr__(self,"completed_at",completed);object.__setattr__(self,"background_task_ids",tuple(sorted(set(self.background_task_ids))));object.__setattr__(self,"source_artifact_refs",tuple(sorted(set(self.source_artifact_refs))));object.__setattr__(self,"reason_codes",tuple(sorted(set(self.reason_codes))))

@dataclass(frozen=True,slots=True)
class RuntimeHealth:
    app_version:str; schema_version:int; settings_status:str; database_status:str; calendar_status:str; finbert_status:str; provider_capabilities:tuple[str,...]; migration_status:str; checked_at:datetime
    def __post_init__(self):
        checked=ensure_utc(self.checked_at,"health checked_at")
        if not self.app_version or self.schema_version<1:raise ContractViolation("invalid runtime health")
        object.__setattr__(self,"checked_at",checked);object.__setattr__(self,"provider_capabilities",tuple(sorted(set(self.provider_capabilities))))

@dataclass(frozen=True,slots=True)
class ReportRevisionLink:
    link_id:str; base_report_id:str; revised_report_id:str; revision_kind:RevisionKind; invariant_section_hash:str; created_at:datetime
    def __post_init__(self):
        kind=_enum(RevisionKind,self.revision_kind,"revision kind");created=ensure_utc(self.created_at,"revision created_at")
        if not self.base_report_id or not self.revised_report_id or self.base_report_id==self.revised_report_id or len(self.invariant_section_hash)!=64:raise ContractViolation("invalid report revision link")
        expected=stable_hash({"base":self.base_report_id,"revised":self.revised_report_id,"kind":kind,"invariant":self.invariant_section_hash})
        if self.link_id!=expected:raise ContractViolation("revision link identity mismatch")
        object.__setattr__(self,"revision_kind",kind);object.__setattr__(self,"created_at",created)


def report_revision_invariant(document) -> str:
    """Hash every deterministic report field while excluding research sections."""
    sections=tuple(item for item in document.sections if not item.section_id.startswith("research"))
    refs=tuple(sorted({
        ref
        for section in sections
        for block in section.blocks
        for ref in block.source_artifact_refs
    } | {
        ref
        for section in sections
        for block in section.blocks
        if getattr(block.kind,"value",block.kind)=="table"
        for row in block.payload.rows
        for ref in row.source_artifact_refs
    }))
    return stable_hash({
        "kind":document.report_kind,"market":document.market,"instrument":document.instrument,
        "mode":document.analysis_mode,"as_of":document.as_of,"title":document.title,
        "subtitle":document.subtitle,"summary":document.summary,"sections":sections,
        "glossary":document.glossary_entries,"refs":refs,"schema":document.schema_version,
        "renderer":document.renderer_version,
    })
