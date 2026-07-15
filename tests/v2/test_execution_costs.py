"""V2-7 EX30--EX39：成交成本、流动性和 Decimal 保守量化。"""
from decimal import Decimal

from tradehelper_v2.contracts import ExecutionEvidenceGrade, ExecutionPolicy, LiquidityEvidence, OrderSide, stable_hash
from tradehelper_v2.execution.costs import CostModel
from tradehelper_v2.risk.market_rules import default_market_rules


def _liquidity(now, volume, volatility=Decimal("0.20")):
    payload = {"median_daily_volume_20": volume, "annualized_volatility_20": volatility, "cutoff_at": now, "source": "fixture"}
    return LiquidityEvidence(volume, volatility, now, "fixture", stable_hash(payload))


def test_liquidity_cap_and_slippage_are_monotonic(us_instrument, now):
    rules = default_market_rules(us_instrument.market, us_instrument.exchange, now)
    policy = ExecutionPolicy()
    low = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("1000"), market_rules=rules, policy=policy, liquidity=_liquidity(now, Decimal("100000")), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    high = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("4000"), market_rules=rules, policy=policy, liquidity=_liquidity(now, Decimal("100000")), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    capped = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("6000"), market_rules=rules, policy=policy, liquidity=_liquidity(now, Decimal("100000")), event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert low.slippage_rate <= high.slippage_rate <= capped.slippage_rate
    assert capped.fillable_shares == Decimal("5000")
    assert capped.unfilled_shares == Decimal("1000")


def test_missing_liquidity_is_low_evidence_and_buy_sell_are_adverse(us_instrument, now):
    rules = default_market_rules(us_instrument.market, us_instrument.exchange, now)
    liquidity = _liquidity(now, None)
    buy = CostModel.estimate(side=OrderSide.BUY, raw_price=Decimal("10"), requested_shares=Decimal("1"), market_rules=rules, policy=ExecutionPolicy(), liquidity=liquidity, event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    sell = CostModel.estimate(side=OrderSide.SELL, raw_price=Decimal("10"), requested_shares=Decimal("1"), market_rules=rules, policy=ExecutionPolicy(), liquidity=liquidity, event_at=now, evidence_grade=ExecutionEvidenceGrade.HIGH)
    assert buy.evidence_grade is ExecutionEvidenceGrade.LOW
    assert buy.fill_price > Decimal("10") > sell.fill_price
    assert "EXEC_LIQUIDITY_EVIDENCE_MISSING" in buy.reason_codes
