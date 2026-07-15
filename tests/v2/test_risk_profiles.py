from tradehelper_v2.contracts import RiskProfile
from tradehelper_v2.risk import RiskOfficer
from risk_helpers import request_for


def test_rk11_each_plan_has_two_risk_profiles(us_instrument):
    decisions = RiskOfficer().assess(request_for(us_instrument), generated_at=request_for(us_instrument).as_of).decisions
    assert {item.profile for item in decisions} == {RiskProfile.CONSERVATIVE, RiskProfile.AGGRESSIVE}
