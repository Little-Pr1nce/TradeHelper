from decimal import Decimal

from tradehelper_v2.risk import RiskOfficer
from risk_helpers import request_for


def test_rk35_a_and_us_have_same_conditional_entry_semantics(a_instrument, us_instrument):
    a = RiskOfficer().assess(request_for(a_instrument, cash=Decimal("100000")), generated_at=request_for(a_instrument, cash=Decimal("100000")).as_of)
    us = RiskOfficer().assess(request_for(us_instrument), generated_at=request_for(us_instrument).as_of)
    assert any(item.disposition.value == "conditionally_approved" for item in a.decisions)
    assert any(item.disposition.value == "conditionally_approved" for item in us.decisions)
