"""V2-7 EX20--EX29：历史成交、现金缩量与状态增量。"""
from decimal import Decimal

from execution_helpers import intent_for
from tradehelper_v2.contracts import ExecutionEvent, ExecutionEvidenceGrade, ExecutionPolicy, ExecutionState, EventGranularity, LiquidityEvidence, TradingStatus, stable_hash
from tradehelper_v2.execution import HistoricalFillSimulator
from tradehelper_v2.execution.simulator import HistoricalSimulationRequest
from tradehelper_v2.risk.market_rules import default_market_rules


def _event(instrument, now, price="101"):
    value=Decimal(price)
    return ExecutionEvent("trigger",instrument,now.date(),now,now,EventGranularity.QUOTE,value,value,value,value,Decimal("1000"),Decimal("100"),None,None,TradingStatus.OPEN,"fixture","high",now,now)


def _liquidity(now):
    payload={"median_daily_volume_20":Decimal("100000"),"annualized_volatility_20":Decimal("0.20"),"cutoff_at":now,"source":"fixture"}
    return LiquidityEvidence(Decimal("100000"),Decimal("0.20"),now,"fixture",stable_hash(payload))


def test_filled_buy_applies_adverse_price_and_protective_levels(us_instrument, now):
    intent=intent_for(us_instrument,now)
    state=ExecutionState(us_instrument.market,"USD",Decimal("10000"),Decimal("0"),Decimal("0"),None,None,None,None,now,"fixture")
    result=HistoricalFillSimulator().simulate(HistoricalSimulationRequest(intent,state,(_event(us_instrument,now),),default_market_rules(us_instrument.market,us_instrument.exchange,now),ExecutionPolicy(),_liquidity(now),now))
    assert result.run.outcome.value == "filled"
    assert result.fills[0].fill_price > Decimal("100")
    assert result.run.final_state_delta.active_stop == Decimal("95")
    assert result.run.final_state_delta.active_take_profit == Decimal("110")


def test_cash_shortage_reduces_or_rejects_without_expanding_risk(us_instrument, now):
    intent=intent_for(us_instrument,now,shares=Decimal("10"))
    state=ExecutionState(us_instrument.market,"USD",Decimal("500"),Decimal("0"),Decimal("0"),None,None,None,None,now,"fixture")
    result=HistoricalFillSimulator().simulate(HistoricalSimulationRequest(intent,state,(_event(us_instrument,now),),default_market_rules(us_instrument.market,us_instrument.exchange,now),ExecutionPolicy(),_liquidity(now),now))
    assert result.fills[0].filled_shares <= intent.requested_shares
    assert result.run.outcome.value in {"partial", "rejected"}
