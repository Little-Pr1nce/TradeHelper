"""V2-8 组合决策层的不可变合同。

本层只在同一冻结账户/市场批次中排序和缩小 V2-6 的批准股数；绝不生成
新信号、默认本金或可执行券商订单。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from .account import AccountSnapshot, as_decimal
from .enums import DecisionMode, Market
from .market_data import ContractViolation, InstrumentId, ensure_utc, stable_hash
from .risk import ExecutionDecision, FrozenAccountValuation, MarketRuleSet, PlanEvidenceSnapshot, RiskDecisionBundle, RiskPolicy, RiskProfile
from .strategy import PlanAction, QuantityIntent, TradePlan
from .scenario import TradingScenario


class _StringEnum(str, Enum):
    def __str__(self): return self.value


class PortfolioRole(_StringEnum): HOLDING = "holding"; WATCHLIST = "watchlist"
class HoldingRiskStatus(_StringEnum): QUANTIFIED = "quantified"; UNQUANTIFIED = "unquantified"; BREACHED = "breached"
class CorrelationStatus(_StringEnum): COMPLETE = "complete"; PARTIAL = "partial"; UNAVAILABLE = "unavailable"
class AllocationStatus(_StringEnum): ALLOCATED_NOW = "allocated_now"; RESERVED_CONDITIONAL = "reserved_conditional"; SHARED_EXIT_RESERVATION = "shared_exit_reservation"; MONITOR_ONLY = "monitor_only"; BLOCKED = "blocked"; NO_ORDER = "no_order"
class ReplacementStatus(_StringEnum): RESEARCH_AFTER_EXIT = "research_after_exit"; WATCH_ONLY = "watch_only"; REJECTED = "rejected"
class PortfolioEvidenceGrade(_StringEnum): HIGH = "high"; MEDIUM = "medium"; LOW = "low"; INSUFFICIENT = "insufficient"
class PortfolioHeatStatus(_StringEnum): COMPLETE = "complete"; INCOMPLETE = "incomplete"; BREACHED = "breached"


PORTFOLIO_REASON_CODES = frozenset("""
PORTFOLIO_ALLOCATED PORTFOLIO_CONDITIONAL_RESERVATION PORTFOLIO_PROTECTIVE_EXIT_PRIORITY
PORTFOLIO_EXIT_RESERVATION_SHARED PORTFOLIO_EXIT_STATE_RECHECK_REQUIRED PORTFOLIO_NO_ORDER_UPSTREAM
PORTFOLIO_NOT_SELECTED PORTFOLIO_DUPLICATE_ENTRY_SUPPRESSED PORTFOLIO_CASH_LIMITED
PORTFOLIO_SINGLE_POSITION_LIMITED PORTFOLIO_TOTAL_EXPOSURE_LIMITED PORTFOLIO_HEAT_LIMITED
PORTFOLIO_HEAT_EXHAUSTED PORTFOLIO_HOLDING_RISK_UNKNOWN PORTFOLIO_STOP_ALREADY_BREACHED
PORTFOLIO_HIGH_CORRELATION_LIMITED PORTFOLIO_CORRELATION_EVIDENCE_MISSING
PORTFOLIO_CORRELATION_MULTIPLIER_APPLIED PORTFOLIO_LOT_ROUNDED PORTFOLIO_ZERO_CAPACITY
PORTFOLIO_INCOMPLETE_VALUATION PORTFOLIO_EQUITY_ZERO PORTFOLIO_PROFILE_SEPARATED
PORTFOLIO_EXIT_PROCEEDS_NOT_REUSED PORTFOLIO_REPLACEMENT_RESEARCH_ONLY
PORTFOLIO_REPLACEMENT_SOURCE_EXIT_REQUIRED PORTFOLIO_REPLACEMENT_TARGET_NOT_QUALIFIED
PORTFOLIO_HHI_WARNING PORTFOLIO_VOLATILITY_UNAVAILABLE PORTFOLIO_EVIDENCE_HIGH
PORTFOLIO_EVIDENCE_MEDIUM PORTFOLIO_EVIDENCE_LOW PORTFOLIO_EVIDENCE_INSUFFICIENT
PORTFOLIO_HARD_CONSTRAINT_IMMUTABLE
""".split())


def _enum(kind, value, field):
    try: return value if isinstance(value, kind) else kind(str(value))
    except ValueError as exc: raise ContractViolation(f"unsupported {field}: {value}") from exc

def _money(value, field, *, positive=False, nonnegative=False):
    result=as_decimal(value, field)
    if positive and result <= 0: raise ContractViolation(f"{field} must be positive")
    if nonnegative and result < 0: raise ContractViolation(f"{field} cannot be negative")
    return result

def _reasons(values):
    result=tuple(sorted(set(values)))
    if any(item not in PORTFOLIO_REASON_CODES for item in result): raise ContractViolation("unknown portfolio reason code")
    return result


@dataclass(frozen=True, slots=True)
class PortfolioPolicy:
    policy_version: str = "portfolio_policy_v1"; conservative_heat_cap: Decimal = Decimal("0.04"); aggressive_heat_cap: Decimal = Decimal("0.06"); absolute_heat_hard_cap: Decimal = Decimal("0.08"); high_correlation_threshold: Decimal = Decimal("0.75"); high_correlation_group_cap: Decimal = Decimal("0.35"); hhi_warning: Decimal = Decimal("0.25"); correlation_lookback_sessions: int = 90; minimum_correlation_samples: int = 20; unknown_correlation_multiplier: Decimal = Decimal("0.50"); annualization_sessions: int = 252; allocation_method: str = "lexicographic_waterfall_v1"; hard_constraint_version: str = "portfolio_hard_constraints_v1"; parameter_hash: str = ""
    def __post_init__(self):
        for name in ("conservative_heat_cap","aggressive_heat_cap","absolute_heat_hard_cap","high_correlation_threshold","high_correlation_group_cap","hhi_warning","unknown_correlation_multiplier"):
            object.__setattr__(self,name,_money(getattr(self,name),name,positive=True))
        if (not self.policy_version or self.hard_constraint_version!="portfolio_hard_constraints_v1" or self.allocation_method!="lexicographic_waterfall_v1" or self.conservative_heat_cap!=Decimal("0.04") or self.aggressive_heat_cap!=Decimal("0.06") or self.absolute_heat_hard_cap!=Decimal("0.08") or self.high_correlation_threshold!=Decimal("0.75") or self.high_correlation_group_cap!=Decimal("0.35") or self.unknown_correlation_multiplier!=Decimal("0.50") or self.correlation_lookback_sessions!=90 or self.minimum_correlation_samples!=20 or self.annualization_sessions!=252): raise ContractViolation("portfolio hard policy is immutable")
        expected=stable_hash({name:getattr(self,name) for name in self.__dataclass_fields__ if name!="parameter_hash"})
        if self.parameter_hash and self.parameter_hash!=expected: raise ContractViolation("portfolio policy hash mismatch")
        object.__setattr__(self,"parameter_hash",expected)


@dataclass(frozen=True, slots=True)
class PortfolioCandidate:
    candidate_id: str; role: PortfolioRole; trading_scenario: TradingScenario; trade_plan: TradePlan; execution_decision: ExecutionDecision; plan_evidence: PlanEvidenceSnapshot | None; market_rules: MarketRuleSet; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        role=_enum(PortfolioRole,self.role,"portfolio role"); scenario, plan, decision=self.trading_scenario,self.trade_plan,self.execution_decision
        generated=ensure_utc(self.generated_at,"candidate generated_at")
        if (self.schema_version!=1 or scenario.instrument!=plan.instrument or plan.instrument!=decision.instrument or scenario.scenario_id!=plan.scenario_id or plan.plan_id!=decision.plan_id or plan.action is not decision.action or plan.quantity_intent is not decision.quantity_intent or self.market_rules.market is not plan.instrument.market or self.market_rules.rule_version!=decision.market_rule_version or generated<scenario.as_of or generated<decision.generated_at): raise ContractViolation("portfolio candidate upstream identity mismatch")
        evidence=self.plan_evidence
        if evidence and (evidence.instrument!=plan.instrument or evidence.strategy_id!=plan.strategy_id or evidence.strategy_version!=plan.strategy_version or evidence.parameter_hash!=plan.parameter_hash or (evidence.profile is not None and evidence.profile is not decision.profile)): raise ContractViolation("portfolio evidence does not match plan")
        expected=stable_hash({"role":role,"scenario_id":scenario.scenario_id,"plan_id":plan.plan_id,"decision_id":decision.decision_id,"evidence_id":evidence.evidence_id if evidence else None,"rule_version":self.market_rules.rule_version})
        if self.candidate_id!=expected: raise ContractViolation("portfolio candidate identity mismatch")
        if evidence and generated<evidence.generated_at: raise ContractViolation("portfolio candidate predates evidence")
        object.__setattr__(self,"role",role); object.__setattr__(self,"generated_at",generated)


@dataclass(frozen=True, slots=True)
class HoldingRiskSnapshot:
    holding_risk_id: str; instrument: InstrumentId; shares: Decimal; reference_price: Decimal | None; market_value: Decimal | None; stop_price: Decimal | None; exit_friction_reserve: Decimal; planned_loss_amount: Decimal | None; status: HoldingRiskStatus; source_plan_id: str | None; source_decision_id: str | None; captured_at: datetime; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        shares=_money(self.shares,"holding shares",positive=True); price=None if self.reference_price is None else _money(self.reference_price,"holding reference price",positive=True); value=None if self.market_value is None else _money(self.market_value,"holding value",positive=True); reserve=_money(self.exit_friction_reserve,"exit friction",nonnegative=True); status=_enum(HoldingRiskStatus,self.status,"holding risk status"); stop=None if self.stop_price is None else _money(self.stop_price,"stop price",positive=True)
        expected_loss=None if price is None or stop is None or stop>=price else (price-stop)*shares+reserve
        captured=ensure_utc(self.captured_at,"holding captured_at"); generated=ensure_utc(self.generated_at,"holding generated_at")
        if self.schema_version!=1 or generated<captured or (price is None)!=(value is None) or (price is not None and value!=shares*price) or ((status is HoldingRiskStatus.QUANTIFIED) != (expected_loss is not None)) or (status is HoldingRiskStatus.BREACHED and not(price is not None and stop is not None and stop>=price)) or (status is HoldingRiskStatus.UNQUANTIFIED and (stop is not None or self.planned_loss_amount is not None)) or self.planned_loss_amount!=expected_loss: raise ContractViolation("holding risk values are inconsistent")
        expected=stable_hash({"instrument":self.instrument,"shares":shares,"reference_price":price,"market_value":value,"stop_price":stop,"exit_friction_reserve":reserve,"status":status,"source_plan_id":self.source_plan_id,"source_decision_id":self.source_decision_id,"captured_at":captured})
        if self.holding_risk_id!=expected: raise ContractViolation("holding risk identity mismatch")
        for key,value in (("shares",shares),("reference_price",price),("market_value",value),("exit_friction_reserve",reserve),("stop_price",stop),("status",status),("captured_at",captured),("generated_at",generated)): object.__setattr__(self,key,value)


@dataclass(frozen=True, slots=True)
class InstrumentReturnRisk:
    instrument: InstrumentId; sample_count: int; start_session_date: date | None; end_session_date: date | None; annualized_volatility: Decimal | None; adjustment_mode: str; source_bar_hash: str
    def __post_init__(self):
        if self.sample_count < 0 or not self.adjustment_mode or not self.source_bar_hash: raise ContractViolation("invalid instrument return risk")
        volatility=None if self.annualized_volatility is None else _money(self.annualized_volatility,"annualized volatility",nonnegative=True)
        if (self.sample_count < 2) != (volatility is None): raise ContractViolation("return risk sample/volatility mismatch")
        object.__setattr__(self,"annualized_volatility",volatility)


@dataclass(frozen=True, slots=True)
class CorrelationPair:
    left: InstrumentId; right: InstrumentId; coefficient: Decimal | None; overlapping_samples: int; status: CorrelationStatus
    def __post_init__(self):
        status=_enum(CorrelationStatus,self.status,"correlation status"); coefficient=None if self.coefficient is None else _money(self.coefficient,"correlation coefficient")
        if self.left==self.right or self.left.stable_key>self.right.stable_key or self.overlapping_samples<0 or (status is CorrelationStatus.COMPLETE) != (coefficient is not None) or (coefficient is not None and not Decimal("-1")<=coefficient<=Decimal("1")): raise ContractViolation("invalid correlation pair")
        object.__setattr__(self,"status",status); object.__setattr__(self,"coefficient",coefficient)


@dataclass(frozen=True, slots=True)
class PortfolioCorrelationSnapshot:
    correlation_snapshot_id: str; market: Market; universe: tuple[InstrumentId,...]; instrument_risks: tuple[InstrumentReturnRisk,...]; pairs: tuple[CorrelationPair,...]; lookback_sessions: int; minimum_samples: int; return_method: str; annualization_sessions: int; cutoff_at: datetime; status: CorrelationStatus; source_batch_hash: str; generated_at: datetime
    def __post_init__(self):
        market=self.market if isinstance(self.market,Market) else Market(str(self.market).upper()); raw_universe=tuple(self.universe); universe=tuple(sorted(set(raw_universe),key=lambda x:x.stable_key)); risks=tuple(sorted(self.instrument_risks,key=lambda x:x.instrument.stable_key)); pairs=tuple(sorted(self.pairs,key=lambda x:(x.left.stable_key,x.right.stable_key))); status=_enum(CorrelationStatus,self.status,"correlation snapshot status"); cutoff=ensure_utc(self.cutoff_at,"correlation cutoff"); generated=ensure_utc(self.generated_at,"correlation generated_at")
        expected_pair_count=len(universe)*(len(universe)-1)//2
        completed_instruments=sum(item.sample_count>=self.minimum_samples for item in risks)
        expected_status=(CorrelationStatus.UNAVAILABLE if not universe or completed_instruments==0 else CorrelationStatus.COMPLETE if completed_instruments==len(universe) and all(item.status is CorrelationStatus.COMPLETE for item in pairs) and len(pairs)==expected_pair_count else CorrelationStatus.PARTIAL)
        if (len(raw_universe)!=len(universe) or len({item.instrument for item in risks})!=len(risks) or generated<cutoff or any(item.market is not market for item in universe) or {item.instrument for item in risks} != set(universe) or len(pairs)!=expected_pair_count or len({(item.left,item.right) for item in pairs})!=len(pairs) or any(item.left not in universe or item.right not in universe or item.status is CorrelationStatus.PARTIAL for item in pairs) or status is not expected_status or self.lookback_sessions!=90 or self.minimum_samples!=20 or self.return_method!="simple_daily_close_return_v1" or self.annualization_sessions!=252): raise ContractViolation("correlation snapshot universe mismatch")
        expected=stable_hash({"market":market,"universe":universe,"instrument_risks":risks,"pairs":pairs,"lookback":self.lookback_sessions,"minimum":self.minimum_samples,"method":self.return_method,"annualization":self.annualization_sessions,"cutoff_at":cutoff,"status":status,"source_batch_hash":self.source_batch_hash})
        if self.correlation_snapshot_id!=expected: raise ContractViolation("correlation snapshot identity mismatch")
        for key,value in (("market",market),("universe",universe),("instrument_risks",risks),("pairs",pairs),("status",status),("cutoff_at",cutoff),("generated_at",generated)): object.__setattr__(self,key,value)


@dataclass(frozen=True, slots=True)
class PortfolioRiskSnapshot:
    risk_snapshot_id: str; market: Market; valuation_id: str; equity: Decimal; cash: Decimal; invested_value: Decimal; invested_pct: Decimal; weights_by_instrument: tuple[tuple[InstrumentId,Decimal],...]; max_position_instrument: InstrumentId | None; max_position_pct: Decimal; hhi: Decimal; portfolio_annualized_volatility: Decimal | None; planned_loss_amount: Decimal | None; planned_loss_pct: Decimal | None; high_correlation_pairs: tuple[CorrelationPair,...]; heat_status: PortfolioHeatStatus; evidence_grade: PortfolioEvidenceGrade; reason_codes: tuple[str,...]; calculated_at: datetime
    def __post_init__(self):
        market=self.market if isinstance(self.market,Market) else Market(str(self.market).upper()); equity=_money(self.equity,"portfolio equity",nonnegative=True); cash=_money(self.cash,"portfolio cash",nonnegative=True); invested=_money(self.invested_value,"invested value",nonnegative=True); pct=_money(self.invested_pct,"invested pct",nonnegative=True); grade=_enum(PortfolioEvidenceGrade,self.evidence_grade,"portfolio evidence grade"); heat=_enum(PortfolioHeatStatus,self.heat_status,"heat status")
        expected_pct=invested/equity if equity else Decimal("0")
        if invested>equity or pct!=expected_pct or (equity>0 and cash+invested!=equity): raise ContractViolation("portfolio valuation totals invalid")
        weights=tuple(sorted(((instrument,_money(weight,"weight",nonnegative=True)) for instrument,weight in self.weights_by_instrument),key=lambda item:item[0].stable_key))
        if len({item[0] for item in weights})!=len(weights) or any(item[0].market is not market for item in weights) or sum((item[1] for item in weights),Decimal("0"))!=pct: raise ContractViolation("portfolio weights invalid")
        loss=None if self.planned_loss_amount is None else _money(self.planned_loss_amount,"planned loss",nonnegative=True); loss_pct=None if self.planned_loss_pct is None else _money(self.planned_loss_pct,"planned loss pct",nonnegative=True)
        if (loss is None)!=(loss_pct is None) or (equity==0 and loss is not None) or (loss is not None and loss_pct!=loss/equity): raise ContractViolation("portfolio loss totals invalid")
        max_pct=_money(self.max_position_pct,"max position pct",nonnegative=True); hhi=_money(self.hhi,"hhi",nonnegative=True); volatility=None if self.portfolio_annualized_volatility is None else _money(self.portfolio_annualized_volatility,"portfolio volatility",nonnegative=True); high_pairs=tuple(sorted(self.high_correlation_pairs,key=lambda item:(item.left.stable_key,item.right.stable_key))); reasons=_reasons(self.reason_codes)
        expected_max_instrument,expected_max_pct=max(weights,key=lambda item:item[1],default=(None,Decimal("0")))
        if self.max_position_instrument!=expected_max_instrument or max_pct!=expected_max_pct or hhi!=sum((weight*weight for _,weight in weights),Decimal("0")): raise ContractViolation("portfolio concentration metrics invalid")
        expected=stable_hash({"market":market,"valuation_id":self.valuation_id,"equity":equity,"cash":cash,"invested":invested,"invested_pct":pct,"weights":weights,"max_position_instrument":self.max_position_instrument,"max_position_pct":max_pct,"hhi":hhi,"volatility":volatility,"loss":loss,"loss_pct":loss_pct,"high_pairs":high_pairs,"heat":heat,"grade":grade,"reasons":reasons,"calculated_at":ensure_utc(self.calculated_at,"risk calculated_at")})
        if self.risk_snapshot_id!=expected: raise ContractViolation("portfolio risk snapshot identity mismatch")
        for key,value in (("market",market),("equity",equity),("cash",cash),("invested_value",invested),("invested_pct",pct),("weights_by_instrument",weights),("max_position_pct",max_pct),("hhi",hhi),("portfolio_annualized_volatility",volatility),("high_correlation_pairs",high_pairs),("planned_loss_amount",loss),("planned_loss_pct",loss_pct),("heat_status",heat),("evidence_grade",grade),("reason_codes",reasons),("calculated_at",ensure_utc(self.calculated_at,"risk calculated_at"))): object.__setattr__(self,key,value)


@dataclass(frozen=True, slots=True)
class PortfolioInputBatch:
    batch_id: str; market: Market; currency: str; mode: DecisionMode; account_snapshot: AccountSnapshot; valuation: FrozenAccountValuation; risk_policy: RiskPolicy; portfolio_policy: PortfolioPolicy; risk_bundles: tuple[RiskDecisionBundle,...]; candidates: tuple[PortfolioCandidate,...]; watchlist: tuple[InstrumentId,...]; holding_risks: tuple[HoldingRiskSnapshot,...]; correlation_snapshot: PortfolioCorrelationSnapshot; as_of: datetime; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        market=self.market if isinstance(self.market,Market) else Market(str(self.market).upper()); mode=_enum(DecisionMode,self.mode,"batch mode"); at=ensure_utc(self.as_of,"batch as_of"); generated=ensure_utc(self.generated_at,"batch generated_at"); account=self.account_snapshot; valuation=self.valuation; bundles=tuple(sorted(self.risk_bundles,key=lambda x:x.risk_bundle_id)); candidates=tuple(sorted(self.candidates,key=lambda x:x.candidate_id)); raw_watchlist=tuple(self.watchlist); watchlist=tuple(sorted(set(raw_watchlist),key=lambda x:x.stable_key)); positions={item.instrument for item in account.positions}; risks=tuple(sorted(self.holding_risks,key=lambda x:x.instrument.stable_key))
        candidate_decisions = [item.execution_decision.decision_id for item in candidates]
        bundle_decisions = [decision.decision_id for bundle in bundles for decision in bundle.decisions]
        if (self.schema_version!=1 or len(raw_watchlist)!=len(watchlist) or len({item.risk_bundle_id for item in bundles})!=len(bundles) or len(set(candidate_decisions))!=len(candidate_decisions) or len(set(bundle_decisions))!=len(bundle_decisions) or account.market is not market or valuation.market is not market or account.currency!=self.currency or valuation.currency!=self.currency or valuation.account_hash!=stable_hash(account) or any(item.market is not market for item in watchlist) or positions & set(watchlist) or {item.instrument for item in risks}!=positions or self.correlation_snapshot.market is not market or set(self.correlation_snapshot.universe)!=positions|set(watchlist) or sorted(candidate_decisions)!=sorted(bundle_decisions)): raise ContractViolation("portfolio input batch facts are inconsistent")
        if any(item.trade_plan.instrument.market is not market or item.market_rules.exchange is not item.trade_plan.instrument.exchange or not(item.market_rules.effective_from<=at and (item.market_rules.effective_to is None or at<item.market_rules.effective_to)) or item.trading_scenario.mode is not mode or item.trading_scenario.as_of!=at or item.generated_at < item.trading_scenario.as_of or (item.role is PortfolioRole.HOLDING and item.trade_plan.instrument not in positions) or (item.role is PortfolioRole.WATCHLIST and item.trade_plan.instrument not in watchlist) for item in candidates): raise ContractViolation("portfolio candidate role or timestamp mismatch")
        if account.captured_at>at or valuation.valuation_at>at or any(item.captured_at>at for item in risks) or self.correlation_snapshot.cutoff_at>at: raise ContractViolation("portfolio batch contains future facts")
        source_generated=(valuation.generated_at, self.correlation_snapshot.generated_at, *(item.generated_at for item in bundles), *(item.generated_at for item in candidates), *(item.generated_at for item in risks))
        if generated<at or any(generated<item for item in source_generated): raise ContractViolation("portfolio batch predates source evidence")
        if any(item.plan_evidence and (item.plan_evidence.data_cutoff_at>at or item.plan_evidence.evaluated_at>at) for item in candidates): raise ContractViolation("portfolio evidence is from the future")
        if any(bundle.instrument.market is not market or bundle.account_hash!=valuation.account_hash or bundle.valuation_id!=valuation.valuation_id or bundle.risk_policy_version!=self.risk_policy.policy_version for bundle in bundles): raise ContractViolation("portfolio risk bundles do not share account/policy")
        expected=stable_hash({"market":market,"currency":account.currency,"mode":mode,"account_hash":stable_hash(account),"valuation_id":valuation.valuation_id,"risk_policy":self.risk_policy.parameter_hash,"portfolio_policy":self.portfolio_policy.parameter_hash,"bundle_ids":tuple(x.risk_bundle_id for x in bundles),"candidate_ids":tuple(x.candidate_id for x in candidates),"watchlist":watchlist,"holding_risk_ids":tuple(x.holding_risk_id for x in risks),"correlation_snapshot_id":self.correlation_snapshot.correlation_snapshot_id,"as_of":at})
        if self.batch_id!=expected: raise ContractViolation("portfolio input batch identity mismatch")
        for key,value in (("market",market),("currency",account.currency),("mode",mode),("risk_bundles",bundles),("candidates",candidates),("watchlist",watchlist),("holding_risks",risks),("as_of",at),("generated_at",generated)): object.__setattr__(self,key,value)


@dataclass(frozen=True, slots=True)
class PortfolioAllocation:
    allocation_id: str; batch_id: str; profile: RiskProfile; candidate_id: str; instrument: InstrumentId; plan_id: str; decision_id: str; action: PlanAction; level: str; status: AllocationStatus; rank: int | None; rank_components: tuple[tuple[str,str],...]; approved_shares: Decimal; final_requested_shares: Decimal; current_position_value: Decimal | None; reference_entry_price: Decimal | None; reserved_cash: Decimal; reserved_incremental_loss: Decimal; estimated_position_pct: Decimal | None; reservation_group_id: str | None; binding_constraints: tuple[str,...]; reason_codes: tuple[str,...]; generated_at: datetime
    def __post_init__(self):
        profile=_enum(RiskProfile,self.profile,"allocation profile"); action=_enum(PlanAction,self.action,"allocation action"); status=_enum(AllocationStatus,self.status,"allocation status"); approved=_money(self.approved_shares,"approved shares",nonnegative=True); final=_money(self.final_requested_shares,"final requested shares",nonnegative=True); reserved_cash=_money(self.reserved_cash,"reserved cash",nonnegative=True); reserved_loss=_money(self.reserved_incremental_loss,"reserved loss",nonnegative=True); current_value=None if self.current_position_value is None else _money(self.current_position_value,"current position value",nonnegative=True); entry=None if self.reference_entry_price is None else _money(self.reference_entry_price,"reference entry price",positive=True); estimated_pct=None if self.estimated_position_pct is None else _money(self.estimated_position_pct,"estimated position pct",nonnegative=True); constraints=tuple(sorted(set(self.binding_constraints))); reasons=_reasons(self.reason_codes); components=tuple((str(key),str(value)) for key,value in self.rank_components)
        active={AllocationStatus.ALLOCATED_NOW,AllocationStatus.RESERVED_CONDITIONAL,AllocationStatus.SHARED_EXIT_RESERVATION}
        if final>approved or ((status in {AllocationStatus.BLOCKED,AllocationStatus.MONITOR_ONLY,AllocationStatus.NO_ORDER}) and (final!=0 or reserved_cash!=0 or reserved_loss!=0)) or (status in active and final<=0): raise ContractViolation("portfolio allocation capacity/status mismatch")
        if (status is AllocationStatus.ALLOCATED_NOW and (self.level not in {"A", "B"} or action not in {PlanAction.BUY,PlanAction.ADD,PlanAction.SELL,PlanAction.REDUCE})) or (status is AllocationStatus.RESERVED_CONDITIONAL and (self.level not in {"A", "B"} or action not in {PlanAction.BUY,PlanAction.ADD,PlanAction.SELL,PlanAction.REDUCE})) or (status is AllocationStatus.SHARED_EXIT_RESERVATION and (action not in {PlanAction.SELL, PlanAction.REDUCE} or self.level not in {"A", "B"} or not self.reservation_group_id)):
            raise ContractViolation("portfolio allocation status/action mismatch")
        # group_id 由本 allocation 的稳定 ID 集合生成；把它反向放进 allocation
        # identity 会形成循环，故 allocation identity 不包含 group_id。
        expected=stable_hash({"batch_id":self.batch_id,"profile":profile,"candidate_id":self.candidate_id,"instrument":self.instrument,"plan_id":self.plan_id,"decision_id":self.decision_id,"action":action,"level":self.level,"status":status,"rank":self.rank,"rank_components":components,"approved":approved,"final":final,"current_value":current_value,"entry":entry,"reserved_cash":reserved_cash,"reserved_loss":reserved_loss,"estimated_pct":estimated_pct,"constraints":constraints,"reasons":reasons})
        if self.allocation_id!=expected: raise ContractViolation("portfolio allocation identity mismatch")
        for key,value in (("profile",profile),("action",action),("status",status),("rank_components",components),("approved_shares",approved),("final_requested_shares",final),("current_position_value",current_value),("reference_entry_price",entry),("reserved_cash",reserved_cash),("reserved_incremental_loss",reserved_loss),("estimated_position_pct",estimated_pct),("binding_constraints",constraints),("reason_codes",reasons),("generated_at",ensure_utc(self.generated_at,"allocation generated_at"))): object.__setattr__(self,key,value)


@dataclass(frozen=True, slots=True)
class PortfolioReservationGroup:
    group_id: str; batch_id: str; profile: RiskProfile; instrument: InstrumentId; side: str; member_allocation_ids: tuple[str,...]; max_aggregate_shares: Decimal; consumption_policy: str; reason_codes: tuple[str,...]; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        profile=_enum(RiskProfile,self.profile,"reservation profile"); maximum=_money(self.max_aggregate_shares,"reservation shares",positive=True); members=tuple(sorted(set(self.member_allocation_ids)))
        if self.schema_version!=1 or self.side!="sell" or len(members)<2 or self.consumption_policy!="first_fill_consumes_then_recheck_v1": raise ContractViolation("invalid exit reservation group")
        expected=stable_hash({"batch_id":self.batch_id,"profile":profile,"instrument":self.instrument,"side":self.side,"members":members,"maximum":maximum,"policy":self.consumption_policy,"reasons":_reasons(self.reason_codes)})
        if self.group_id!=expected: raise ContractViolation("reservation group identity mismatch")
        object.__setattr__(self,"profile",profile); object.__setattr__(self,"member_allocation_ids",members); object.__setattr__(self,"max_aggregate_shares",maximum); object.__setattr__(self,"reason_codes",_reasons(self.reason_codes)); object.__setattr__(self,"generated_at",ensure_utc(self.generated_at,"group generated_at"))


@dataclass(frozen=True, slots=True)
class PortfolioReservationSnapshot:
    profile: RiskProfile; frozen_equity: Decimal; frozen_cash: Decimal; deployable_cash: Decimal; reserved_entry_cash: Decimal; remaining_cash: Decimal; reserved_entry_notional: Decimal; projected_invested_pct_at_reference_price: Decimal; current_planned_loss: Decimal | None; reserved_incremental_loss: Decimal; projected_heat_pct: Decimal | None; exit_release_estimate: Decimal; evidence_grade: PortfolioEvidenceGrade; reason_codes: tuple[str,...]
    def __post_init__(self):
        profile=_enum(RiskProfile,self.profile,"reservation profile"); grade=_enum(PortfolioEvidenceGrade,self.evidence_grade,"reservation grade")
        for name in ("frozen_equity","frozen_cash","deployable_cash","reserved_entry_cash","remaining_cash","reserved_entry_notional","projected_invested_pct_at_reference_price","reserved_incremental_loss","exit_release_estimate"):
            object.__setattr__(self,name,_money(getattr(self,name),name,nonnegative=True))
        if self.remaining_cash!=self.deployable_cash-self.reserved_entry_cash or self.reserved_entry_cash>self.deployable_cash or (self.reserved_entry_notional>0 and self.projected_invested_pct_at_reference_price>Decimal("0.90")): raise ContractViolation("portfolio reservation cash/exposure mismatch")
        if self.current_planned_loss is not None: object.__setattr__(self,"current_planned_loss",_money(self.current_planned_loss,"current planned loss",nonnegative=True))
        if self.projected_heat_pct is not None: object.__setattr__(self,"projected_heat_pct",_money(self.projected_heat_pct,"projected heat",nonnegative=True))
        object.__setattr__(self,"profile",profile); object.__setattr__(self,"evidence_grade",grade); object.__setattr__(self,"reason_codes",_reasons(self.reason_codes))


@dataclass(frozen=True, slots=True)
class ReplacementCandidate:
    replacement_id: str; profile: RiskProfile; source_instrument: InstrumentId; source_exit_allocation_id: str; target_instrument: InstrumentId; target_entry_allocation_id: str; status: ReplacementStatus; source_exit_reason_codes: tuple[str,...]; target_rank_components: tuple[tuple[str,str],...]; estimated_release_amount: Decimal; target_required_cash: Decimal; funding_shortfall_after_current_cash: Decimal; reanalysis_required: bool; reason_codes: tuple[str,...]; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        profile=_enum(RiskProfile,self.profile,"replacement profile"); status=_enum(ReplacementStatus,self.status,"replacement status")
        if self.schema_version!=1 or self.source_instrument==self.target_instrument or self.source_instrument.market is not self.target_instrument.market or not self.reanalysis_required: raise ContractViolation("invalid replacement candidate")
        for name in ("estimated_release_amount","target_required_cash","funding_shortfall_after_current_cash"): object.__setattr__(self,name,_money(getattr(self,name),name,nonnegative=True))
        expected=stable_hash({"profile":profile,"source":self.source_instrument,"source_exit":self.source_exit_allocation_id,"target":self.target_instrument,"target_entry":self.target_entry_allocation_id,"status":status,"release":self.estimated_release_amount,"required":self.target_required_cash,"shortfall":self.funding_shortfall_after_current_cash,"reasons":_reasons(self.reason_codes)})
        if self.replacement_id!=expected: raise ContractViolation("replacement identity mismatch")
        object.__setattr__(self,"profile",profile); object.__setattr__(self,"status",status); object.__setattr__(self,"source_exit_reason_codes",_reasons(self.source_exit_reason_codes)); object.__setattr__(self,"reason_codes",_reasons(self.reason_codes)); object.__setattr__(self,"target_rank_components",tuple(sorted(self.target_rank_components))); object.__setattr__(self,"generated_at",ensure_utc(self.generated_at,"replacement generated_at"))


@dataclass(frozen=True, slots=True)
class PortfolioProfileDecision:
    profile_decision_id: str; batch_id: str; profile: RiskProfile; allocations: tuple[PortfolioAllocation,...]; reservation_groups: tuple[PortfolioReservationGroup,...]; holding_priority_allocation_ids: tuple[str,...]; entry_priority_allocation_ids: tuple[str,...]; blocked_allocation_ids: tuple[str,...]; current_risk_snapshot: PortfolioRiskSnapshot; reservation_snapshot: PortfolioReservationSnapshot; replacement_candidates: tuple[ReplacementCandidate,...]; evidence_grade: PortfolioEvidenceGrade; reason_codes: tuple[str,...]; generated_at: datetime
    def __post_init__(self):
        profile=_enum(RiskProfile,self.profile,"profile decision profile"); allocations=tuple(sorted(self.allocations,key=lambda x:x.allocation_id)); groups=tuple(sorted(self.reservation_groups,key=lambda x:x.group_id)); replacements=tuple(sorted(self.replacement_candidates,key=lambda x:x.replacement_id)); grade=_enum(PortfolioEvidenceGrade,self.evidence_grade,"profile decision grade")
        generated=ensure_utc(self.generated_at,"profile decision generated_at"); identifiers={item.allocation_id for item in allocations}
        group_ids={item.group_id for item in groups}; grouped_allocations={item.allocation_id for item in allocations if item.reservation_group_id is not None}; group_members={member for group in groups for member in group.member_allocation_ids}
        by_id={item.allocation_id:item for item in allocations}
        holding_priority=tuple(self.holding_priority_allocation_ids); entry_priority=tuple(self.entry_priority_allocation_ids); blocked=tuple(self.blocked_allocation_ids)
        expected_holding={item.allocation_id for item in allocations if item.action in {PlanAction.SELL,PlanAction.REDUCE} and item.final_requested_shares>0}
        expected_entries={item.allocation_id for item in allocations if item.action in {PlanAction.BUY,PlanAction.ADD}}
        expected_blocked={item.allocation_id for item in allocations if item.status is AllocationStatus.BLOCKED}
        reservation=self.reservation_snapshot
        reserved_cash=sum((item.reserved_cash for item in allocations),Decimal("0")); reserved_loss=sum((item.reserved_incremental_loss for item in allocations),Decimal("0")); reserved_notional=sum((item.final_requested_shares*item.reference_entry_price for item in allocations if item.action in {PlanAction.BUY,PlanAction.ADD} and item.final_requested_shares>0 and item.reference_entry_price is not None),Decimal("0")); expected_projected=(self.current_risk_snapshot.invested_value+reserved_notional)/self.current_risk_snapshot.equity if self.current_risk_snapshot.equity else Decimal("0")
        invalid_group=any(any(by_id[member].instrument!=group.instrument or by_id[member].profile is not profile or by_id[member].reservation_group_id!=group.group_id for member in group.member_allocation_ids) or group.max_aggregate_shares!=max(by_id[member].final_requested_shares for member in group.member_allocation_ids) for group in groups)
        invalid_replacement=any(item.source_exit_allocation_id not in by_id or item.target_entry_allocation_id not in by_id or by_id.get(item.source_exit_allocation_id) is None or by_id[item.source_exit_allocation_id].instrument!=item.source_instrument or by_id[item.source_exit_allocation_id].action not in {PlanAction.SELL,PlanAction.REDUCE} or by_id[item.source_exit_allocation_id].final_requested_shares<=0 or by_id[item.target_entry_allocation_id].instrument!=item.target_instrument or by_id[item.target_entry_allocation_id].action is not PlanAction.BUY or by_id[item.target_entry_allocation_id].final_requested_shares>=by_id[item.target_entry_allocation_id].approved_shares for item in replacements)
        if (len(identifiers)!=len(allocations) or len({item.candidate_id for item in allocations})!=len(allocations) or len({item.decision_id for item in allocations})!=len(allocations) or len(group_ids)!=len(groups) or any(item.batch_id!=self.batch_id or item.profile is not profile for item in allocations) or any(item.batch_id!=self.batch_id or item.profile is not profile for item in groups) or any(item.reservation_group_id is not None and item.reservation_group_id not in group_ids for item in allocations) or grouped_allocations!=group_members or invalid_group or reservation.profile is not profile or reservation.evidence_grade is not grade or reservation.frozen_equity!=self.current_risk_snapshot.equity or reservation.frozen_cash!=self.current_risk_snapshot.cash or reservation.current_planned_loss!=self.current_risk_snapshot.planned_loss_amount or reservation.reserved_entry_cash!=reserved_cash or reservation.reserved_incremental_loss!=reserved_loss or reservation.reserved_entry_notional!=reserved_notional or reservation.projected_invested_pct_at_reference_price!=expected_projected or any(item.profile is not profile for item in replacements) or invalid_replacement or len(set(holding_priority))!=len(holding_priority) or set(holding_priority)!=expected_holding or len(set(entry_priority))!=len(entry_priority) or set(entry_priority)!=expected_entries or set(blocked)!=expected_blocked or generated<self.current_risk_snapshot.calculated_at or any(generated<item.generated_at for item in (*allocations,*groups,*replacements))): raise ContractViolation("profile decision references inconsistent")
        expected=stable_hash({"batch_id":self.batch_id,"profile":profile,"allocation_ids":tuple(item.allocation_id for item in allocations),"group_ids":tuple(item.group_id for item in groups),"holding_priority":self.holding_priority_allocation_ids,"entry_priority":self.entry_priority_allocation_ids,"blocked":tuple(sorted(self.blocked_allocation_ids)),"risk_snapshot":self.current_risk_snapshot.risk_snapshot_id,"reservation":self.reservation_snapshot,"replacement_ids":tuple(item.replacement_id for item in replacements),"grade":grade,"reasons":_reasons(self.reason_codes)})
        if self.profile_decision_id!=expected: raise ContractViolation("profile decision identity mismatch")
        for key,value in (("profile",profile),("allocations",allocations),("reservation_groups",groups),("replacement_candidates",replacements),("blocked_allocation_ids",tuple(sorted(set(self.blocked_allocation_ids)))),("evidence_grade",grade),("reason_codes",_reasons(self.reason_codes)),("generated_at",generated)): object.__setattr__(self,key,value)


@dataclass(frozen=True, slots=True)
class PortfolioDecisionBundle:
    portfolio_bundle_id: str; batch_id: str; market: Market; account_hash: str; valuation_id: str; conservative: PortfolioProfileDecision; aggressive: PortfolioProfileDecision; portfolio_policy_version: str; generated_at: datetime; schema_version: int = 1
    def __post_init__(self):
        market=self.market if isinstance(self.market,Market) else Market(str(self.market).upper())
        generated=ensure_utc(self.generated_at,"portfolio bundle generated_at")
        if (self.schema_version!=1 or self.conservative.batch_id!=self.batch_id or self.aggressive.batch_id!=self.batch_id or self.conservative.profile is not RiskProfile.CONSERVATIVE or self.aggressive.profile is not RiskProfile.AGGRESSIVE or not self.portfolio_policy_version or generated<self.conservative.generated_at or generated<self.aggressive.generated_at): raise ContractViolation("portfolio bundle profile mismatch")
        expected=stable_hash({"batch_id":self.batch_id,"market":market,"account_hash":self.account_hash,"valuation_id":self.valuation_id,"conservative":self.conservative.profile_decision_id,"aggressive":self.aggressive.profile_decision_id,"policy":self.portfolio_policy_version})
        if self.portfolio_bundle_id!=expected: raise ContractViolation("portfolio bundle identity mismatch")
        object.__setattr__(self,"market",market); object.__setattr__(self,"generated_at",generated)
