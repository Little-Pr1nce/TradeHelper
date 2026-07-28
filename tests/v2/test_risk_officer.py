from dataclasses import replace

from contracts import DecisionDisposition, ExecutionLevel, PlanAction
from risk import RiskOfficer
from risk_helpers import request_for


def test_rk01_no_account_is_not_simulated(us_instrument):
    request = request_for(us_instrument); request = request.__class__(request.instrument, request.strategy_bundle, request.trading_scenario, request.data_quality, None, None, None, (), request.market_rules, None, request.policy, request.as_of)
    decisions = RiskOfficer().assess(request, generated_at=request.as_of).decisions
    assert all(item.approved_shares == 0 for item in decisions if item.action in {PlanAction.BUY, PlanAction.ADD})
    assert all(item.level is ExecutionLevel.C for item in decisions if item.action is PlanAction.BUY)


def test_rk06_waiting_is_conditionally_approved(us_instrument):
    decisions = RiskOfficer().assess(request_for(us_instrument), generated_at=request_for(us_instrument).as_of).decisions
    assert any(item.action is PlanAction.BUY and item.disposition is DecisionDisposition.CONDITIONALLY_APPROVED for item in decisions)


def test_noninferior_forecast_keeps_new_risk_at_b_level(us_instrument):
    request = request_for(us_instrument)
    scenario = replace(
        request.trading_scenario,
        reason_codes=tuple(sorted(set(request.trading_scenario.reason_codes) | {"FORECAST_STOCK_NONINFERIOR"})),
    )
    request = replace(request, trading_scenario=scenario)
    decisions = RiskOfficer().assess(request, generated_at=request.as_of).decisions
    entries = [item for item in decisions if item.action is PlanAction.BUY]
    assert entries and all(item.level is ExecutionLevel.B for item in entries)
    assert all("RISK_FORECAST_NONINFERIOR_CAP" in item.reason_codes for item in entries)
