from tradehelper_v2.contracts import DecisionDisposition, ExecutionLevel, PlanAction
from tradehelper_v2.risk import RiskOfficer
from risk_helpers import request_for


def test_rk01_no_account_is_not_simulated(us_instrument):
    request = request_for(us_instrument); request = request.__class__(request.instrument, request.strategy_bundle, request.trading_scenario, request.data_quality, None, None, None, (), request.market_rules, None, request.policy, request.as_of)
    decisions = RiskOfficer().assess(request, generated_at=request.as_of).decisions
    assert all(item.approved_shares == 0 for item in decisions if item.action in {PlanAction.BUY, PlanAction.ADD})
    assert all(item.level is ExecutionLevel.C for item in decisions if item.action is PlanAction.BUY)


def test_rk06_waiting_is_conditionally_approved(us_instrument):
    decisions = RiskOfficer().assess(request_for(us_instrument), generated_at=request_for(us_instrument).as_of).decisions
    assert any(item.action is PlanAction.BUY and item.disposition is DecisionDisposition.CONDITIONALLY_APPROVED for item in decisions)
