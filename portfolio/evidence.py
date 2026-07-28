"""组合层只消费已冻结的账户、计划和相关性证据。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import combinations
from typing import Mapping, Sequence

from contracts import (CanonicalBar, CorrelationPair, CorrelationStatus, ExecutionLevel, HoldingRiskSnapshot, HoldingRiskStatus, InstrumentId, InstrumentReturnRisk, Market, PortfolioCorrelationSnapshot, PortfolioEvidenceGrade, PortfolioHeatStatus, PortfolioPolicy, PortfolioRiskSnapshot, QuantityIntent, RiskDecisionBundle, PlanAction, ValuationStatus, stable_hash)
from risk.sizing import friction_reserve


def build_holding_risks(*, valuation, account, candidates, risk_bundles, captured_at: datetime, generated_at: datetime) -> tuple[HoldingRiskSnapshot, ...]:
    """为每个真实持仓寻找最低的 A/B 全退出保护价，缺失时显式降级。"""
    protective={item for bundle in risk_bundles for item in bundle.protective_decision_ids}
    by_instrument={}
    for candidate in candidates:
        decision=candidate.execution_decision; plan=candidate.trade_plan
        if (candidate.role.value=="holding" and decision.decision_id in protective and decision.level in {ExecutionLevel.A,ExecutionLevel.B} and plan.action is PlanAction.SELL and plan.quantity_intent is QuantityIntent.FULL_EXIT):
            level=plan.stop.level.value if plan.stop and plan.stop.level else plan.trigger_level.value if plan.trigger_level else None
            if level is not None: by_instrument.setdefault(candidate.trade_plan.instrument,[]).append((Decimal(str(level)),plan,decision,candidate.market_rules))
    values={item.instrument:item for item in valuation.position_values}
    output=[]
    for position in account.positions:
        valued=values.get(position.instrument)
        candidates_for=by_instrument.get(position.instrument,())
        if valued is None or not candidates_for:
            price = None if valued is None else valued.price
            value = None if valued is None else valued.market_value
            stop=loss=None; reserve=Decimal("0"); status=HoldingRiskStatus.UNQUANTIFIED; source_plan=source_decision=None
        else:
            stop,plan,decision,rules=min(candidates_for,key=lambda item:item[0]); price=valued.price; value=valued.market_value; reserve=friction_reserve(position.shares,price,stop,rules); source_plan=plan.plan_id; source_decision=decision.decision_id
            status=HoldingRiskStatus.BREACHED if stop>=price else HoldingRiskStatus.QUANTIFIED
            loss=None if status is HoldingRiskStatus.BREACHED else (price-stop)*position.shares+reserve
        identity={"instrument":position.instrument,"shares":position.shares,"reference_price":price,"market_value":value,"stop_price":stop,"exit_friction_reserve":reserve,"status":status,"source_plan_id":source_plan,"source_decision_id":source_decision,"captured_at":captured_at}
        output.append(HoldingRiskSnapshot(stable_hash(identity),position.instrument,position.shares,price,value,stop,reserve,loss,status,source_plan,source_decision,captured_at,generated_at))
    return tuple(sorted(output,key=lambda item:item.instrument.stable_key))


def _returns_for(instrument: InstrumentId, bars: Sequence[CanonicalBar], *, cutoff_at: datetime, lookback: int):
    visible=sorted((bar for bar in bars if bar.instrument==instrument and bar.fetched_at<=cutoff_at and bar.trading_date<=cutoff_at.date()),key=lambda item:item.trading_date)
    visible=visible[-(lookback+1):]
    if len({bar.trading_date for bar in visible})!=len(visible) or len({bar.adjustment_mode for bar in visible})>1 or len({bar.corporate_action_version for bar in visible})>1:
        return {}, None, None, "conflicting", stable_hash(tuple(visible))
    returns={current.trading_date:Decimal(str(current.close))/Decimal(str(previous.close))-Decimal("1") for previous,current in zip(visible,visible[1:])}
    adjustment=visible[-1].adjustment_mode.value if visible else "unavailable"
    return returns,(visible[0].trading_date if len(visible)>1 else None),(visible[-1].trading_date if len(visible)>1 else None),adjustment,stable_hash(tuple(visible))


def _sample_std(values: Sequence[Decimal]) -> Decimal | None:
    if len(values)<2: return None
    mean=sum(values,Decimal("0"))/Decimal(len(values))
    variance=sum(((value-mean)**2 for value in values),Decimal("0"))/Decimal(len(values)-1)
    return variance.sqrt()


def _pearson(left: Sequence[Decimal],right: Sequence[Decimal]) -> Decimal | None:
    if len(left)<2 or len(left)!=len(right): return None
    lm=sum(left,Decimal("0"))/Decimal(len(left)); rm=sum(right,Decimal("0"))/Decimal(len(right))
    numerator=sum(((a-lm)*(b-rm) for a,b in zip(left,right)),Decimal("0"))
    denominator=(sum(((a-lm)**2 for a in left),Decimal("0"))*sum(((b-rm)**2 for b in right),Decimal("0"))).sqrt()
    return None if denominator==0 else max(Decimal("-1"),min(Decimal("1"),numerator/denominator))


def build_correlation_snapshot(*, market: Market, universe: Sequence[InstrumentId], bars_by_instrument: Mapping[InstrumentId,Sequence[CanonicalBar]], policy: PortfolioPolicy, cutoff_at: datetime, source_batch_hash: str, generated_at: datetime) -> PortfolioCorrelationSnapshot:
    """只使用 cutoff 前完成日 K，缺样本/口径冲突必须显式保留。"""
    instruments=tuple(sorted(set(universe),key=lambda item:item.stable_key))
    if any(item.market is not market for item in instruments): raise ValueError("correlation universe market mismatch")
    series={}; risks=[]
    for instrument in instruments:
        values,start,end,adjustment,bar_hash=_returns_for(instrument,bars_by_instrument.get(instrument,()),cutoff_at=cutoff_at,lookback=policy.correlation_lookback_sessions)
        series[instrument]=values
        volatility=_sample_std(tuple(values.values()))
        annualized=None if volatility is None else volatility*Decimal(policy.annualization_sessions).sqrt()
        risks.append(InstrumentReturnRisk(instrument,len(values),start,end,annualized,adjustment,bar_hash))
    pairs=[]
    for left,right in combinations(instruments,2):
        dates=sorted(set(series[left])&set(series[right])); coefficient=None
        if len(dates)>=policy.minimum_correlation_samples:
            coefficient=_pearson(tuple(series[left][date] for date in dates),tuple(series[right][date] for date in dates))
        status=CorrelationStatus.COMPLETE if coefficient is not None else CorrelationStatus.UNAVAILABLE
        pairs.append(CorrelationPair(left,right,coefficient,len(dates),status))
    completed=sum(item.sample_count>=policy.minimum_correlation_samples for item in risks)
    status=CorrelationStatus.UNAVAILABLE if not instruments or completed==0 else CorrelationStatus.COMPLETE if completed==len(instruments) and all(item.status is CorrelationStatus.COMPLETE for item in pairs) else CorrelationStatus.PARTIAL
    identity={"market":market,"universe":instruments,"instrument_risks":tuple(risks),"pairs":tuple(pairs),"lookback":policy.correlation_lookback_sessions,"minimum":policy.minimum_correlation_samples,"method":"simple_daily_close_return_v1","annualization":policy.annualization_sessions,"cutoff_at":cutoff_at,"status":status,"source_batch_hash":source_batch_hash}
    return PortfolioCorrelationSnapshot(stable_hash(identity),market,instruments,tuple(risks),tuple(pairs),policy.correlation_lookback_sessions,policy.minimum_correlation_samples,"simple_daily_close_return_v1",policy.annualization_sessions,cutoff_at,status,source_batch_hash,generated_at)


def build_portfolio_risk_snapshot(*, valuation, holding_risks, correlation_snapshot, policy, calculated_at: datetime) -> PortfolioRiskSnapshot:
    """不以已知风险子集冒充完整 heat；相关性缺失同样保持缺失。"""
    complete=valuation.status is ValuationStatus.COMPLETE
    equity=valuation.equity if complete and valuation.equity is not None else Decimal("0")
    invested=valuation.invested_value if complete and valuation.invested_value is not None else Decimal("0")
    positions=valuation.position_values if complete else ()
    invested_pct=invested/equity if equity else Decimal("0")
    weights=list((item.instrument,item.market_value/equity) for item in positions) if equity else []
    if weights:
        # Decimal division can round each position independently, leaving the
        # summed weights one ULP away from invested/equity. Derive one weight
        # as the exact complement so operation ordering cannot retain a ULP.
        largest_index=max(range(len(weights)),key=lambda index:weights[index][1])
        largest_instrument,_=weights[largest_index]
        other_weight_total=sum(
            (weight for index,(_,weight) in enumerate(weights) if index!=largest_index),
            Decimal("0"),
        )
        weights[largest_index]=(largest_instrument,invested_pct-other_weight_total)
    weights=tuple(weights)
    risks={item.instrument:item for item in holding_risks}
    heat=PortfolioHeatStatus.BREACHED if any(item.status is HoldingRiskStatus.BREACHED for item in holding_risks) else PortfolioHeatStatus.INCOMPLETE if not complete or equity<=0 or any(item.status is HoldingRiskStatus.UNQUANTIFIED for item in holding_risks) else PortfolioHeatStatus.COMPLETE
    loss=sum((item.planned_loss_amount for item in holding_risks),Decimal("0")) if heat is PortfolioHeatStatus.COMPLETE else None
    loss_pct=None if loss is None else loss/equity
    hhi=sum((weight*weight for _,weight in weights),Decimal("0")); max_item,max_weight=max(weights,key=lambda item:item[1],default=(None,Decimal("0")))
    reasons=[]
    if not complete: reasons.append("PORTFOLIO_INCOMPLETE_VALUATION")
    elif equity==0: reasons.append("PORTFOLIO_EQUITY_ZERO")
    if hhi>=policy.hhi_warning: reasons.append("PORTFOLIO_HHI_WARNING")
    if heat is not PortfolioHeatStatus.COMPLETE: reasons.append("PORTFOLIO_HOLDING_RISK_UNKNOWN" if heat is PortfolioHeatStatus.INCOMPLETE else "PORTFOLIO_STOP_ALREADY_BREACHED")
    volatility = None
    held_instruments={instrument for instrument,_ in weights}
    high_pairs = tuple(pair for pair in correlation_snapshot.pairs if pair.left in held_instruments and pair.right in held_instruments and pair.status is CorrelationStatus.COMPLETE and pair.coefficient >= policy.high_correlation_threshold)
    risk_by_instrument = {item.instrument: item for item in correlation_snapshot.instrument_risks}
    if weights and all(risk_by_instrument.get(instrument) and risk_by_instrument[instrument].annualized_volatility is not None for instrument, _ in weights):
        variance = Decimal("0")
        for left, left_weight in weights:
            for right, right_weight in weights:
                coefficient = Decimal("1") if left == right else next((pair.coefficient for pair in correlation_snapshot.pairs if {pair.left, pair.right} == {left, right} and pair.status is CorrelationStatus.COMPLETE), None)
                if coefficient is None:
                    variance = None; break
                variance += left_weight * right_weight * risk_by_instrument[left].annualized_volatility * risk_by_instrument[right].annualized_volatility * coefficient
            if variance is None: break
        if variance is not None:
            volatility = variance.sqrt() if variance > 0 else Decimal("0")
    if volatility is None:
        reasons.append("PORTFOLIO_VOLATILITY_UNAVAILABLE")
    grade=PortfolioEvidenceGrade.HIGH if equity>0 and heat is PortfolioHeatStatus.COMPLETE and correlation_snapshot.status is CorrelationStatus.COMPLETE else PortfolioEvidenceGrade.LOW if equity>0 and heat is PortfolioHeatStatus.COMPLETE else PortfolioEvidenceGrade.INSUFFICIENT
    reasons.append({PortfolioEvidenceGrade.HIGH:"PORTFOLIO_EVIDENCE_HIGH",PortfolioEvidenceGrade.LOW:"PORTFOLIO_EVIDENCE_LOW",PortfolioEvidenceGrade.INSUFFICIENT:"PORTFOLIO_EVIDENCE_INSUFFICIENT"}[grade])
    identity={"market":valuation.market,"valuation_id":valuation.valuation_id,"equity":equity,"cash":valuation.cash,"invested":invested,"invested_pct":invested_pct,"weights":weights,"max_position_instrument":max_item,"max_position_pct":max_weight,"hhi":hhi,"volatility":volatility,"loss":loss,"loss_pct":loss_pct,"high_pairs":high_pairs,"heat":heat,"grade":grade,"reasons":tuple(sorted(set(reasons))),"calculated_at":calculated_at}
    return PortfolioRiskSnapshot(stable_hash(identity),valuation.market,valuation.valuation_id,equity,valuation.cash,invested,invested_pct,weights,max_item,max_weight,hhi,volatility,loss,loss_pct,high_pairs,heat,grade,tuple(reasons),calculated_at)
