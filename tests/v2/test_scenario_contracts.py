from dataclasses import replace
from datetime import timedelta
import pytest
from tradehelper_v2.contracts import ContractViolation, DecisionMode, ScenarioFactKind, ScenarioFactUpdate
from test_scenario_planner import NOW, _forecast, _mode_request, _quote, _request, _snapshot
from tradehelper_v2.scenario import ScenarioPlanner

def test_sc00_identity_is_stable_and_policy_sensitive(us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    first=ScenarioPlanner().build(request); second=ScenarioPlanner().build(request)
    assert first.scenario_id == second.scenario_id and first.event_key == second.event_key
    changed=type(request)(request.instrument,request.mode,request.as_of,request.origin_snapshot,request.current_snapshot,None,(),request.forecasts,request.data_quality,request.decision_session,"scenario_policy_v2")
    assert ScenarioPlanner().build(changed).scenario_id != first.scenario_id

def test_sc00_forecast_generated_at_is_not_business_identity(us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    first=ScenarioPlanner().build(request,generated_at=NOW)
    forecasts=list(request.forecasts)
    forecasts[0]=replace(forecasts[0],generated_at=NOW+timedelta(seconds=1))
    second=ScenarioPlanner().build(replace(request,forecasts=tuple(forecasts)),generated_at=NOW+timedelta(seconds=2))
    assert first.forecast_bundle_hash == second.forecast_bundle_hash
    assert first.scenario_id == second.scenario_id
    assert first.event_key == second.event_key

def test_sc00_changed_current_feature_quality_and_session_change_identity(us_instrument):
    base=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    baseline=ScenarioPlanner().build(base)
    changed_quality=replace(base,data_quality=replace(base.data_quality,score=99.0))
    changed_session=replace(base,decision_session=replace(base.decision_session,regular_close=base.decision_session.regular_close-timedelta(minutes=30)))
    assert ScenarioPlanner().build(changed_quality).scenario_id != baseline.scenario_id
    assert ScenarioPlanner().build(changed_session).scenario_id != baseline.scenario_id

    quote=_quote(us_instrument,observed_at=NOW-timedelta(minutes=5))
    pre=_mode_request(base,DecisionMode.PRE,quote=quote)
    changed_snapshot=replace(pre,current_snapshot=replace(pre.current_snapshot,feature_hash="c"*64))
    assert ScenarioPlanner().build(pre).scenario_id != ScenarioPlanner().build(changed_snapshot).scenario_id

def test_sc00_rejects_unregistered_fact_feature():
    with pytest.raises(ContractViolation): ScenarioFactUpdate(ScenarioFactKind.NEWS,"n",NOW,"x","a"*64,("fund.pe_ttm",))
    with pytest.raises(ContractViolation): ScenarioFactUpdate(ScenarioFactKind.NEWS,"n",NOW,"x","not-a-hash",("news.count_1d",))
    with pytest.raises(ContractViolation): ScenarioFactUpdate(ScenarioFactKind.NEWS,"n",NOW,"x","a"*64,("news.not_registered",))

def test_sc00_request_rejects_missing_quote_payload_and_future_quality(us_instrument):
    base=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    quote=_quote(us_instrument,observed_at=NOW-timedelta(minutes=5))
    snapshot=_snapshot(us_instrument,DecisionMode.PRE,base.origin_snapshot.latest_bar_date,quote=quote)
    with pytest.raises(ContractViolation):
        replace(base,mode=DecisionMode.PRE,current_snapshot=snapshot,current_quote=None)
    with pytest.raises(ContractViolation):
        replace(base,data_quality=replace(base.data_quality,evaluated_at=NOW+timedelta(seconds=1)))

def test_sc00_scenario_contract_rejects_forged_hash_or_reason(us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)]))
    with pytest.raises(ContractViolation):
        replace(scenario,forecast_bundle_hash="0"*64)
    with pytest.raises(ContractViolation):
        replace(scenario,reason_codes=("FREE_TEXT",))
