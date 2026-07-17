"""V2-11 展示层不可变合同。

展示层只携带冻结后的业务 artifact 与可渲染读模型；它从不产生预测、策略、
风控或订单结论。所有 ID 都通过 stable_hash 建立可审计身份。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
import re

from .account import AccountSnapshot
from .analysis import FeatureSnapshot
from .enums import DecisionMode, Market
from .execution import OrderIntentBundle
from .forecast import ForecastResult
from .learning import ForecastOutcome, JointOutcome, LearningMetricSnapshot, StrategyOutcome
from .market_data import (
    ContractViolation,
    FundamentalSnapshot,
    InstrumentId,
    NewsSnapshot,
    QuoteSnapshot,
    StockMetadata,
    ensure_utc,
    stable_hash,
)
from .portfolio import PortfolioDecisionBundle
from .quality import DataQualityReport
from .research import HypothesisOutcome, HypothesisValidation, ResearchHypothesis, ResearchMetricSnapshot
from .risk import FrozenAccountValuation, PlanEvidenceSnapshot, RiskDecisionBundle
from .scenario import TradingScenario
from .strategy import StrategyBundle


class _StringEnum(str, Enum):
    def __str__(self) -> str: return self.value


class ReportKind(_StringEnum): SINGLE_STOCK="single_stock"; PORTFOLIO="portfolio"
class ReportBlockKind(_StringEnum): TEXT="text"; CALLOUT="callout"; METRIC="metric"; TABLE="table"; CHART="chart"; DIVIDER="divider"
class ChartKind(_StringEnum): CALIBRATION="calibration"; FORECAST_TIMELINE="forecast_timeline"; CUMULATIVE_PERFORMANCE="cumulative_performance"; DRAWDOWN="drawdown"
class ExportFormat(_StringEnum): MARKDOWN="markdown"; HTML="html"; PDF="pdf"
class ExportStatus(_StringEnum): PENDING="pending"; COMPLETED="completed"; FAILED="failed"
class ReportSeverity(_StringEnum): INFO="info"; POSITIVE="positive"; WARNING="warning"; DANGER="danger"; UNAVAILABLE="unavailable"
class AnalysisStage(_StringEnum): VALIDATE_INPUT="validate_input"; RESOLVE_SUBJECT="resolve_subject"; REFRESH_METADATA="refresh_metadata"; REFRESH_MARKET_DATA="refresh_market_data"; BUILD_FEATURES="build_features"; FORECAST="forecast"; SCENARIO="scenario"; STRATEGY="strategy"; RISK="risk"; EXECUTION_PREVIEW="execution_preview"; PORTFOLIO_ALLOCATION="portfolio_allocation"; RESEARCH="research"; LEARNING_UPDATE="learning_update"; BUILD_REPORT="build_report"; PERSIST_REPORT="persist_report"; COMPLETED="completed"
class TaskStatus(_StringEnum): QUEUED="queued"; RUNNING="running"; WAITING="waiting"; CANCELLING="cancelling"; CANCELLED="cancelled"; COMPLETED="completed"; FAILED="failed"
class LedgerViewKind(_StringEnum): FORECAST="forecast"; STRATEGY="strategy"; JOINT="joint"; RESEARCH="research"


PRESENTATION_REASON_CODES=frozenset("""
PRESENTATION_IDENTITY_MISMATCH PRESENTATION_SOURCE_MISSING PRESENTATION_TARGET_DATE_INVALID
PRESENTATION_CURRENCY_MISMATCH REPORT_MODEL_SAMPLE_INSUFFICIENT REPORT_MODEL_UNDERPERFORMED_BASELINE
REPORT_MODEL_DATA_QUALITY_BLOCKED REPORT_MODEL_DRIFTED REPORT_MODEL_CONFIRMATION_PENDING
REPORT_TAKE_PROFIT_UNAVAILABLE REPORT_HISTORY_SAMPLE_INSUFFICIENT REPORT_HISTORY_UNAVAILABLE
REPORT_RESEARCH_UNAVAILABLE REPORT_DATA_FIELD_MISSING REPORT_PORTFOLIO_VALUATION_INCOMPLETE
REPORT_EXPORT_FAILED REPORT_ARCHIVED TASK_RATE_LIMIT_WAITING TASK_CANCELLED TASK_STAGE_FAILED
SETTINGS_CAPABILITY_UNAVAILABLE
""".split())
PRESENTATION_POLICY_REF = "external:tradehelper_v2_presentation_policy_v1"


def _enum(kind, value, name):
    try: return value if isinstance(value, kind) else kind(str(value))
    except ValueError as exc: raise ContractViolation(f"unsupported {name}: {value}") from exc


def _refs(values):
    result=tuple(sorted(set(str(item) for item in values if item)))
    if not result: raise ContractViolation("presentation source references are required")
    # 展示合同会被导出和长期保存；来源标识只能是业务 artifact ID 或显式
    # external display fact，不能把配置、工作目录或 secret 带入报告。
    forbidden=("api_key", "token", "password", "secret", "/users/", "\\\\", ".sqlite")
    if any(any(word in item.lower() for word in forbidden) or not re.fullmatch(r"[A-Za-z0-9:._/@+=-]+",item) for item in result):
        raise ContractViolation("unsafe presentation source reference")
    return result


def _source_ref(prefix: str, value: object, *identity_names: str) -> str:
    for name in identity_names:
        identity = getattr(value, name, None)
        if identity:
            raw = str(identity)
            return f"{prefix}:{raw}" if re.fullmatch(r"[A-Za-z0-9._/@+=-]+", raw) else f"{prefix}:{stable_hash(raw)}"
    return f"{prefix}:{stable_hash(value)}"


def presentation_source_refs(*values: object) -> tuple[str, ...]:
    """Return the exact auditable source set for frozen presentation artifacts."""
    prefixes = {
        StockMetadata: ("metadata", ()),
        QuoteSnapshot: ("quote", ()),
        DataQualityReport: ("quality", ()),
        FeatureSnapshot: ("feature", ("feature_hash",)),
        ForecastResult: ("forecast", ("event_key",)),
        TradingScenario: ("scenario", ("scenario_id",)),
        StrategyBundle: ("strategy", ("bundle_id",)),
        RiskDecisionBundle: ("risk", ("risk_bundle_id",)),
        OrderIntentBundle: ("order", ("intent_bundle_id",)),
        PlanEvidenceSnapshot: ("plan_evidence", ("evidence_id",)),
        ForecastOutcome: ("forecast_outcome", ("forecast_outcome_id",)),
        StrategyOutcome: ("strategy_outcome", ("strategy_outcome_id",)),
        JointOutcome: ("joint_outcome", ("joint_outcome_id",)),
        LearningMetricSnapshot: ("learning_metric", ("snapshot_id",)),
        ResearchHypothesis: ("research_hypothesis", ("hypothesis_id",)),
        HypothesisValidation: ("research_validation", ("validation_id",)),
        HypothesisOutcome: ("research_outcome", ("outcome_id",)),
        ResearchMetricSnapshot: ("research_metric", ("snapshot_id",)),
        NewsSnapshot: ("news", ("stable_key",)),
        FundamentalSnapshot: ("fundamental", ()),
        AccountSnapshot: ("account", ()),
        FrozenAccountValuation: ("valuation", ("valuation_id",)),
        PortfolioDecisionBundle: ("portfolio", ("portfolio_bundle_id",)),
        WatchlistSnapshot: ("watchlist", ("watchlist_id",)),
    }
    result: list[str] = []
    for raw in values:
        nested = raw if isinstance(raw, (tuple, list)) else (raw,)
        for value in nested:
            if value is None:
                continue
            for kind, (prefix, names) in prefixes.items():
                if isinstance(value, kind):
                    result.append(_source_ref(prefix, value, *names))
                    break
            else:
                raise ContractViolation(f"unsupported presentation source artifact: {type(value).__name__}")
    return _refs(result)


@dataclass(frozen=True, slots=True)
class ReportTableRow:
    row_id:str; cells:tuple[str,...]; severity:ReportSeverity|None; source_artifact_refs:tuple[str,...]
    def __post_init__(self):
        if not self.row_id or not self.cells: raise ContractViolation("report table row is incomplete")
        object.__setattr__(self,"severity",None if self.severity is None else _enum(ReportSeverity,self.severity,"row severity")); object.__setattr__(self,"cells",tuple(str(item) for item in self.cells)); object.__setattr__(self,"source_artifact_refs",_refs(self.source_artifact_refs))


@dataclass(frozen=True, slots=True)
class ReportTable:
    table_id:str; title:str; columns:tuple[str,...]; rows:tuple[ReportTableRow,...]; empty_state:str|None=None; interpretation:str|None=None
    def __post_init__(self):
        rows=tuple(self.rows)
        if not self.table_id or not self.title or not self.columns or len(set(self.columns))!=len(self.columns) or any(len(row.cells)!=len(self.columns) for row in rows): raise ContractViolation("invalid report table")
        object.__setattr__(self,"columns",tuple(str(item) for item in self.columns)); object.__setattr__(self,"rows",rows)


@dataclass(frozen=True, slots=True)
class ChartSpec:
    chart_id:str; chart_kind:ChartKind; title:str; x_axis:str; y_axis:str; series:tuple[tuple[str,tuple[tuple[str,float],...]],...]; baseline:tuple[tuple[str,float],...]; sample_count:int; sample_range:tuple[str,str]|None; interpretation:str; empty_state:str|None=None
    def __post_init__(self):
        kind=_enum(ChartKind,self.chart_kind,"chart kind"); series=tuple(sorted(self.series,key=lambda item:item[0])); baseline=tuple(self.baseline)
        if not self.chart_id or not self.title or not self.x_axis or not self.y_axis or not self.interpretation or self.sample_count<0 or len({name for name,_ in series})!=len(series): raise ContractViolation("invalid chart")
        if any(not name or any(not isinstance(x,str) or not isinstance(y,(int,float)) or not isfinite(y) for x,y in points) for name,points in series) or any(not isinstance(x,str) or not isinstance(y,(int,float)) or not isfinite(y) for x,y in baseline): raise ContractViolation("invalid chart points")
        object.__setattr__(self,"chart_kind",kind); object.__setattr__(self,"series",series); object.__setattr__(self,"baseline",baseline)
    @property
    def content_hash(self): return stable_hash({"kind":self.chart_kind,"title":self.title,"x":self.x_axis,"y":self.y_axis,"series":self.series,"baseline":self.baseline,"samples":self.sample_count,"range":self.sample_range,"interpretation":self.interpretation,"empty":self.empty_state})


@dataclass(frozen=True, slots=True)
class ReportBlock:
    kind: ReportBlockKind
    payload: object
    source_artifact_refs: tuple[str, ...]

    def __post_init__(self):
        kind=_enum(ReportBlockKind,self.kind,"report block kind")
        if kind is ReportBlockKind.TABLE and not isinstance(self.payload,ReportTable): raise ContractViolation("table block needs ReportTable")
        if kind is ReportBlockKind.CHART and not isinstance(self.payload,ChartSpec): raise ContractViolation("chart block needs ChartSpec")
        if kind not in {ReportBlockKind.TABLE, ReportBlockKind.CHART} and not isinstance(self.payload, (str, int, float)):
            raise ContractViolation("text and metric blocks require scalar display payloads")
        refs = _refs(self.source_artifact_refs)
        if any(word in str(self.payload).lower() for word in ("api_key", "password=", "bearer ", ".sqlite", "/users/")):
            raise ContractViolation("report block contains private configuration")
        object.__setattr__(self,"kind",kind)
        object.__setattr__(self,"source_artifact_refs",refs)


@dataclass(frozen=True, slots=True)
class ReportSection:
    section_id:str; title:str; purpose:str; severity:ReportSeverity|None; blocks:tuple[ReportBlock,...]
    def __post_init__(self):
        if not self.section_id or not self.title or not self.purpose or not self.blocks: raise ContractViolation("invalid report section")
        object.__setattr__(self,"severity",None if self.severity is None else _enum(ReportSeverity,self.severity,"section severity")); object.__setattr__(self,"blocks",tuple(self.blocks))


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_key:str; display_name:str; plain_language_definition:str; preferred_direction:str; minimum_sample_guidance:str; unit:str|None=None
    def __post_init__(self):
        if not all((self.metric_key,self.display_name,self.plain_language_definition,self.preferred_direction,self.minimum_sample_guidance)): raise ContractViolation("invalid metric definition")


@dataclass(frozen=True, slots=True)
class SingleStockPresentationInput:
    presentation_id: str
    instrument: InstrumentId
    analysis_mode: DecisionMode
    as_of: datetime
    history_period: str
    metadata: StockMetadata
    quote_snapshot: QuoteSnapshot | None
    data_quality: DataQualityReport
    feature_snapshot: FeatureSnapshot
    forecasts: tuple[ForecastResult, ...]
    scenario: TradingScenario
    strategy_bundle: StrategyBundle
    risk_bundle: RiskDecisionBundle
    order_intent_bundle: OrderIntentBundle
    learning_evidence: tuple[PlanEvidenceSnapshot, ...]
    forecast_outcomes: tuple[ForecastOutcome, ...]
    strategy_outcomes: tuple[StrategyOutcome, ...]
    joint_outcomes: tuple[JointOutcome, ...]
    metric_snapshots: tuple[LearningMetricSnapshot, ...]
    research_hypotheses: tuple[ResearchHypothesis, ...]
    research_validations: tuple[HypothesisValidation, ...]
    research_outcomes: tuple[HypothesisOutcome, ...]
    research_metric_snapshots: tuple[ResearchMetricSnapshot, ...]
    news_summary: tuple[NewsSnapshot, ...]
    fundamental_summary: FundamentalSnapshot | None
    source_artifact_refs: tuple[str, ...]
    built_at: datetime

    def __post_init__(self):
        mode = _enum(DecisionMode, self.analysis_mode, "analysis mode")
        as_of = ensure_utc(self.as_of, "presentation as_of")
        built = ensure_utc(self.built_at, "presentation built_at")
        forecasts = tuple(sorted(self.forecasts, key=lambda item: item.horizon))
        sequences = (
            self.learning_evidence, self.forecast_outcomes, self.strategy_outcomes,
            self.joint_outcomes, self.metric_snapshots, self.research_hypotheses,
            self.research_validations, self.research_outcomes,
            self.research_metric_snapshots, self.news_summary,
        )
        all_sources = (
            self.metadata, self.quote_snapshot, self.data_quality, self.feature_snapshot,
            forecasts, self.scenario, self.strategy_bundle, self.risk_bundle,
            self.order_intent_bundle, *sequences, self.fundamental_summary,
        )
        refs = _refs(self.source_artifact_refs)
        required_refs = tuple(sorted((*presentation_source_refs(*all_sources), PRESENTATION_POLICY_REF)))
        if refs != required_refs:
            raise ContractViolation("presentation source references do not close over frozen artifacts")
        if not self.history_period or built < as_of or tuple(item.horizon for item in forecasts) != (1, 3, 5, 10):
            raise ContractViolation("invalid single-stock presentation input")
        instrument_items = (
            self.metadata, self.quote_snapshot, self.feature_snapshot, *forecasts,
            self.scenario, self.strategy_bundle, self.risk_bundle,
            *self.learning_evidence, *self.forecast_outcomes, *self.strategy_outcomes,
            *self.joint_outcomes, *self.research_hypotheses, *self.research_validations,
            *self.research_outcomes, *self.news_summary, self.fundamental_summary,
        )
        if any(getattr(item, "instrument", self.instrument) not in {None, self.instrument} for item in instrument_items if item is not None):
            raise ContractViolation("presentation artifact instrument mismatch")
        if self.feature_snapshot.mode is not mode or self.feature_snapshot.cutoff_at != as_of:
            raise ContractViolation("feature snapshot does not match presentation cutoff")
        if any(item.cutoff_at > as_of or item.generated_at > built for item in forecasts):
            raise ContractViolation("forecast is newer than the presentation cutoff")
        if (self.metadata.fetched_at > built or self.data_quality.evaluated_at > built or
                (self.quote_snapshot is not None and (
                    self.quote_snapshot.observed_at > as_of or self.quote_snapshot.fetched_at > built
                )) or
                any(item.available_at > as_of or item.fetched_at > built for item in self.news_summary) or
                (self.fundamental_summary is not None and (
                    self.fundamental_summary.available_at > as_of or
                    self.fundamental_summary.fetched_at > built or
                    any(field.published_at is not None and field.published_at > as_of
                        for field in self.fundamental_summary.fields.values())
                ))):
            raise ContractViolation("presentation contains facts unavailable at the analysis cutoff")
        assessment_keys = {item.horizon: item.forecast_event_key for item in self.scenario.horizon_assessments}
        if (self.scenario.instrument != self.instrument or self.scenario.mode is not mode or self.scenario.as_of != as_of or
                self.scenario.current_feature_hash != self.feature_snapshot.feature_hash or
                assessment_keys != {item.horizon: item.event_key for item in forecasts}):
            raise ContractViolation("scenario does not close over forecast and feature artifacts")
        if self.strategy_bundle.instrument != self.instrument or self.strategy_bundle.scenario_id != self.scenario.scenario_id:
            raise ContractViolation("strategy bundle does not close over scenario")
        if (self.risk_bundle.instrument != self.instrument or self.risk_bundle.scenario_id != self.scenario.scenario_id or
                self.risk_bundle.strategy_bundle_id != self.strategy_bundle.bundle_id):
            raise ContractViolation("risk bundle does not close over strategy")
        if self.order_intent_bundle.risk_bundle_id != self.risk_bundle.risk_bundle_id:
            raise ContractViolation("order intent bundle does not close over risk")
        generated = tuple(
            item.generated_at for item in (
                self.feature_snapshot, *forecasts, self.scenario, self.strategy_bundle,
                self.risk_bundle, self.order_intent_bundle, *self.learning_evidence,
                *self.forecast_outcomes, *self.strategy_outcomes, *self.joint_outcomes,
                *self.metric_snapshots, *self.research_hypotheses,
                *self.research_validations, *self.research_outcomes,
                *self.research_metric_snapshots,
            )
        )
        if any(item > built for item in generated):
            raise ContractViolation("presentation contains an artifact generated after built_at")
        identity = {
            "instrument": self.instrument, "mode": mode, "as_of": as_of,
            "history": self.history_period, "metadata": self.metadata,
            "quote": self.quote_snapshot, "quality": self.data_quality,
            "feature": self.feature_snapshot, "forecasts": forecasts,
            "scenario": self.scenario, "strategy": self.strategy_bundle,
            "risk": self.risk_bundle, "orders": self.order_intent_bundle,
            "learning_evidence": self.learning_evidence,
            "forecast_outcomes": self.forecast_outcomes,
            "strategy_outcomes": self.strategy_outcomes,
            "joint_outcomes": self.joint_outcomes,
            "metric_snapshots": self.metric_snapshots,
            "research_hypotheses": self.research_hypotheses,
            "research_validations": self.research_validations,
            "research_outcomes": self.research_outcomes,
            "research_metric_snapshots": self.research_metric_snapshots,
            "news": self.news_summary, "fundamental": self.fundamental_summary,
            "refs": refs,
        }
        if self.presentation_id != stable_hash(identity):
            raise ContractViolation("single-stock presentation identity mismatch")
        for name, value in (
            ("analysis_mode", mode), ("as_of", as_of), ("built_at", built),
            ("forecasts", forecasts), ("source_artifact_refs", refs),
        ):
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class PortfolioPresentationInput:
    presentation_id: str
    market: Market
    analysis_mode: DecisionMode
    as_of: datetime
    history_period: str
    account_snapshot: AccountSnapshot
    frozen_account_valuation: FrozenAccountValuation
    portfolio_decision_bundle: PortfolioDecisionBundle
    instruments: tuple[SingleStockPresentationInput, ...]
    watchlist_snapshot: WatchlistSnapshot | None
    portfolio_learning_evidence: tuple[LearningMetricSnapshot, ...]
    portfolio_research_evidence: tuple[ResearchMetricSnapshot, ...]
    source_artifact_refs: tuple[str, ...]
    built_at: datetime

    def __post_init__(self):
        market = _enum(Market, self.market, "portfolio market")
        mode = _enum(DecisionMode, self.analysis_mode, "portfolio analysis mode")
        as_of = ensure_utc(self.as_of, "portfolio as_of")
        built = ensure_utc(self.built_at, "portfolio built_at")
        instruments = tuple(sorted(self.instruments, key=lambda item: item.instrument.stable_key))
        refs = _refs(self.source_artifact_refs)
        required = set(presentation_source_refs(
            self.account_snapshot, self.frozen_account_valuation,
            self.portfolio_decision_bundle, self.watchlist_snapshot,
            self.portfolio_learning_evidence,
            self.portfolio_research_evidence,
        ))
        required.add(PRESENTATION_POLICY_REF)
        required.update(ref for item in instruments for ref in item.source_artifact_refs)
        if refs != tuple(sorted(required)):
            raise ContractViolation("portfolio source references do not close over frozen artifacts")
        if (not self.history_period or built < as_of or self.account_snapshot.market is not market or
                self.frozen_account_valuation.market is not market or self.portfolio_decision_bundle.market is not market or
                self.frozen_account_valuation.account_hash != stable_hash(self.account_snapshot) or
                self.portfolio_decision_bundle.account_hash != self.frozen_account_valuation.account_hash or
                self.portfolio_decision_bundle.valuation_id != self.frozen_account_valuation.valuation_id or
                any(item.instrument.market is not market or item.analysis_mode is not mode or item.as_of != as_of or
                    item.risk_bundle.account_hash != self.frozen_account_valuation.account_hash or
                    item.risk_bundle.valuation_id != self.frozen_account_valuation.valuation_id
                    for item in instruments) or
                (self.watchlist_snapshot is not None and self.watchlist_snapshot.market is not market)):
            raise ContractViolation("invalid portfolio presentation input")
        if (self.account_snapshot.captured_at > as_of or
                self.frozen_account_valuation.valuation_at > as_of or
                self.frozen_account_valuation.generated_at > built or
                self.portfolio_decision_bundle.generated_at > built or
                (self.watchlist_snapshot is not None and self.watchlist_snapshot.created_at > built) or
                any(item.generated_at > built for item in (
                    *self.portfolio_learning_evidence, *self.portfolio_research_evidence,
                ))):
            raise ContractViolation("portfolio presentation contains future account or evidence facts")
        identity = {
            "market": market, "mode": mode, "as_of": as_of,
            "history": self.history_period, "account": self.account_snapshot,
            "valuation": self.frozen_account_valuation,
            "portfolio": self.portfolio_decision_bundle,
            "instruments": instruments, "watchlist": self.watchlist_snapshot,
            "learning": self.portfolio_learning_evidence,
            "research": self.portfolio_research_evidence, "refs": refs,
        }
        if self.presentation_id != stable_hash(identity):
            raise ContractViolation("portfolio presentation identity mismatch")
        for name, value in (
            ("market", market), ("analysis_mode", mode), ("as_of", as_of),
            ("built_at", built), ("instruments", instruments),
            ("source_artifact_refs", refs),
        ):
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class ReportDocument:
    report_id:str; report_kind:ReportKind; market:Market; instrument:InstrumentId|None; analysis_mode:DecisionMode; as_of:datetime; title:str; subtitle:str; summary:str; sections:tuple[ReportSection,...]; glossary_entries:tuple[MetricDefinition,...]; source_artifact_refs:tuple[str,...]; schema_version:int; renderer_version:str; generated_at:datetime
    def __post_init__(self):
        kind=_enum(ReportKind,self.report_kind,"report kind"); market=_enum(Market,self.market,"report market"); mode=_enum(DecisionMode,self.analysis_mode,"report analysis mode"); as_of=ensure_utc(self.as_of,"report as_of"); generated=ensure_utc(self.generated_at,"report generated_at"); refs=_refs(self.source_artifact_refs); sections=tuple(self.sections)
        block_refs = {
            ref
            for section in sections
            for block in section.blocks
            for ref in block.source_artifact_refs
        }
        row_refs = {
            ref
            for section in sections
            for block in section.blocks
            if block.kind is ReportBlockKind.TABLE
            for row in block.payload.rows
            for ref in row.source_artifact_refs
        }
        if (self.schema_version!=1 or not self.renderer_version or not self.title or not self.summary or not sections or
                (kind is ReportKind.SINGLE_STOCK)!=(self.instrument is not None) or
                (self.instrument is not None and self.instrument.market is not market) or generated<as_of or
                block_refs | row_refs != set(refs)):
            raise ContractViolation("invalid report document")
        identity={"kind":kind,"market":market,"instrument":self.instrument,"mode":mode,"as_of":as_of,"title":self.title,"subtitle":self.subtitle,"summary":self.summary,"sections":sections,"glossary":tuple(self.glossary_entries),"refs":refs,"schema":self.schema_version,"renderer":self.renderer_version}
        if self.report_id!=stable_hash(identity): raise ContractViolation("report document identity mismatch")
        object.__setattr__(self,"report_kind",kind); object.__setattr__(self,"market",market); object.__setattr__(self,"analysis_mode",mode); object.__setattr__(self,"as_of",as_of); object.__setattr__(self,"generated_at",generated); object.__setattr__(self,"sections",sections); object.__setattr__(self,"source_artifact_refs",refs)
    @property
    def document_hash(self): return stable_hash({"report_id":self.report_id,"sections":self.sections,"refs":self.source_artifact_refs,"renderer":self.renderer_version})


@dataclass(frozen=True, slots=True)
class ReportFeedback:
    feedback_id:str; report_id:str; rating:int; note:str|None; created_at:datetime
    def __post_init__(self):
        created=ensure_utc(self.created_at,"feedback created_at")
        if not self.report_id or not 1<=self.rating<=5 or (self.note is not None and len(self.note)>1000): raise ContractViolation("invalid report feedback")
        expected=stable_hash({"report":self.report_id,"rating":self.rating,"note":self.note,"created":created})
        if self.feedback_id!=expected: raise ContractViolation("feedback identity mismatch")
        object.__setattr__(self,"created_at",created)


@dataclass(frozen=True, slots=True)
class ReportExportArtifact:
    export_id:str; report_id:str; format:ExportFormat; path:str; content_hash:str|None; status:ExportStatus; error_code:str|None; created_at:datetime
    def __post_init__(self):
        fmt=_enum(ExportFormat,self.format,"export format"); status=_enum(ExportStatus,self.status,"export status"); created=ensure_utc(self.created_at,"export created_at")
        if not self.report_id or not self.path or (status is ExportStatus.COMPLETED)!=(self.content_hash is not None) or (status is ExportStatus.FAILED)!=(self.error_code is not None): raise ContractViolation("invalid export artifact")
        expected=stable_hash({"report":self.report_id,"format":fmt,"path":self.path,"content":self.content_hash,"status":status,"error":self.error_code,"created":created})
        if self.export_id!=expected: raise ContractViolation("export identity mismatch")
        object.__setattr__(self,"format",fmt); object.__setattr__(self,"status",status); object.__setattr__(self,"created_at",created)


@dataclass(frozen=True, slots=True)
class WatchlistSnapshot:
    watchlist_id:str; market:Market; instruments:tuple[InstrumentId,...]; created_at:datetime
    def __post_init__(self):
        market=_enum(Market,self.market,"watchlist market"); instruments=tuple(sorted(set(self.instruments),key=lambda item:item.stable_key)); created=ensure_utc(self.created_at,"watchlist created_at")
        if any(item.market is not market for item in instruments): raise ContractViolation("watchlist market mismatch")
        expected=stable_hash({"market":market,"instruments":instruments,"created":created})
        if self.watchlist_id!=expected: raise ContractViolation("watchlist identity mismatch")
        object.__setattr__(self,"market",market); object.__setattr__(self,"instruments",instruments); object.__setattr__(self,"created_at",created)


@dataclass(frozen=True, slots=True)
class AnalysisTaskProgress:
    task_id:str; stage:AnalysisStage; status:TaskStatus; completed_units:int; total_units:int; instrument:InstrumentId|None; message_code:str; elapsed_seconds:float; retry_at:datetime|None; cancellable:bool; background:bool; emitted_at:datetime
    def __post_init__(self):
        stage=_enum(AnalysisStage,self.stage,"analysis stage"); status=_enum(TaskStatus,self.status,"task status"); emitted=ensure_utc(self.emitted_at,"task emitted_at"); retry=None if self.retry_at is None else ensure_utc(self.retry_at,"task retry_at")
        if not self.task_id or self.completed_units<0 or self.total_units<0 or self.completed_units>self.total_units or self.elapsed_seconds<0 or not isfinite(self.elapsed_seconds) or (status is TaskStatus.WAITING and retry is None): raise ContractViolation("invalid task progress")
        object.__setattr__(self,"stage",stage); object.__setattr__(self,"status",status); object.__setattr__(self,"emitted_at",emitted); object.__setattr__(self,"retry_at",retry)


@dataclass(frozen=True, slots=True)
class HistoricalEvaluationQuery:
    market:Market; ledger_kind:LedgerViewKind|None=None; instrument:InstrumentId|None=None; horizon:int|None=None; model_version:str|None=None; strategy_id:str|None=None; market_regime_key:str|None=None; evidence_origin:str|None=None; date_from:datetime|None=None; date_to:datetime|None=None; include_unverifiable:bool=False
    def __post_init__(self):
        market=_enum(Market,self.market,"evaluation market")
        if self.instrument and self.instrument.market is not market or self.horizon is not None and self.horizon not in {1,3,5,10}: raise ContractViolation("invalid evaluation query")
        start=None if self.date_from is None else ensure_utc(self.date_from,"evaluation date_from"); end=None if self.date_to is None else ensure_utc(self.date_to,"evaluation date_to")
        if start and end and end<start: raise ContractViolation("evaluation range is reversed")
        object.__setattr__(self,"market",market); object.__setattr__(self,"ledger_kind",None if self.ledger_kind is None else _enum(LedgerViewKind,self.ledger_kind,"ledger kind")); object.__setattr__(self,"date_from",start); object.__setattr__(self,"date_to",end)


@dataclass(frozen=True, slots=True)
class ReportHistoryQuery:
    report_kind:ReportKind|None=None; market:Market|None=None; instrument:InstrumentId|None=None; analysis_mode:DecisionMode|None=None; history_period:str|None=None; date_from:datetime|None=None; date_to:datetime|None=None; minimum_rating:int|None=None; include_archived:bool=False; page:int=1; page_size:int=50
    def __post_init__(self):
        if self.page<1 or not 1<=self.page_size<=1000 or self.minimum_rating is not None and not 1<=self.minimum_rating<=5: raise ContractViolation("invalid report history query")
        market=None if self.market is None else _enum(Market,self.market,"history market")
        if self.instrument and market and self.instrument.market is not market: raise ContractViolation("history market mismatch")
        start=None if self.date_from is None else ensure_utc(self.date_from,"history date_from"); end=None if self.date_to is None else ensure_utc(self.date_to,"history date_to")
        if start and end and end<start: raise ContractViolation("history range is reversed")
        object.__setattr__(self,"report_kind",None if self.report_kind is None else _enum(ReportKind,self.report_kind,"history report kind")); object.__setattr__(self,"market",market); object.__setattr__(self,"analysis_mode",None if self.analysis_mode is None else _enum(DecisionMode,self.analysis_mode,"history mode")); object.__setattr__(self,"date_from",start); object.__setattr__(self,"date_to",end)


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    """持久化的不可变报告外壳；document 本身仍是唯一展示事实。"""
    report_id:str; report_document_json:str; document_hash:str; report_kind:ReportKind; market:Market; instrument:InstrumentId|None; analysis_mode:DecisionMode; as_of:datetime; source_artifact_refs:tuple[str,...]; renderer_version:str; history_period:str; archived:bool; created_at:datetime; latest_rating:int|None=None
    def __post_init__(self):
        kind=_enum(ReportKind,self.report_kind,"snapshot kind"); market=_enum(Market,self.market,"snapshot market"); mode=_enum(DecisionMode,self.analysis_mode,"snapshot mode"); as_of=ensure_utc(self.as_of,"snapshot as_of"); created=ensure_utc(self.created_at,"snapshot created_at"); refs=_refs(self.source_artifact_refs)
        if not self.report_id or not self.report_document_json or len(self.document_hash)!=64 or not self.renderer_version or not self.history_period or self.latest_rating is not None and not 1<=self.latest_rating<=5 or (kind is ReportKind.SINGLE_STOCK)!=(self.instrument is not None) or self.instrument is not None and self.instrument.market is not market: raise ContractViolation("invalid report snapshot")
        object.__setattr__(self,"report_kind",kind); object.__setattr__(self,"market",market); object.__setattr__(self,"analysis_mode",mode); object.__setattr__(self,"as_of",as_of); object.__setattr__(self,"created_at",created); object.__setattr__(self,"source_artifact_refs",refs)


@dataclass(frozen=True, slots=True)
class HistoricalEvaluationView:
    query:HistoricalEvaluationQuery; maturity_summary:tuple[tuple[str,object],...]; headline_metrics:tuple[tuple[str,object],...]; charts:tuple[ChartSpec,...]; tables:tuple[ReportTable,...]; glossary_entries:tuple[MetricDefinition,...]; warnings:tuple[str,...]; source_artifact_refs:tuple[str,...]; built_at:datetime
    def __post_init__(self):
        built=ensure_utc(self.built_at,"evaluation built_at"); refs=_refs(self.source_artifact_refs)
        row_refs = {ref for table in self.tables for row in table.rows for ref in row.source_artifact_refs}
        if (any(not isinstance(chart,ChartSpec) for chart in self.charts) or
                any(not isinstance(table,ReportTable) for table in self.tables) or
                PRESENTATION_POLICY_REF not in refs or not row_refs.issubset(set(refs))):
            raise ContractViolation("invalid historical evaluation view")
        object.__setattr__(self,"maturity_summary",tuple(sorted(self.maturity_summary))); object.__setattr__(self,"headline_metrics",tuple(sorted(self.headline_metrics))); object.__setattr__(self,"charts",tuple(self.charts)); object.__setattr__(self,"tables",tuple(self.tables)); object.__setattr__(self,"warnings",tuple(self.warnings)); object.__setattr__(self,"source_artifact_refs",refs); object.__setattr__(self,"built_at",built)


@dataclass(frozen=True, slots=True)
class ReportHistoryPage:
    query:ReportHistoryQuery; items:tuple[ReportSnapshot,...]; total_count:int; has_next:bool
    def __post_init__(self):
        if self.total_count<0 or len(self.items)>self.total_count: raise ContractViolation("invalid report history page")
        object.__setattr__(self,"items",tuple(self.items))
