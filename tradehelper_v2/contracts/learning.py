"""V2-9 学习层不可变事实合同。

学习层只记录已冻结决策的到期结果和受控候选生命周期；不改写预测、计划、
风控或生产源码。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from math import isfinite

from .account import as_decimal
from .enums import Market
from .forecast import DirectionProbabilities, ForecastDirection, ForecastScope
from .market_data import ContractViolation, InstrumentId, ensure_utc, stable_hash


class _StringEnum(str, Enum):
    def __str__(self):
        return self.value


class EvidenceOrigin(_StringEnum): ISSUED_ONLINE="issued_online"; RECONSTRUCTED_OOF="reconstructed_oof"; SHADOW_ONLINE="shadow_online"
class OutcomeStatus(_StringEnum): PENDING="pending"; MATURED="matured"; UNVERIFIABLE="unverifiable"; CONFLICTING="conflicting"; SUPERSEDED="superseded"
class LearningEvidenceGrade(_StringEnum): HIGH="high"; MEDIUM="medium"; LOW="low"; INSUFFICIENT="insufficient"
class LedgerKind(_StringEnum): FORECAST="forecast"; STRATEGY="strategy"; JOINT="joint"
class CandidateKind(_StringEnum): FORECAST_CONFIGURATION="forecast_configuration"; SCENARIO_SOFT_POLICY="scenario_soft_policy"; STRATEGY_PARAMETER_SET="strategy_parameter_set"; RISK_SOFT_POLICY="risk_soft_policy"; PORTFOLIO_SOFT_POLICY="portfolio_soft_policy"; EXECUTION_SOFT_POLICY="execution_soft_policy"
class CandidateScope(_StringEnum): STOCK="stock"; INDUSTRY="industry"; MARKET="market"
class JointOutcomeKind(_StringEnum): RECOMMENDATION_REPLAY="recommendation_replay"; POLICY_OOF="policy_oof"; BROKER_OBSERVED="broker_observed"
class CandidateLifecycle(_StringEnum): CANDIDATE="candidate"; CHALLENGER="challenger"; SHADOW="shadow"; CHAMPION="champion"; DRIFTED="drifted"; RETIRED="retired"; ROLLED_BACK="rolled_back"
class PromotionDecision(_StringEnum): HOLD="hold"; PROMOTE_TO_SHADOW="promote_to_shadow"; PROMOTE_TO_CHALLENGER="promote_to_challenger"; PROMOTE_TO_CHAMPION="promote_to_champion"; REJECT="reject"; ROLLBACK="rollback"; SUSPEND_NEW_RISK="suspend_new_risk"
class LearningRunStatus(_StringEnum): PENDING="pending"; RUNNING="running"; COMPLETED="completed"; FAILED="failed"; CANCELLED="cancelled"


LEARNING_REASON_CODES=frozenset("""
LEARNING_PENDING_TARGET_SESSION LEARNING_MATURED LEARNING_TARGET_BAR_MISSING LEARNING_TARGET_BAR_NOT_FINAL LEARNING_CALENDAR_UNAVAILABLE LEARNING_LISTING_WINDOW_INSUFFICIENT LEARNING_ADJUSTMENT_MISMATCH LEARNING_LABEL_POLICY_UNAVAILABLE LEARNING_EVIDENCE_CONFLICT LEARNING_REVISION_SUPERSEDED LEARNING_DUPLICATE_IGNORED LEARNING_ISSUED_ONLINE LEARNING_RECONSTRUCTED_OOF LEARNING_SHADOW_ONLY LEARNING_FORECAST_SCORED LEARNING_FORECAST_UNAVAILABLE_NOT_SCORED LEARNING_SCENARIO_ATTRIBUTED LEARNING_PLAN_NOT_TRIGGERED LEARNING_PLAN_TRIGGERED LEARNING_ORDER_REJECTED LEARNING_ORDER_FILLED LEARNING_WINDOW_CLOSE LEARNING_EXIT_EVALUATED LEARNING_EXIT_COST_UNAVAILABLE LEARNING_DAILY_PATH_AMBIGUOUS LEARNING_EXECUTION_EVIDENCE_LOW LEARNING_PORTFOLIO_SEQUENTIAL_REPLAY LEARNING_PATH_METRICS_UNAVAILABLE LEARNING_COUNTERFACTUAL_PAIRED LEARNING_COUNTERFACTUAL_UNAVAILABLE LEARNING_STOCK_SCOPE LEARNING_INDUSTRY_FALLBACK LEARNING_MARKET_FALLBACK LEARNING_SAMPLE_INSUFFICIENT LEARNING_POSITIVE_UNCERTAIN LEARNING_RELIABLE_POSITIVE LEARNING_NEGATIVE_EXPECTATION LEARNING_DRIFT_DETECTED LEARNING_CANDIDATE_WITHIN_BOUNDS LEARNING_CANDIDATE_OUT_OF_BOUNDS LEARNING_SELECTION_PASSED LEARNING_CONFIRMATION_PASSED LEARNING_SHADOW_PASSED LEARNING_PROMOTED LEARNING_REJECTED LEARNING_ROLLED_BACK LEARNING_NEW_RISK_SUSPENDED LEARNING_HARD_CONSTRAINT_IMMUTABLE LEARNING_SOURCE_CODE_IMMUTABLE
""".split())


def _enum(kind, value, field):
    try: return value if isinstance(value, kind) else kind(str(value))
    except ValueError as exc: raise ContractViolation(f"unsupported {field}: {value}") from exc
def _reasons(values):
    value=tuple(sorted(set(values)))
    if any(item not in LEARNING_REASON_CODES for item in value): raise ContractViolation("unknown learning reason code")
    return value
def _finite(value, field):
    number=float(value)
    if not isfinite(number): raise ContractViolation(f"{field} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class LearningPolicy:
    policy_version:str="learning_policy_v1"; ledger_version:str="learning_ledger_v1"; forecast_horizons:tuple[int,...]=(1,3,5,10); calibration_bins:int=10; bootstrap_draws:int=1000; bootstrap_block_min:int=5; min_observation_samples:int=10; min_reliable_samples:int=30; min_oof_folds:int=3; min_confirmation_samples:int=20; min_shadow_samples:int=20; strategy_evaluation_horizons:tuple[int,...]=(1,3,5,10); selection_fraction:Decimal=Decimal("0.60"); embargo_sessions:int=10; drift_recent_samples:int=30; drift_reference_samples:int=60; max_candidate_count_per_scope:int=20; max_foreground_learning_ms:int=0; parameter_hash:str=""
    def __post_init__(self):
        if (self.ledger_version!="learning_ledger_v1" or self.forecast_horizons!=(1,3,5,10) or self.strategy_evaluation_horizons!=(1,3,5,10) or self.calibration_bins!=10 or self.bootstrap_draws!=1000 or self.bootstrap_block_min!=5 or self.min_observation_samples!=10 or self.min_reliable_samples<30 or self.min_oof_folds<3 or self.min_confirmation_samples<20 or self.min_shadow_samples<20 or self.selection_fraction!=Decimal("0.60") or self.embargo_sessions<max(self.forecast_horizons) or self.drift_recent_samples<30 or self.drift_reference_samples<60 or self.max_candidate_count_per_scope!=20 or self.max_foreground_learning_ms!=0): raise ContractViolation("learning hard policy is immutable")
        expected=stable_hash({key:getattr(self,key) for key in self.__dataclass_fields__ if key!="parameter_hash"})
        if self.parameter_hash and self.parameter_hash!=expected: raise ContractViolation("learning policy hash mismatch")
        object.__setattr__(self,"parameter_hash",expected)


@dataclass(frozen=True, slots=True)
class MaturityEvidence:
    evidence_id:str; instrument:InstrumentId; origin_session_date:date; target_session_date:date; reference_adjustment_mode:str; reference_price:Decimal; target_bar_key:str|None; target_price:Decimal|None; actual_return:Decimal|None; actual_direction:ForecastDirection|None; flat_band:Decimal; bar_source:str|None; bar_payload_hash:str|None; bar_fetched_at:datetime|None; available_at:datetime|None; evaluated_at:datetime; status:OutcomeStatus; evidence_grade:LearningEvidenceGrade; revision:int; supersedes_evidence_id:str|None; reason_codes:tuple[str,...]; generated_at:datetime
    def __post_init__(self):
        status=_enum(OutcomeStatus,self.status,"maturity status"); grade=_enum(LearningEvidenceGrade,self.evidence_grade,"maturity grade"); reference=as_decimal(self.reference_price,"reference price"); band=as_decimal(self.flat_band,"flat band")
        target=None if self.target_price is None else as_decimal(self.target_price,"target price"); result=None if self.actual_return is None else as_decimal(self.actual_return,"actual return")
        evaluated=ensure_utc(self.evaluated_at,"maturity evaluated_at"); available=ensure_utc(self.available_at,"maturity available_at") if self.available_at else None; generated=ensure_utc(self.generated_at,"maturity generated_at")
        unknown_target=(self.target_session_date==self.origin_session_date and self.reference_adjustment_mode=="unavailable" and status in {OutcomeStatus.PENDING,OutcomeStatus.UNVERIFIABLE})
        if (self.target_session_date<self.origin_session_date or (self.target_session_date==self.origin_session_date and not unknown_target) or reference<=0 or band<0 or self.revision<1 or (available and available>evaluated) or generated<evaluated): raise ContractViolation("invalid maturity evidence")
        if status is OutcomeStatus.MATURED:
            if target is None or result is None or self.actual_direction is None or not self.target_bar_key or not self.bar_payload_hash or available is None: raise ContractViolation("matured evidence needs final target bar")
            if result!=target/reference-Decimal("1"): raise ContractViolation("maturity return mismatch")
            direction=ForecastDirection.BULLISH if result>band else ForecastDirection.BEARISH if result<-band else ForecastDirection.NEUTRAL
            if _enum(ForecastDirection,self.actual_direction,"actual direction") is not direction: raise ContractViolation("maturity direction mismatch")
        elif status is not OutcomeStatus.SUPERSEDED and any(value is not None for value in (target,result,self.actual_direction)): raise ContractViolation("unmatured evidence cannot claim outcome")
        # revision 是业务身份；状态转换为 superseded 不得制造第二份同 revision 事实。
        identity={"instrument":self.instrument,"origin":self.origin_session_date,"target":self.target_session_date,"reference_adjustment_mode":self.reference_adjustment_mode,"reference":reference,"target_bar_key":self.target_bar_key,"target_price":target,"revision":self.revision,"supersedes":self.supersedes_evidence_id}
        if self.evidence_id!=stable_hash(identity): raise ContractViolation("maturity evidence identity mismatch")
        for key,value in (("status",status),("evidence_grade",grade),("reference_price",reference),("target_price",target),("actual_return",result),("flat_band",band),("evaluated_at",evaluated),("available_at",available),("generated_at",generated),("reason_codes",_reasons(self.reason_codes))): object.__setattr__(self,key,value)


@dataclass(frozen=True, slots=True)
class ForecastOutcome:
    forecast_outcome_id:str; forecast_event_key:str; instrument:InstrumentId; origin_session_date:date; target_session_date:date; horizon:int; model_scope:ForecastScope; scope_key:str; model_family:str; model_version:str; feature_set_id:str; model_input_hash:str; training_data_hash:str|None; evidence_origin:EvidenceOrigin; maturity_evidence_id:str|None; predicted_direction:ForecastDirection|None; probabilities:DirectionProbabilities|None; predicted_p10:float|None; predicted_p50:float|None; predicted_p90:float|None; actual_direction:ForecastDirection|None; actual_return:Decimal|None; actual_price:Decimal|None; direction_correct:bool|None; event_brier:float|None; event_log_loss:float|None; interval_hit:bool|None; absolute_return_error:float|None; market_regime_key:str|None; status:OutcomeStatus; evidence_grade:LearningEvidenceGrade; reason_codes:tuple[str,...]; evaluated_at:datetime; generated_at:datetime
    def __post_init__(self):
        origin=_enum(EvidenceOrigin,self.evidence_origin,"outcome origin"); status=_enum(OutcomeStatus,self.status,"outcome status"); grade=_enum(LearningEvidenceGrade,self.evidence_grade,"outcome grade")
        if self.horizon not in {1,3,5,10} or (self.target_session_date<=self.origin_session_date and self.actual_direction is not None): raise ContractViolation("invalid forecast outcome horizon")
        scored=(self.actual_direction is not None)
        if status is OutcomeStatus.MATURED and (not scored or self.probabilities is None or self.predicted_direction is None or self.maturity_evidence_id is None): raise ContractViolation("matured forecast outcome missing facts")
        reasons=_reasons(self.reason_codes)
        if scored:
            if self.actual_return is None or self.actual_price is None or self.event_brier is None or self.event_log_loss is None or self.interval_hit is None or self.absolute_return_error is None:
                raise ContractViolation("scored forecast outcome missing metrics")
            for value,field in ((self.event_brier,"event brier"),(self.event_log_loss,"event log loss"),(self.absolute_return_error,"absolute return error")):_finite(value,field)
            if self.predicted_p10 is None or self.predicted_p50 is None or self.predicted_p90 is None or not (self.predicted_p10<=self.predicted_p50<=self.predicted_p90): raise ContractViolation("forecast interval is invalid")
        expected=stable_hash({"forecast_event_key":self.forecast_event_key,"origin":origin,"maturity":self.maturity_evidence_id,"status":status,"actual_return":self.actual_return,"revision":self.maturity_evidence_id,"reasons":reasons})
        if self.forecast_outcome_id!=expected: raise ContractViolation("forecast outcome identity mismatch")
        evaluated=ensure_utc(self.evaluated_at,"outcome evaluated_at"); generated=ensure_utc(self.generated_at,"outcome generated_at")
        if generated < evaluated: raise ContractViolation("forecast outcome generated before evaluation")
        object.__setattr__(self,"evidence_origin",origin); object.__setattr__(self,"status",status); object.__setattr__(self,"evidence_grade",grade); object.__setattr__(self,"reason_codes",reasons); object.__setattr__(self,"evaluated_at",evaluated); object.__setattr__(self,"generated_at",generated)

@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    scenario_outcome_id:str; scenario_id:str; instrument:InstrumentId; forecast_outcome_ids:tuple[str,...]; expected_bias:str; realized_bias:str|None; policy_version:str; evidence_origin:EvidenceOrigin; status:OutcomeStatus; evidence_grade:LearningEvidenceGrade; reason_codes:tuple[str,...]; evaluated_at:datetime; generated_at:datetime
    def __post_init__(self):
        origin=_enum(EvidenceOrigin,self.evidence_origin,"scenario outcome origin"); status=_enum(OutcomeStatus,self.status,"scenario outcome status"); grade=_enum(LearningEvidenceGrade,self.evidence_grade,"scenario evidence")
        ids=tuple(sorted(set(self.forecast_outcome_ids)))
        if not self.scenario_id or not self.policy_version or (status is OutcomeStatus.MATURED and (len(ids)!=4 or self.realized_bias is None)): raise ContractViolation("invalid scenario outcome")
        expected=stable_hash({"scenario":self.scenario_id,"forecast_outcomes":ids,"expected":self.expected_bias,"realized":self.realized_bias,"policy":self.policy_version,"origin":origin,"status":status,"reasons":_reasons(self.reason_codes)})
        if self.scenario_outcome_id!=expected: raise ContractViolation("scenario outcome identity mismatch")
        evaluated=ensure_utc(self.evaluated_at,"scenario evaluated_at"); generated=ensure_utc(self.generated_at,"scenario generated_at")
        if generated < evaluated: raise ContractViolation("scenario outcome generated before evaluation")
        object.__setattr__(self,"forecast_outcome_ids",ids); object.__setattr__(self,"evidence_origin",origin); object.__setattr__(self,"status",status); object.__setattr__(self,"evidence_grade",grade); object.__setattr__(self,"reason_codes",_reasons(self.reason_codes)); object.__setattr__(self,"evaluated_at",evaluated); object.__setattr__(self,"generated_at",generated)

@dataclass(frozen=True, slots=True)
class LearningRun:
    run_id:str; market:Market; scope_key:str; cutoff_at:datetime; task_kind:str; candidate_set_hash:str; status:LearningRunStatus; cancel_reason:str|None; result_hash:str|None; generated_at:datetime
    def __post_init__(self):
        status=_enum(LearningRunStatus,self.status,"learning run status"); cutoff=ensure_utc(self.cutoff_at,"learning cutoff")
        if not self.scope_key or not self.task_kind or len(self.candidate_set_hash)!=64: raise ContractViolation("invalid learning run")
        expected=stable_hash({"market":self.market,"scope":self.scope_key,"cutoff":cutoff,"task":self.task_kind,"candidates":self.candidate_set_hash})
        if self.run_id!=expected: raise ContractViolation("learning run identity mismatch")
        object.__setattr__(self,"status",status); object.__setattr__(self,"cutoff_at",cutoff); object.__setattr__(self,"generated_at",ensure_utc(self.generated_at,"learning run generated_at"))

@dataclass(frozen=True, slots=True)
class LearningMetricSnapshot:
    """一个不可变账本切片；数值来自成熟 outcome，不保存原始可变聚合对象。"""
    snapshot_id:str; ledger_kind:LedgerKind; scope_key:str; data_cutoff_at:datetime; sample_count:int; metrics:tuple[tuple[str,float|None],...]; generated_at:datetime
    def __post_init__(self):
        kind=_enum(LedgerKind,self.ledger_kind,"metric ledger kind"); cutoff=ensure_utc(self.data_cutoff_at,"metric cutoff"); generated=ensure_utc(self.generated_at,"metric generated")
        values=tuple(sorted((str(key),None if value is None else _finite(value,f"metric {key}")) for key,value in self.metrics))
        if not self.scope_key or self.sample_count<0 or len({key for key,_ in values})!=len(values): raise ContractViolation("invalid learning metric snapshot")
        expected=stable_hash({"ledger":kind,"scope":self.scope_key,"cutoff":cutoff,"sample_count":self.sample_count,"metrics":values})
        if self.snapshot_id!=expected: raise ContractViolation("learning metric snapshot identity mismatch")
        object.__setattr__(self,"ledger_kind",kind); object.__setattr__(self,"data_cutoff_at",cutoff); object.__setattr__(self,"metrics",values); object.__setattr__(self,"generated_at",generated)

@dataclass(frozen=True, slots=True)
class LearningCandidateVersion:
    candidate_id:str; kind:CandidateKind; scope:CandidateScope; scope_key:str; market:Market; profile:str|None; base_version:str; parameter_hash:str; search_space_hash:str; lifecycle:CandidateLifecycle; evidence_origin:EvidenceOrigin; created_at:datetime; generated_at:datetime; reason_codes:tuple[str,...]; projection_key:str=""
    def __post_init__(self):
        kind=_enum(CandidateKind,self.kind,"candidate kind"); scope=_enum(CandidateScope,self.scope,"candidate scope"); lifecycle=_enum(CandidateLifecycle,self.lifecycle,"candidate lifecycle"); origin=_enum(EvidenceOrigin,self.evidence_origin,"candidate origin")
        if not self.scope_key or not self.base_version or not self.projection_key or len(self.parameter_hash)!=64 or len(self.search_space_hash)!=64: raise ContractViolation("invalid learning candidate")
        identity={"kind":kind,"scope":scope,"scope_key":self.scope_key,"market":self.market,"profile":self.profile,"base_version":self.base_version,"parameter_hash":self.parameter_hash,"search_space_hash":self.search_space_hash,"origin":origin,"lifecycle":lifecycle,"projection_key":self.projection_key}
        if self.candidate_id!=stable_hash(identity): raise ContractViolation("learning candidate identity mismatch")
        created=ensure_utc(self.created_at,"candidate created_at"); generated=ensure_utc(self.generated_at,"candidate generated_at")
        if generated < created: raise ContractViolation("candidate generated before creation")
        for key,value in (("kind",kind),("scope",scope),("lifecycle",lifecycle),("evidence_origin",origin),("created_at",created),("generated_at",generated),("reason_codes",_reasons(self.reason_codes))): object.__setattr__(self,key,value)

@dataclass(frozen=True, slots=True)
class PromotionEvent:
    promotion_id:str; candidate_id:str; projection_key:str; decision:PromotionDecision; previous_candidate_id:str|None; evidence_hash:str; decided_at:datetime; generated_at:datetime; reason_codes:tuple[str,...]; evidence_sample_count:int=0; hard_guardrails_ok:bool=False; deployment_candidate_id:str|None=None
    def __post_init__(self):
        decision=_enum(PromotionDecision,self.decision,"promotion decision"); reasons=_reasons(self.reason_codes)
        if decision is PromotionDecision.PROMOTE_TO_CHAMPION and self.deployment_candidate_id != self.candidate_id: raise ContractViolation("champion promotion must deploy the promoted candidate")
        if decision is PromotionDecision.ROLLBACK and (not self.deployment_candidate_id or self.deployment_candidate_id == self.candidate_id): raise ContractViolation("rollback requires a distinct healthy champion target")
        if decision not in {PromotionDecision.PROMOTE_TO_CHAMPION,PromotionDecision.ROLLBACK} and self.deployment_candidate_id is not None: raise ContractViolation("this lifecycle event cannot change deployment target")
        decided=ensure_utc(self.decided_at,"promotion decided_at"); generated=ensure_utc(self.generated_at,"promotion generated_at")
        if generated < decided: raise ContractViolation("promotion generated before decision")
        expected=stable_hash({"candidate":self.candidate_id,"projection":self.projection_key,"decision":decision,"previous":self.previous_candidate_id,"deployment_candidate_id":self.deployment_candidate_id,"evidence":self.evidence_hash,"samples":self.evidence_sample_count,"hard_guardrails_ok":self.hard_guardrails_ok,"decided_at":decided,"reasons":reasons})
        if self.promotion_id!=expected or len(self.evidence_hash)!=64 or self.evidence_sample_count<0: raise ContractViolation("promotion identity mismatch")
        object.__setattr__(self,"decision",decision); object.__setattr__(self,"reason_codes",reasons); object.__setattr__(self,"decided_at",decided); object.__setattr__(self,"generated_at",generated)

@dataclass(frozen=True, slots=True)
class StrategyOutcome:
    strategy_outcome_id:str; plan_id:str; scenario_id:str; decision_id:str; instrument:InstrumentId; action:str; family:str; strategy_id:str; strategy_version:str; parameter_hash:str; profile:str; evidence_origin:EvidenceOrigin; evaluation_horizon:int; target_session_date:date; trigger_state:str; trigger_at:datetime|None; fill_outcome:str; fill_price:Decimal|None; filled_shares:Decimal; gross_return:Decimal|None; net_return:Decimal|None; benchmark_return:Decimal|None; excess_return:Decimal|None; mae:Decimal|None; mfe:Decimal|None; execution_evidence_grade:LearningEvidenceGrade; status:OutcomeStatus; reason_codes:tuple[str,...]; generated_at:datetime
    # 以下字段在早期兼容记录中允许为空；新产生的结果必须由同一条 V2-7 路径填入。
    valid_from:datetime|None=None; expires_at:datetime|None=None; exit_type:str|None=None; exit_at:datetime|None=None; exit_price:Decimal|None=None; holding_sessions:int|None=None; commission:Decimal|None=None; tax:Decimal|None=None; slippage:Decimal|None=None; exit_avoided_loss:Decimal|None=None; exit_opportunity_cost:Decimal|None=None; exit_quality:Decimal|None=None; entry_fill_id:str|None=None; exit_fill_id:str|None=None; market_regime_key:str|None=None; evaluated_at:datetime|None=None
    def __post_init__(self):
        origin=_enum(EvidenceOrigin,self.evidence_origin,"strategy outcome origin"); status=_enum(OutcomeStatus,self.status,"strategy outcome status"); grade=_enum(LearningEvidenceGrade,self.execution_evidence_grade,"execution evidence")
        if self.evaluation_horizon not in {1,3,5,10} or self.filled_shares<0: raise ContractViolation("invalid strategy outcome")
        if self.trigger_state=="not_triggered" and (self.fill_outcome!="not_applicable" or self.net_return is not None): raise ContractViolation("untriggered plan cannot be a trade")
        if self.fill_outcome=="rejected" and self.net_return is not None: raise ContractViolation("rejected order is not strategy loss")
        reasons=_reasons(self.reason_codes)
        identity={"plan":self.plan_id,"decision":self.decision_id,"origin":origin,"horizon":self.evaluation_horizon,"target":self.target_session_date,"trigger":self.trigger_state,"fill":self.fill_outcome,"entry_fill_id":self.entry_fill_id,"exit_fill_id":self.exit_fill_id,"exit_type":self.exit_type,"exit_at":self.exit_at,"exit_price":self.exit_price,"gross":self.gross_return,"net":self.net_return,"mae":self.mae,"mfe":self.mfe,"reasons":reasons}
        if self.strategy_outcome_id!=stable_hash(identity): raise ContractViolation("strategy outcome identity mismatch")
        if self.valid_from is not None: object.__setattr__(self,"valid_from",ensure_utc(self.valid_from,"strategy valid_from"))
        if self.expires_at is not None: object.__setattr__(self,"expires_at",ensure_utc(self.expires_at,"strategy expires_at"))
        if self.valid_from and self.expires_at and self.expires_at < self.valid_from: raise ContractViolation("strategy validity window is reversed")
        if self.exit_at is not None: object.__setattr__(self,"exit_at",ensure_utc(self.exit_at,"strategy exit_at"))
        if self.trigger_at is not None: object.__setattr__(self,"trigger_at",ensure_utc(self.trigger_at,"strategy trigger_at"))
        for field in ("exit_price","commission","tax","slippage","exit_avoided_loss","exit_opportunity_cost","exit_quality"):
            value=getattr(self,field)
            if value is not None: object.__setattr__(self,field,as_decimal(value,field.replace("_"," ")))
        if self.holding_sessions is not None and self.holding_sessions < 0: raise ContractViolation("holding sessions must be non-negative")
        evaluated=ensure_utc(self.evaluated_at or self.generated_at,"strategy evaluated_at"); generated=ensure_utc(self.generated_at,"strategy outcome generated_at")
        if generated < evaluated: raise ContractViolation("strategy outcome generated before evaluation")
        object.__setattr__(self,"evidence_origin",origin); object.__setattr__(self,"status",status); object.__setattr__(self,"execution_evidence_grade",grade); object.__setattr__(self,"reason_codes",reasons); object.__setattr__(self,"evaluated_at",evaluated); object.__setattr__(self,"generated_at",generated)

@dataclass(frozen=True, slots=True)
class JointOutcome:
    joint_outcome_id:str; outcome_kind:JointOutcomeKind; portfolio_bundle_id:str|None; profile:str|None; batch_id:str|None; account_hash:str; valuation_id:str|None; market:Market; currency:str; ordered_allocation_ids:tuple[str,...]; intent_ids:tuple[str,...]; execution_run_ids:tuple[str,...]; evidence_origin:EvidenceOrigin; starting_equity:Decimal; ending_equity:Decimal; net_cash_flow:Decimal; time_weighted_return:Decimal; benchmark_return:Decimal|None; alpha:Decimal|None; max_drawdown:Decimal; volatility:Decimal|None; sharpe:Decimal|None; calmar:Decimal|None; realized_friction:Decimal; planned_loss:Decimal|None; realized_loss:Decimal|None; entry_count:int; exit_count:int; rejected_count:int; status:OutcomeStatus; evidence_grade:LearningEvidenceGrade; reason_codes:tuple[str,...]; generated_at:datetime
    replay_window:tuple[date,date]|None=None; risk_contribution:Decimal|None=None; execution_contribution:Decimal|None=None; portfolio_contribution:Decimal|None=None
    def __post_init__(self):
        kind=_enum(JointOutcomeKind,self.outcome_kind,"joint outcome kind"); origin=_enum(EvidenceOrigin,self.evidence_origin,"joint origin"); status=_enum(OutcomeStatus,self.status,"joint status"); grade=_enum(LearningEvidenceGrade,self.evidence_grade,"joint evidence")
        if self.starting_equity<=0 or self.ending_equity<0 or self.currency != ("CNY" if self.market is Market.A else "USD") or (self.alpha is not None and self.benchmark_return is None): raise ContractViolation("invalid joint outcome")
        allocations=tuple(self.ordered_allocation_ids); intents=tuple(self.intent_ids); runs=tuple(self.execution_run_ids); reasons=_reasons(self.reason_codes)
        if len(set(allocations))!=len(allocations) or len(set(intents))!=len(intents) or len(set(runs))!=len(runs):
            raise ContractViolation("joint outcome contains duplicate references")
        identity={"kind":kind,"bundle":self.portfolio_bundle_id,"profile":self.profile,"batch":self.batch_id,"account":self.account_hash,"valuation":self.valuation_id,"market":self.market,"currency":self.currency,"allocations":allocations,"intents":intents,"runs":runs,"origin":origin,"start":self.starting_equity,"end":self.ending_equity,"cash_flow":self.net_cash_flow,"twr":self.time_weighted_return,"benchmark":self.benchmark_return,"alpha":self.alpha,"drawdown":self.max_drawdown,"volatility":self.volatility,"sharpe":self.sharpe,"calmar":self.calmar,"friction":self.realized_friction,"planned_loss":self.planned_loss,"realized_loss":self.realized_loss,"counts":(self.entry_count,self.exit_count,self.rejected_count),"window":self.replay_window,"status":status,"grade":grade,"reasons":reasons}
        if self.joint_outcome_id!=stable_hash(identity): raise ContractViolation("joint outcome identity mismatch")
        generated=ensure_utc(self.generated_at,"joint outcome generated_at")
        if self.replay_window is not None and (self.replay_window[1] < self.replay_window[0] or generated.date() < self.replay_window[1]): raise ContractViolation("joint replay window is reversed or not yet observable")
        for field in ("risk_contribution","execution_contribution","portfolio_contribution"):
            value=getattr(self,field)
            if value is not None: object.__setattr__(self,field,as_decimal(value,field.replace("_"," ")))
        object.__setattr__(self,"ordered_allocation_ids",allocations); object.__setattr__(self,"intent_ids",intents); object.__setattr__(self,"execution_run_ids",runs); object.__setattr__(self,"outcome_kind",kind); object.__setattr__(self,"evidence_origin",origin); object.__setattr__(self,"status",status); object.__setattr__(self,"evidence_grade",grade); object.__setattr__(self,"reason_codes",reasons); object.__setattr__(self,"generated_at",generated)
