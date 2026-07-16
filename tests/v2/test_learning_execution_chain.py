"""V2-9 使用真实 V2-5/V2-6/V2-7 合同生成策略账和联合账。"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from execution_helpers import intent_for
from risk_helpers import request_for
from test_fill_simulator import _event, _liquidity
from tradehelper_v2.contracts import (
    AllocationStatus,
    ContractViolation,
    EvidenceOrigin,
    EventGranularity,
    ExecutionEvent,
    ExecutionPolicy,
    ExecutionState,
    JointOutcomeKind,
    LearningEvidenceGrade,
    PlanAction,
    PortfolioAllocation,
    RiskProfile,
    TradingStatus,
    stable_hash,
)
from tradehelper_v2.execution import HistoricalFillSimulator, OrderIntentFactory
from tradehelper_v2.execution.simulator import HistoricalSimulationRequest
from tradehelper_v2.learning import EquityPoint, replay_joint, strategy_outcome
from tradehelper_v2.risk import RiskOfficer
from tradehelper_v2.risk.market_rules import default_market_rules


def _plans(request, bundle):
    return {
        plan.plan_id: plan
        for branch in (
            request.strategy_bundle.entry_or_add,
            request.strategy_bundle.reduce_or_exit,
            request.strategy_bundle.hold,
            request.strategy_bundle.invalidation,
        )
        for plan in branch.plans
        if any(decision.plan_id == plan.plan_id for decision in bundle.decisions)
    }


def test_strategy_outcome_consumes_real_risk_decision_and_fill(
    us_instrument,
    calendar,
    now,
):
    request=request_for(us_instrument,as_of=now)
    risk_bundle=RiskOfficer().assess(request,generated_at=now)
    plans=_plans(request,risk_bundle)
    intents=OrderIntentFactory(calendar).build_bundle(
        risk_bundle,
        plans,
        {},
        now,
        ExecutionPolicy(),
    ).intents
    replay_at=max(item.valid_from for item in intents)+timedelta(minutes=30)
    state=ExecutionState(
        us_instrument.market,
        "USD",
        Decimal("100000"),
        Decimal("0"),
        Decimal("0"),
        None,
        None,
        None,
        None,
        now,
        "fixture",
        account_hash=risk_bundle.account_hash,
    )
    filled=None
    selected_intent=None
    for index,intent in enumerate(intents):
        price=intent.trigger_level or Decimal("100")
        event=ExecutionEvent(
            f"learning-{index}",
            us_instrument,
            replay_at.date(),
            replay_at,
            replay_at,
            EventGranularity.QUOTE,
            price,
            price,
            price,
            price,
            Decimal("100000"),
            Decimal("100"),
            None,
            None,
            TradingStatus.OPEN,
            "fixture",
            "high",
            replay_at,
            replay_at,
        )
        result=HistoricalFillSimulator().simulate(
            HistoricalSimulationRequest(
                intent,
                state,
                (event,),
                request.market_rules,
                ExecutionPolicy(),
                _liquidity(now),
                replay_at,
            )
        )
        if result.run.outcome.value in {"filled","partial"}:
            filled=result.fills[0]
            selected_intent=intent
            break
    assert filled is not None and selected_intent is not None
    plan=plans[selected_intent.plan_id]
    decision=next(item for item in risk_bundle.decisions if item.decision_id == selected_intent.decision_id)
    outcome=strategy_outcome(
        plan=plan,
        decision=decision,
        horizon=5,
        target_session_date=replay_at.date()+timedelta(days=7),
        evidence_origin=EvidenceOrigin.RECONSTRUCTED_OOF,
        trigger_state="triggered",
        fill=filled,
        target_close=filled.fill_price*Decimal("1.05"),
        estimated_exit_cost=Decimal("1"),
        benchmark_return=Decimal(".01"),
        generated_at=replay_at,
        price_path=(filled.fill_price*Decimal(".98"),filled.fill_price*Decimal("1.03")),
        market_regime_key="risk_on",
    )
    assert outcome.entry_fill_id == filled.fill_id
    assert outcome.fill_outcome == filled.outcome.value
    assert outcome.net_return > Decimal(".04")
    assert outcome.mae == Decimal("-.02")
    assert outcome.mfe == Decimal(".05")


def test_window_close_without_exit_cost_cannot_claim_net_return(
    us_instrument,
    calendar,
    now,
):
    request=request_for(us_instrument,as_of=now)
    risk_bundle=RiskOfficer().assess(request,generated_at=now)
    plans=_plans(request,risk_bundle)
    intents=OrderIntentFactory(calendar).build_bundle(
        risk_bundle,plans,{},now,ExecutionPolicy()
    ).intents
    replay_at=max(item.valid_from for item in intents)+timedelta(minutes=30)
    state=ExecutionState(
        us_instrument.market,"USD",Decimal("100000"),Decimal("0"),Decimal("0"),
        None,None,None,None,now,"fixture",account_hash=risk_bundle.account_hash,
    )
    intent=fill=None
    for index,candidate in enumerate(intents):
        price=candidate.trigger_level or Decimal("100")
        event=ExecutionEvent(
            f"missing-exit-cost-{index}",
            us_instrument,
            replay_at.date(),
            replay_at,
            replay_at,
            EventGranularity.QUOTE,
            price,
            price,
            price,
            price,
            Decimal("100000"),
            Decimal("100"),
            None,
            None,
            TradingStatus.OPEN,
            "fixture",
            "high",
            replay_at,
            replay_at,
        )
        candidate_fill=HistoricalFillSimulator().simulate(
            HistoricalSimulationRequest(
                candidate,state,(event,),request.market_rules,ExecutionPolicy(),_liquidity(now),replay_at
            )
        ).fills[0]
        if candidate_fill.outcome.value in {"filled","partial"}:
            intent,fill=candidate,candidate_fill
            break
    assert intent is not None and fill is not None
    decision=next(item for item in risk_bundle.decisions if item.decision_id == intent.decision_id)
    outcome=strategy_outcome(
        plan=plans[intent.plan_id],
        decision=decision,
        horizon=5,
        target_session_date=replay_at.date()+timedelta(days=7),
        evidence_origin=EvidenceOrigin.RECONSTRUCTED_OOF,
        trigger_state="triggered",
        fill=fill,
        target_close=fill.fill_price*Decimal("1.05"),
        generated_at=replay_at,
    )
    assert outcome.status.value == "unverifiable"
    assert outcome.net_return is None
    assert "LEARNING_EXIT_COST_UNAVAILABLE" in outcome.reason_codes


def _allocation(fill, now, *, action=PlanAction.BUY):
    profile=RiskProfile.CONSERVATIVE
    values={
        "batch_id":"batch",
        "profile":profile,
        "candidate_id":"candidate",
        "instrument":fill.instrument,
        "plan_id":fill.plan_id,
        "decision_id":fill.decision_id,
        "action":action,
        "level":"A",
        "status":AllocationStatus.ALLOCATED_NOW,
        "rank":1,
        "rank_components":(),
        "approved":fill.requested_shares,
        "final":fill.requested_shares,
        "current_value":None,
        "entry":fill.fill_price,
        "reserved_cash":fill.gross_value+fill.total_fee,
        "reserved_loss":Decimal("100"),
        "estimated_pct":Decimal(".10"),
        "constraints":(),
        "reasons":(),
    }
    return PortfolioAllocation(
        stable_hash(values),
        "batch",
        profile,
        "candidate",
        fill.instrument,
        fill.plan_id,
        fill.decision_id,
        action,
        "A",
        AllocationStatus.ALLOCATED_NOW,
        1,
        (),
        fill.requested_shares,
        fill.requested_shares,
        None,
        fill.fill_price,
        fill.gross_value+fill.total_fee,
        Decimal("100"),
        Decimal(".10"),
        None,
        (),
        (),
        now,
    )


def _filled_buy(us_instrument, now):
    intent=intent_for(us_instrument,now)
    state=ExecutionState(
        us_instrument.market,
        "USD",
        Decimal("10000"),
        Decimal("0"),
        Decimal("0"),
        None,
        None,
        None,
        None,
        now,
        "fixture",
    )
    result=HistoricalFillSimulator().simulate(
        HistoricalSimulationRequest(
            intent,
            state,
            (_event(us_instrument,now),),
            default_market_rules(us_instrument.market,us_instrument.exchange,now),
            ExecutionPolicy(),
            _liquidity(now),
            now,
        )
    )
    return result.fills[0]


def test_joint_replay_consumes_frozen_allocation_and_real_fill(us_instrument, now):
    fill=_filled_buy(us_instrument,now)
    allocation=_allocation(fill,now)
    ending_price=fill.fill_price*Decimal("1.10")
    ending_equity=Decimal("10000")+fill.cash_delta+fill.filled_shares*ending_price
    outcome=replay_joint(
        outcome_kind=JointOutcomeKind.RECOMMENDATION_REPLAY,
        portfolio_bundle_id="portfolio",
        profile=RiskProfile.CONSERVATIVE,
        batch_id="batch",
        account_hash="a"*64,
        valuation_id="valuation",
        market=us_instrument.market,
        currency="USD",
        starting_cash=Decimal("10000"),
        starting_positions={},
        starting_prices={},
        fills=(fill,),
        ending_prices={us_instrument:ending_price},
        evidence_origin=EvidenceOrigin.ISSUED_ONLINE,
        benchmark_return=Decimal(".02"),
        generated_at=now+timedelta(days=1),
        ordered_allocations=(allocation,),
        equity_points=(
            EquityPoint(now,Decimal("10000")),
            EquityPoint(now+timedelta(days=1),ending_equity),
        ),
    )
    assert outcome.entry_count == 1
    assert outcome.ending_equity == ending_equity
    assert outcome.realized_friction == fill.total_fee
    assert outcome.evidence_grade is LearningEvidenceGrade.HIGH
    assert outcome.alpha == outcome.time_weighted_return-Decimal(".02")


def test_joint_replay_rejects_allocation_action_mismatch_and_unlocated_cash_flow(
    us_instrument,
    now,
):
    fill=_filled_buy(us_instrument,now)
    with pytest.raises(ContractViolation):
        replay_joint(
            outcome_kind=JointOutcomeKind.RECOMMENDATION_REPLAY,
            portfolio_bundle_id="portfolio",
            profile=RiskProfile.CONSERVATIVE,
            batch_id="batch",
            account_hash="a"*64,
            valuation_id="valuation",
            market=us_instrument.market,
            currency="USD",
            starting_cash=Decimal("10000"),
            starting_positions={},
            starting_prices={},
            fills=(fill,),
            ending_prices={us_instrument:fill.fill_price},
            evidence_origin=EvidenceOrigin.ISSUED_ONLINE,
            generated_at=now,
            ordered_allocations=(_allocation(fill,now,action=PlanAction.SELL),),
        )
    with pytest.raises(ContractViolation):
        replay_joint(
            outcome_kind=JointOutcomeKind.RECOMMENDATION_REPLAY,
            portfolio_bundle_id="portfolio",
            profile=RiskProfile.CONSERVATIVE,
            batch_id="batch",
            account_hash="a"*64,
            valuation_id="valuation",
            market=us_instrument.market,
            currency="USD",
            starting_cash=Decimal("10000"),
            starting_positions={},
            starting_prices={},
            fills=(),
            ending_prices={},
            evidence_origin=EvidenceOrigin.ISSUED_ONLINE,
            generated_at=now,
            external_cash_flows=((now.date(),Decimal("100")),),
        )
