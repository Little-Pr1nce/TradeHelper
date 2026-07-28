"""V2-7 EX40--EX47：A/美股最终成交规则。"""
from datetime import timedelta
from decimal import Decimal

from execution_helpers import intent_for
from contracts import ExecutionEvent, ExecutionState, EventGranularity, PlanAction, TradingStatus
from execution.market_rules import ExecutionMarketRules
from risk.market_rules import default_market_rules


def _event(instrument, now, *, price="100", status=TradingStatus.OPEN, volume=Decimal("1000")):
    value = Decimal(price)
    return ExecutionEvent("event", instrument, now.date(), now, now, EventGranularity.QUOTE, value, value, value, value, volume, Decimal("100"), None, None, status, "fixture", "high", now, now)


def test_a_share_lot_t1_and_full_exit_odd_lot(a_instrument, now):
    rules = default_market_rules(a_instrument.market, a_instrument.exchange, now)
    intent = intent_for(a_instrument, now, action=PlanAction.SELL, shares=Decimal("150"))
    state = ExecutionState(a_instrument.market, "CNY", Decimal("0"), Decimal("150"), Decimal("150"), Decimal("100"), now.date() - timedelta(days=1), None, None, now, "fixture")
    check = ExecutionMarketRules.check(intent, state, _event(a_instrument, now), rules)
    assert check.permitted_shares == Decimal("150")
    same_day = ExecutionState(a_instrument.market, "CNY", Decimal("0"), Decimal("150"), Decimal("150"), Decimal("100"), now.date(), None, None, now, "fixture")
    assert ExecutionMarketRules.check(intent, same_day, _event(a_instrument, now), rules).reason_codes == ("EXEC_T1_BLOCKED",)


def test_suspension_unknown_and_zero_volume_have_distinct_reasons(us_instrument, now):
    intent = intent_for(us_instrument, now)
    state = ExecutionState(us_instrument.market, "USD", Decimal("10000"), Decimal("0"), Decimal("0"), None, None, None, None, now, "fixture")
    rules = default_market_rules(us_instrument.market, us_instrument.exchange, now)
    assert ExecutionMarketRules.check(intent, state, _event(us_instrument, now, status=TradingStatus.SUSPENDED), rules).reason_codes == ("EXEC_SUSPENDED",)
    assert ExecutionMarketRules.check(intent, state, _event(us_instrument, now, status=TradingStatus.UNKNOWN), rules).reason_codes == ("EXEC_TRADING_STATUS_UNKNOWN",)
    assert ExecutionMarketRules.check(intent, state, _event(us_instrument, now, volume=Decimal("0")), rules).reason_codes == ("EXEC_NO_TRADABLE_VOLUME",)
