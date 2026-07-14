from dataclasses import replace

import pytest

from tradehelper_v2.contracts import (
    EntryPosture,
    ForecastAvailability,
    ForecastScope,
    ForecastSupportLevel,
    QualityAction,
    QualityStatus,
    ScenarioStatus,
    StrategyFamily,
)
from tradehelper_v2.scenario import ScenarioPlanner
from test_scenario_planner import _forecast, _request


def _unavailable(forecast, availability):
    target=forecast.target_session_date
    target_key=target.isoformat()
    if availability is ForecastAvailability.CALENDAR_UNAVAILABLE:
        target=None
        target_key="calendar-unavailable"
    event_key="|".join((
        forecast.instrument.stable_key,forecast.origin_session_date.isoformat(),target_key,
        str(forecast.horizon),forecast.model_version,forecast.model_input_hash,
    ))
    return replace(
        forecast,availability=availability,target_session_date=target,probabilities=None,
        return_distribution=None,direction=None,confidence_margin=None,
        execution_eligible=False,event_key=event_key,reason=availability.value,
    )


def test_sc07_cross_stock_and_baseline_forecasts_are_observational(us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    forecasts=[]
    scopes=(ForecastScope.INDUSTRY,ForecastScope.MARKET,ForecastScope.BASELINE,ForecastScope.BASELINE)
    for forecast,scope in zip(request.forecasts,scopes):
        forecasts.append(replace(forecast,model_scope=scope,scope_key=f"{scope.value}:fixture",execution_eligible=False))
    scenario=ScenarioPlanner().build(replace(request,forecasts=tuple(forecasts)))
    assert scenario.forecast_support is ForecastSupportLevel.OBSERVATIONAL
    assert scenario.status is ScenarioStatus.OBSERVATION_ONLY
    assert scenario.entry_posture is EntryPosture.OBSERVATION_ONLY
    assert StrategyFamily.TREND_CONTINUATION in scenario.blocked_strategy_families


def test_sc08_insufficient_samples_still_emit_observation_scenario(us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    forecasts=tuple(_unavailable(item,ForecastAvailability.INSUFFICIENT_SAMPLE) for item in request.forecasts)
    scenario=ScenarioPlanner().build(replace(request,forecasts=forecasts))
    assert scenario.forecast_support is ForecastSupportLevel.UNAVAILABLE
    assert scenario.status is ScenarioStatus.OBSERVATION_ONLY
    assert all(item.probabilities is None for item in scenario.horizon_assessments)


def test_sc08_calendar_unavailable_blocks_without_inventing_session(us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    forecasts=tuple(_unavailable(item,ForecastAvailability.CALENDAR_UNAVAILABLE) for item in request.forecasts)
    scenario=ScenarioPlanner().build(replace(request,forecasts=forecasts,decision_session=None))
    assert scenario.decision_session is None
    assert scenario.valid_from is None and scenario.expires_at is None
    assert scenario.status is ScenarioStatus.BLOCKED
    assert scenario.entry_posture is EntryPosture.BLOCKED


def test_sc16_blocked_symbol_does_not_change_another_symbol(a_instrument,us_instrument):
    planner=ScenarioPlanner()
    us_request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    a_request=_request(a_instrument,[_forecast(a_instrument,h) for h in (1,3,5,10)])
    us_before=planner.build(us_request)
    blocked_quality=replace(
        a_request.data_quality,status=QualityStatus.BLOCKED,
        action=QualityAction.BLOCK_NEW_ENTRIES,block_new_entries=True,
    )
    a_scenario=planner.build(replace(a_request,data_quality=blocked_quality))
    us_after=planner.build(us_request)
    assert a_scenario.status is ScenarioStatus.BLOCKED
    assert us_after.scenario_id == us_before.scenario_id
    assert us_after.status is ScenarioStatus.READY


@pytest.mark.parametrize("case",["bullish","bearish","conflict","unavailable","blocked"])
def test_sc17_protection_survives_every_degradation(case,us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h,case if case in {"bullish","bearish"} else "bullish") for h in (1,3,5,10)])
    if case == "conflict":
        forecasts=list(request.forecasts)
        forecasts[1]=_request(us_instrument,[_forecast(us_instrument,h,"bearish" if h==3 else "bullish") for h in (1,3,5,10)]).forecasts[1]
        request=replace(request,forecasts=tuple(forecasts))
    elif case == "unavailable":
        request=replace(request,forecasts=tuple(_unavailable(item,ForecastAvailability.INSUFFICIENT_SAMPLE) for item in request.forecasts))
    elif case == "blocked":
        request=replace(request,data_quality=replace(request.data_quality,status=QualityStatus.BLOCKED,action=QualityAction.BLOCK_NEW_ENTRIES,block_new_entries=True))
    scenario=ScenarioPlanner().build(request)
    assert {
        StrategyFamily.PROTECTIVE_EXIT,
        StrategyFamily.PROFIT_LOCK,
        StrategyFamily.FAILED_REBOUND_EXIT,
    }.issubset(scenario.allowed_strategy_families)
