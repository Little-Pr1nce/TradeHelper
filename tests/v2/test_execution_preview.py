"""V2-7 EX29/EX46：当前预览只能估计，不能伪造成交。"""
from decimal import Decimal

from execution_helpers import intent_for
from tradehelper_v2.contracts import DecisionMode, ExecutionPolicy, ExecutionState, FreshnessStatus, LiquidityEvidence, MarketState, TradingSession, stable_hash
from tradehelper_v2.execution.preview import CurrentPreviewBuilder
from tradehelper_v2.risk.market_rules import default_market_rules


def test_current_preview_is_never_historical_fill(us_instrument, now):
    liquidity_payload={"median_daily_volume_20":Decimal("100000"),"annualized_volatility_20":Decimal("0.2"),"cutoff_at":now,"source":"fixture"}
    preview=CurrentPreviewBuilder(ExecutionPolicy()).build(intent_for(us_instrument,now),ExecutionState(us_instrument.market,"USD",Decimal("10000"),Decimal("0"),Decimal("0"),None,None,None,None,now,"fixture"),MarketState(us_instrument,DecisionMode.INTRADAY,TradingSession.REGULAR,Decimal("101"),Decimal("100"),None,None,Decimal("100"),now,"fixture",FreshnessStatus.FRESH),default_market_rules(us_instrument.market,us_instrument.exchange,now),LiquidityEvidence(Decimal("100000"),Decimal("0.2"),now,"fixture",stable_hash(liquidity_payload)),now)
    assert preview.status.value == "ready"
    assert "EXEC_CURRENT_PREVIEW_ONLY" in preview.reason_codes
    assert preview.estimated_fill_low is not None
