"""V2-10 受控 LLM 研究假设的不可变合同。

此处只承载冻结事实、结构化假设和验证结果；绝不承载订单、仓位或模型生成的
价格/概率。所有文本仅是研究说明，不能替代可验证事实。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from math import isfinite

from .enums import Market
from .market_data import ContractViolation, InstrumentId, ensure_utc, stable_hash
from .strategy import ConditionEvaluation


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ResearchScope(_StringEnum): SINGLE_STOCK="single_stock"; PORTFOLIO="portfolio"
class HypothesisKind(_StringEnum): FORECAST_PATTERN="forecast_pattern"; MODEL_CONFIGURATION="model_configuration"; STRATEGY_CONFIGURATION="strategy_configuration"; SYSTEM_CHALLENGE="system_challenge"; IMPLEMENTATION_PROPOSAL="implementation_proposal"
class HypothesisValidationStatus(_StringEnum): CONFIRMED="confirmed"; REFUTED="refuted"; PENDING="pending"; INVALID_DATA="invalid_data"
class CandidateEligibility(_StringEnum): ELIGIBLE_FOR_OOF="eligible_for_oof"; OBSERVATION_ONLY="observation_only"; IMPLEMENTATION_REQUIRED="implementation_required"; REJECTED="rejected"
class ResearchRunStatus(_StringEnum): PENDING="pending"; COMPLETED="completed"; PARTIAL="partial"; UNAVAILABLE="unavailable"; FAILED="failed"
class InvocationStatus(_StringEnum): SUCCEEDED="succeeded"; TRANSPORT_FAILED="transport_failed"; TIMED_OUT="timed_out"; TRUNCATED="truncated"; EMPTY="empty"; INVALID_SCHEMA="invalid_schema"
class HypothesisOutcomeStatus(_StringEnum): PENDING="pending"; MATURED="matured"; UNVERIFIABLE="unverifiable"; NOT_APPLICABLE="not_applicable"; SUPERSEDED="superseded"
class HypothesisNovelty(_StringEnum): NOVEL="novel"; OVERLAPS_EXISTING="overlaps_existing"; DUPLICATE="duplicate"

RESEARCH_FACT_NAMESPACES=(
    "feature.closed.", "feature.current.", "feature.news.", "feature.fund.",
    "feature.context.", "forecast.", "scenario.", "strategy.", "risk.",
    "learning.", "portfolio.", "position.",
)
RESEARCH_FACT_STATUSES=frozenset(("available","missing","insufficient_history","stale","blocked","not_applicable","invalid","conflicting"))

RESEARCH_REASON_CODES=frozenset("""
RESEARCH_CONTEXT_FROZEN RESEARCH_CONTEXT_INCOMPLETE RESEARCH_LLM_UNCONFIGURED RESEARCH_LLM_TRANSPORT_FAILED RESEARCH_LLM_TIMEOUT RESEARCH_RESPONSE_TRUNCATED RESEARCH_RESPONSE_EMPTY RESEARCH_SCHEMA_INVALID RESEARCH_HYPOTHESIS_PARSED RESEARCH_HYPOTHESIS_LIMIT_EXCEEDED RESEARCH_INSTRUMENT_UNKNOWN RESEARCH_EVIDENCE_REFERENCE_UNKNOWN RESEARCH_EVIDENCE_AFTER_CUTOFF RESEARCH_FACT_CONFIRMED RESEARCH_FACT_REFUTED RESEARCH_FACT_PENDING_EVENT RESEARCH_FACT_MISSING RESEARCH_FACT_STALE RESEARCH_FACT_BLOCKED RESEARCH_FACT_CONFLICTING RESEARCH_FINANCIAL_SOURCE_REQUIRED RESEARCH_PROMPT_INJECTION_IGNORED RESEARCH_CURRENT_OBSERVATION_ONLY RESEARCH_NO_DIRECT_EXECUTION RESEARCH_NO_EXECUTION_LEVEL RESEARCH_REGISTERED_MODEL_MAPPING RESEARCH_REGISTERED_FEATURE_SET_MAPPING RESEARCH_REGISTERED_STRATEGY_MAPPING RESEARCH_PARAMETER_WITHIN_BOUNDS RESEARCH_PARAMETER_OUT_OF_BOUNDS RESEARCH_UNKNOWN_MODEL_FAMILY RESEARCH_UNKNOWN_FEATURE RESEARCH_UNKNOWN_STRATEGY_TEMPLATE RESEARCH_UNKNOWN_DSL_OPERATOR RESEARCH_RISK_SPEC_INCOMPLETE RESEARCH_IMPLEMENTATION_REQUIRED RESEARCH_CANDIDATE_CREATED RESEARCH_CANDIDATE_LIMIT_REACHED RESEARCH_OOF_REQUIRED RESEARCH_PROMOTION_DELEGATED_TO_LEARNING RESEARCH_DUPLICATE_HYPOTHESIS RESEARCH_REVISION_CREATED RESEARCH_DIRECTION_OUTCOME_SCORED RESEARCH_UNTRIGGERED_NOT_SCORED RESEARCH_SYSTEM_RESULT_NOT_LLM_CREDIT RESEARCH_CANDIDATE_RESULT_LINKED RESEARCH_MARKET_ISOLATED RESEARCH_SOURCE_CODE_IMMUTABLE RESEARCH_SECRETS_REDACTED RESEARCH_USER_VISIBLE
""".split())


def _enum(kind, value, field):
    try: return value if isinstance(value, kind) else kind(str(value))
    except ValueError as exc: raise ContractViolation(f"unsupported {field}: {value}") from exc


def _reasons(values):
    result=tuple(sorted(set(values)))
    if any(value not in RESEARCH_REASON_CODES for value in result): raise ContractViolation("unknown research reason code")
    return result


@dataclass(frozen=True, slots=True)
class ResearchFact:
    fact_id:str; instrument:InstrumentId|None; key:str; value:object|None; value_type:str; unit:str|None; status:str; available_at:datetime; source_refs:tuple[str,...]; source_payload_hash:str|None
    def __post_init__(self):
        available=ensure_utc(self.available_at,"fact available_at"); refs=tuple(sorted(set(self.source_refs)))
        if not self.key.startswith(RESEARCH_FACT_NAMESPACES) or not self.value_type or self.status not in RESEARCH_FACT_STATUSES or not refs: raise ContractViolation("research fact requires registered key, status and sources")
        if self.source_payload_hash is not None and (len(self.source_payload_hash)!=64 or any(item not in "0123456789abcdef" for item in self.source_payload_hash)):
            raise ContractViolation("research fact source hash must be SHA-256")
        if (self.status=="available") != (self.value is not None):
            raise ContractViolation("research fact availability and value mismatch")
        if self.key.startswith("feature.fund.") and self.value is not None and not self.source_payload_hash: raise ContractViolation("financial fact requires canonical source")
        expected=stable_hash({"instrument":self.instrument,"key":self.key,"value":self.value,"status":self.status,"available_at":available,"source_refs":refs,"source_payload_hash":self.source_payload_hash})
        if self.fact_id!=expected: raise ContractViolation("research fact identity mismatch")
        object.__setattr__(self,"available_at",available); object.__setattr__(self,"source_refs",refs)


@dataclass(frozen=True, slots=True)
class ResearchFactManifest:
    manifest_id:str; scope:ResearchScope; market:Market; cutoff_at:datetime; instruments:tuple[InstrumentId,...]; facts:tuple[ResearchFact,...]; artifact_refs:tuple[str,...]; schema_version:int; generated_at:datetime
    def __post_init__(self):
        scope=_enum(ResearchScope,self.scope,"research scope"); cutoff=ensure_utc(self.cutoff_at,"manifest cutoff"); generated=ensure_utc(self.generated_at,"manifest generated_at")
        instruments=tuple(sorted(set(self.instruments),key=lambda item:item.stable_key)); facts=tuple(sorted(self.facts,key=lambda item:item.fact_id)); artifacts=tuple(sorted(set(self.artifact_refs)))
        instrument_set=set(instruments)
        if self.schema_version!=1 or not instruments or len(instruments)>50 or any(item.market is not self.market for item in instruments) or any(item.available_at>cutoff or (item.instrument is not None and (item.instrument not in instrument_set or item.instrument.market is not self.market)) for item in facts): raise ContractViolation("invalid frozen research manifest")
        if any(not set(item.source_refs).issubset(artifacts) for item in facts):
            raise ContractViolation("research fact sources must belong to frozen artifacts")
        if len({(item.instrument,item.key) for item in facts}) != len(facts): raise ContractViolation("manifest has duplicate fact keys")
        expected=stable_hash({"scope":scope,"market":self.market,"cutoff":cutoff,"instruments":instruments,"facts":facts,"artifacts":artifacts,"schema":self.schema_version})
        if self.manifest_id!=expected: raise ContractViolation("research manifest identity mismatch")
        object.__setattr__(self,"scope",scope); object.__setattr__(self,"cutoff_at",cutoff); object.__setattr__(self,"generated_at",generated); object.__setattr__(self,"instruments",instruments); object.__setattr__(self,"facts",facts); object.__setattr__(self,"artifact_refs",artifacts)


@dataclass(frozen=True, slots=True)
class ResearchContext:
    context_id:str; scope:ResearchScope; market:Market; mode:str; cutoff_at:datetime; manifest:ResearchFactManifest; instrument_roles:tuple[tuple[InstrumentId,str],...]; forecast_event_keys:tuple[str,...]; scenario_ids:tuple[str,...]; strategy_bundle_ids:tuple[str,...]; risk_bundle_ids:tuple[str,...]; portfolio_bundle_id:str|None; learning_snapshot_ids:tuple[str,...]; prompt_input_version:str; generated_at:datetime
    def __post_init__(self):
        scope=_enum(ResearchScope,self.scope,"context scope"); cutoff=ensure_utc(self.cutoff_at,"context cutoff"); generated=ensure_utc(self.generated_at,"context generated_at"); roles=tuple(sorted(self.instrument_roles,key=lambda item:item[0].stable_key))
        role_instruments=tuple(instrument for instrument,_ in roles)
        if self.manifest.scope is not scope or self.manifest.market is not self.market or self.manifest.cutoff_at!=cutoff or not self.mode or not self.prompt_input_version or len(set(role_instruments))!=len(role_instruments) or set(role_instruments)!=set(self.manifest.instruments): raise ContractViolation("invalid research context")
        if scope is ResearchScope.SINGLE_STOCK and (len(roles)!=1 or roles[0][1]!="subject"):
            raise ContractViolation("single-stock research requires exactly one subject")
        if scope is ResearchScope.PORTFOLIO and any(role=="subject" for _,role in roles):
            raise ContractViolation("portfolio research cannot contain a subject role")
        if any(instrument.market is not self.market or role not in {"subject","holding","watchlist"} for instrument,role in roles): raise ContractViolation("research context role mismatch")
        context_artifacts=set(self.forecast_event_keys) | set(self.scenario_ids) | set(self.strategy_bundle_ids) | set(self.risk_bundle_ids) | set(self.learning_snapshot_ids)
        if self.portfolio_bundle_id:
            context_artifacts.add(self.portfolio_bundle_id)
        if not context_artifacts.issubset(self.manifest.artifact_refs):
            raise ContractViolation("research context references artifacts outside manifest")
        expected=stable_hash({"scope":scope,"market":self.market,"mode":self.mode,"cutoff":cutoff,"manifest":self.manifest.manifest_id,"roles":roles,"forecast":tuple(sorted(self.forecast_event_keys)),"scenario":tuple(sorted(self.scenario_ids)),"strategy":tuple(sorted(self.strategy_bundle_ids)),"risk":tuple(sorted(self.risk_bundle_ids)),"portfolio":self.portfolio_bundle_id,"learning":tuple(sorted(self.learning_snapshot_ids)),"prompt_input_version":self.prompt_input_version})
        if self.context_id!=expected: raise ContractViolation("research context identity mismatch")
        object.__setattr__(self,"scope",scope); object.__setattr__(self,"cutoff_at",cutoff); object.__setattr__(self,"generated_at",generated); object.__setattr__(self,"instrument_roles",roles); object.__setattr__(self,"forecast_event_keys",tuple(sorted(set(self.forecast_event_keys)))); object.__setattr__(self,"scenario_ids",tuple(sorted(set(self.scenario_ids)))); object.__setattr__(self,"strategy_bundle_ids",tuple(sorted(set(self.strategy_bundle_ids)))); object.__setattr__(self,"risk_bundle_ids",tuple(sorted(set(self.risk_bundle_ids)))); object.__setattr__(self,"learning_snapshot_ids",tuple(sorted(set(self.learning_snapshot_ids))))


@dataclass(frozen=True, slots=True)
class RawResearchResponse:
    response_id:str; request_id:str; context_id:str; revision:int; provider_name:str; model_name:str; content:str; content_hash:str; finish_reason:str|None; invocation_status:InvocationStatus; received_at:datetime; prompt_version:str; prompt_hash:str; provider_request_id:str|None=None; token_usage:int|None=None
    def __post_init__(self):
        status=_enum(InvocationStatus,self.invocation_status,"invocation status"); received=ensure_utc(self.received_at,"response received_at")
        if self.revision<1 or not self.request_id or not self.context_id or not self.provider_name or not self.model_name or len(self.content_hash)!=64 or len(self.prompt_hash)!=64 or (status is InvocationStatus.SUCCEEDED and not self.content) or (self.token_usage is not None and self.token_usage<0): raise ContractViolation("invalid research response")
        if stable_hash(self.content)!=self.content_hash: raise ContractViolation("research response content hash mismatch")
        expected=stable_hash({"request":self.request_id,"context":self.context_id,"revision":self.revision,"provider":self.provider_name,"model":self.model_name,"content_hash":self.content_hash,"finish":self.finish_reason,"status":status,"prompt_version":self.prompt_version,"prompt_hash":self.prompt_hash})
        if self.response_id!=expected: raise ContractViolation("research response identity mismatch")
        object.__setattr__(self,"invocation_status",status); object.__setattr__(self,"received_at",received)


@dataclass(frozen=True, slots=True)
class ResearchHypothesis:
    hypothesis_id:str; business_key:str; response_id:str; context_id:str; instrument:InstrumentId|None; kind:HypothesisKind; title:str; thesis:str; evidence_refs:tuple[str,...]; payload:tuple[tuple[str,object],...]; novelty:HypothesisNovelty; generated_at:datetime
    def __post_init__(self):
        kind=_enum(HypothesisKind,self.kind,"hypothesis kind"); novelty=_enum(HypothesisNovelty,self.novelty,"hypothesis novelty"); refs=tuple(sorted(set(self.evidence_refs))); payload=tuple(sorted(self.payload,key=lambda item:item[0])); generated=ensure_utc(self.generated_at,"hypothesis generated_at")
        if not self.business_key or not self.response_id or not self.context_id or not self.title or len(self.title)>80 or len(self.thesis)>500 or not refs or len({key for key,_ in payload})!=len(payload): raise ContractViolation("invalid research hypothesis")
        if kind is not HypothesisKind.SYSTEM_CHALLENGE and self.instrument is None: raise ContractViolation("non-portfolio hypothesis requires instrument")
        expected=stable_hash({"business":self.business_key,"response":self.response_id,"context":self.context_id,"instrument":self.instrument,"kind":kind,"evidence":refs,"payload":payload,"novelty":novelty})
        if self.hypothesis_id!=expected: raise ContractViolation("research hypothesis identity mismatch")
        object.__setattr__(self,"kind",kind); object.__setattr__(self,"novelty",novelty); object.__setattr__(self,"evidence_refs",refs); object.__setattr__(self,"payload",payload); object.__setattr__(self,"generated_at",generated)


@dataclass(frozen=True, slots=True)
class HypothesisValidation:
    validation_id:str; hypothesis_id:str; context_id:str; status:HypothesisValidationStatus; observed_fact_ids:tuple[str,...]; missing_fact_ids:tuple[str,...]; conflicting_fact_ids:tuple[str,...]; linked_artifact_ids:tuple[str,...]; candidate_eligibility:CandidateEligibility; validator_version:str; reason_codes:tuple[str,...]; evaluated_at:datetime; generated_at:datetime; condition_evaluation:ConditionEvaluation|None=None
    def __post_init__(self):
        status=_enum(HypothesisValidationStatus,self.status,"validation status"); eligibility=_enum(CandidateEligibility,self.candidate_eligibility,"candidate eligibility"); reasons=_reasons(self.reason_codes); evaluated=ensure_utc(self.evaluated_at,"validation evaluated_at"); generated=ensure_utc(self.generated_at,"validation generated_at")
        if not self.hypothesis_id or not self.context_id or not self.validator_version or generated<evaluated: raise ContractViolation("invalid hypothesis validation")
        expected=stable_hash({"hypothesis":self.hypothesis_id,"context":self.context_id,"status":status,"condition_evaluation":self.condition_evaluation,"observed":tuple(sorted(set(self.observed_fact_ids))),"missing":tuple(sorted(set(self.missing_fact_ids))),"conflicting":tuple(sorted(set(self.conflicting_fact_ids))),"artifacts":tuple(sorted(set(self.linked_artifact_ids))),"eligibility":eligibility,"validator":self.validator_version,"reasons":reasons,"evaluated":evaluated})
        if self.validation_id!=expected: raise ContractViolation("hypothesis validation identity mismatch")
        for name in ("observed_fact_ids","missing_fact_ids","conflicting_fact_ids","linked_artifact_ids"): object.__setattr__(self,name,tuple(sorted(set(getattr(self,name)))))
        object.__setattr__(self,"status",status); object.__setattr__(self,"candidate_eligibility",eligibility); object.__setattr__(self,"reason_codes",reasons); object.__setattr__(self,"evaluated_at",evaluated); object.__setattr__(self,"generated_at",generated)


@dataclass(frozen=True, slots=True)
class HypothesisCandidateLink:
    link_id:str; hypothesis_id:str; candidate_id:str|None; eligibility:CandidateEligibility; mapping_registry_version:str; mapping_key:str|None; rejection_reasons:tuple[str,...]; created_at:datetime
    def __post_init__(self):
        eligibility=_enum(CandidateEligibility,self.eligibility,"link eligibility"); reasons=_reasons(self.rejection_reasons); created=ensure_utc(self.created_at,"candidate link created_at")
        if not self.hypothesis_id or not self.mapping_registry_version or (eligibility is CandidateEligibility.ELIGIBLE_FOR_OOF and (not self.candidate_id or not self.mapping_key)) or (eligibility is not CandidateEligibility.ELIGIBLE_FOR_OOF and self.candidate_id is not None): raise ContractViolation("invalid hypothesis candidate link")
        expected=stable_hash({"hypothesis":self.hypothesis_id,"candidate":self.candidate_id,"eligibility":eligibility,"mapping_version":self.mapping_registry_version,"mapping_key":self.mapping_key,"reasons":reasons})
        if self.link_id!=expected: raise ContractViolation("candidate link identity mismatch")
        object.__setattr__(self,"eligibility",eligibility); object.__setattr__(self,"rejection_reasons",reasons); object.__setattr__(self,"created_at",created)

@dataclass(frozen=True, slots=True)
class HypothesisOutcome:
    outcome_id:str; hypothesis_id:str; observation_event_key:str; instrument:InstrumentId; origin_session_date:date; target_session_date:date|None; horizon:int|None; trigger_status:HypothesisValidationStatus; expected_direction:str|None; actual_direction:str|None; actual_return:float|None; direction_correct:bool|None; linked_maturity_evidence_id:str|None; linked_forecast_outcome_id:str|None; linked_candidate_id:str|None; linked_promotion_ids:tuple[str,...]; status:HypothesisOutcomeStatus; evidence_grade:str; evaluated_at:datetime; generated_at:datetime
    def __post_init__(self):
        status=_enum(HypothesisOutcomeStatus,self.status,"hypothesis outcome status"); trigger=_enum(HypothesisValidationStatus,self.trigger_status,"outcome trigger status"); evaluated=ensure_utc(self.evaluated_at,"outcome evaluated_at"); generated=ensure_utc(self.generated_at,"outcome generated_at")
        if not isinstance(self.instrument,InstrumentId): raise ContractViolation("research outcome requires an instrument")
        if self.horizon is not None and self.horizon not in {1,3,5,10}: raise ContractViolation("invalid research outcome horizon")
        if self.actual_return is not None and (isinstance(self.actual_return,bool) or not isfinite(float(self.actual_return))): raise ContractViolation("invalid research outcome return")
        scored=status is HypothesisOutcomeStatus.MATURED
        # 有 horizon 的成熟项是 forecast direction outcome，必须有 V2-9 到期
        # 证据；模型/策略 candidate 的成熟项只表示 OOF/Promotion 事实，不伪造
        # 方向命中，因此允许 horizon=None 且 direction_correct=None。
        if scored and self.horizon is not None and (trigger is not HypothesisValidationStatus.CONFIRMED or self.expected_direction not in {"bullish","neutral","bearish"} or self.actual_direction not in {"bullish","neutral","bearish"} or self.direction_correct is None or not self.linked_maturity_evidence_id or not self.linked_forecast_outcome_id): raise ContractViolation("matured LLM outcome requires issued confirmed evidence")
        if scored and self.horizon is None and (trigger is not HypothesisValidationStatus.CONFIRMED or not self.linked_candidate_id or not self.linked_promotion_ids or not self.evidence_grade.startswith("candidate_")): raise ContractViolation("matured candidate outcome requires confirmed linked OOF evidence")
        if self.direction_correct is not None and self.direction_correct != (self.expected_direction==self.actual_direction): raise ContractViolation("research direction correctness mismatch")
        if trigger is not HypothesisValidationStatus.CONFIRMED and self.direction_correct is not None: raise ContractViolation("untriggered hypothesis cannot receive direction credit")
        identity={"hypothesis":self.hypothesis_id,"event":self.observation_event_key,"instrument":self.instrument,"origin":self.origin_session_date,"target":self.target_session_date,"horizon":self.horizon,"trigger":trigger,"expected":self.expected_direction,"actual":self.actual_direction,"actual_return":self.actual_return,"direction_correct":self.direction_correct,"maturity":self.linked_maturity_evidence_id,"forecast":self.linked_forecast_outcome_id,"candidate":self.linked_candidate_id,"promotions":tuple(sorted(set(self.linked_promotion_ids))),"status":status,"evidence_grade":self.evidence_grade}
        if self.outcome_id!=stable_hash(identity) or generated<evaluated: raise ContractViolation("research outcome identity mismatch")
        object.__setattr__(self,"status",status); object.__setattr__(self,"trigger_status",trigger); object.__setattr__(self,"linked_promotion_ids",tuple(sorted(set(self.linked_promotion_ids)))); object.__setattr__(self,"evaluated_at",evaluated); object.__setattr__(self,"generated_at",generated)


@dataclass(frozen=True, slots=True)
class ResearchMetricSnapshot:
    """LLM 专属账本切片；不与 V2-9 的预测/策略/联合账混合。"""
    snapshot_id:str; market:Market; scope_key:str; cutoff_at:datetime; metrics:tuple[tuple[str,float|None],...]; generated_at:datetime; dimensions:tuple[tuple[str,str],...]=()
    def __post_init__(self):
        cutoff=ensure_utc(self.cutoff_at,"research metric cutoff"); generated=ensure_utc(self.generated_at,"research metric generated")
        values=tuple(sorted((str(key),None if value is None else float(value)) for key,value in self.metrics))
        if not self.scope_key or len({key for key,_ in values})!=len(values) or any(value is not None and not isfinite(value) for _,value in values): raise ContractViolation("invalid research metric snapshot")
        dimensions=tuple(sorted((str(key),str(value)) for key,value in self.dimensions))
        if len({key for key,_ in dimensions})!=len(dimensions): raise ContractViolation("duplicate research metric dimension")
        identity={"market":self.market,"scope":self.scope_key,"cutoff":cutoff,"metrics":values,"dimensions":dimensions}
        if self.snapshot_id!=stable_hash(identity): raise ContractViolation("research metric snapshot identity mismatch")
        object.__setattr__(self,"cutoff_at",cutoff); object.__setattr__(self,"metrics",values); object.__setattr__(self,"generated_at",generated); object.__setattr__(self,"dimensions",dimensions)
