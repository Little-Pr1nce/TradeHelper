"""版本化双市场预检；不创建订单，也不模拟实际成交。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradehelper_v2.contracts import (DecisionMode, FreshnessStatus, InstrumentClassification, Market, MarketEligibility, MarketRuleSet, MarketState, PlanAction, TradingSession)


def default_market_rules(market: Market, exchange, as_of: datetime, classification: InstrumentClassification = InstrumentClassification.ORDINARY) -> MarketRuleSet:
    if market is Market.US:
        return MarketRuleSet("us_rules_v1", market, exchange, Decimal("1"), False, Decimal("0.0003"), Decimal("0"), Decimal("0"), Decimal("0.003"), None, classification, "v2_policy", as_of, None)
    limits = {InstrumentClassification.ORDINARY: .10, InstrumentClassification.ST: .05, InstrumentClassification.GROWTH: .20, InstrumentClassification.STAR: .20, InstrumentClassification.BSE: .30}
    return MarketRuleSet("a_rules_v1", market, exchange, Decimal("100"), True, Decimal("0.0003"), Decimal("5"), Decimal("0.0005"), Decimal("0.003"), limits.get(classification), classification, "v2_policy", as_of, None)


@dataclass(frozen=True, slots=True)
class RulePrecheck:
    eligibility: MarketEligibility
    reasons: tuple[str, ...]
    liquidity_multiplier: Decimal


def precheck(rules: MarketRuleSet, state: MarketState | None, action: PlanAction) -> RulePrecheck:
    """评估当前可执行性；盘前未知事实只要求触发时复检。"""
    if state is None:
        return RulePrecheck(MarketEligibility.RECHECK_REQUIRED, ("RISK_MARKET_RECHECK_REQUIRED",), Decimal("1"))
    if state.mode is DecisionMode.EOD:
        # 收盘事实只能生成下一交易会话的计划，不能冒充当前仍可成交。
        return RulePrecheck(MarketEligibility.RECHECK_REQUIRED, ("RISK_MARKET_RECHECK_REQUIRED",), Decimal("1"))
    if state.freshness_status is not FreshnessStatus.FRESH:
        if state.mode is DecisionMode.INTRADAY:
            return RulePrecheck(MarketEligibility.BLOCKED, ("RISK_MARKET_RECHECK_REQUIRED",), Decimal("1"))
        if state.mode is DecisionMode.PRE:
            return RulePrecheck(MarketEligibility.RECHECK_REQUIRED, ("RISK_EXTENDED_PRICE_ONLY", "RISK_NO_LEVEL2_DEPTH", "RISK_MARKET_RECHECK_REQUIRED"), Decimal("0.25"))
    if rules.market is Market.A:
        if state.mode is DecisionMode.PRE or state.session is not TradingSession.REGULAR:
            return RulePrecheck(MarketEligibility.RECHECK_REQUIRED, ("RISK_MARKET_RECHECK_REQUIRED",), Decimal("1"))
        if state.current_price is None or state.previous_close is None:
            return RulePrecheck(MarketEligibility.RECHECK_REQUIRED, ("RISK_MARKET_RECHECK_REQUIRED",), Decimal("1"))
        change = float(state.current_price / state.previous_close - 1)
        limit = rules.price_limit_pct
        if rules.instrument_classification is InstrumentClassification.UNKNOWN and abs(change) >= .049:
            return RulePrecheck(MarketEligibility.BLOCKED, ("RISK_A_CLASSIFICATION_UNKNOWN",), Decimal("1"))
        if limit is not None and action in {PlanAction.BUY, PlanAction.ADD} and change >= limit - .001:
            return RulePrecheck(MarketEligibility.BLOCKED, ("RISK_PRICE_LIMIT_BLOCKED",), Decimal("1"))
        if limit is not None and action in {PlanAction.REDUCE, PlanAction.SELL} and change <= -limit + .001:
            return RulePrecheck(MarketEligibility.BLOCKED, ("RISK_PRICE_LIMIT_BLOCKED", "RISK_PROTECTIVE_EXIT_PRIORITY"), Decimal("1"))
        return RulePrecheck(MarketEligibility.ELIGIBLE, (), Decimal("1"))
    if state.mode is not DecisionMode.PRE:
        return RulePrecheck(MarketEligibility.ELIGIBLE, (), Decimal("1"))
    if state.bid is not None and state.ask is not None:
        spread = (state.ask - state.bid) / ((state.ask + state.bid) / 2)
        multiplier = Decimal("0.75") if spread <= Decimal("0.002") else Decimal("0.50") if spread <= Decimal("0.005") else Decimal("0.25")
        return RulePrecheck(MarketEligibility.RECHECK_REQUIRED, ("RISK_EXTENDED_TOP_OF_BOOK_ONLY",), multiplier)
    if state.volume is not None:
        return RulePrecheck(MarketEligibility.RECHECK_REQUIRED, ("RISK_EXTENDED_VOLUME_PROXY", "RISK_NO_LEVEL2_DEPTH"), Decimal("0.50"))
    return RulePrecheck(MarketEligibility.RECHECK_REQUIRED, ("RISK_EXTENDED_PRICE_ONLY", "RISK_NO_LEVEL2_DEPTH"), Decimal("0.25"))
