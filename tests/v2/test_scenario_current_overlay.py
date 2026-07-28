from datetime import timedelta

import pytest

from contracts import (
    CurrentPriceState,
    DecisionMode,
    EntryPosture,
    ForecastSupportLevel,
    NewsSnapshot,
    PriceLocation,
    ScenarioFactKind,
    ScenarioFactUpdate,
    ScenarioStatus,
    StrategyFamily,
    TradingSession,
)
from scenario import ScenarioPlanner, build_fact_updates
from contracts.scenario import REGISTERED_NEWS_FEATURES
from test_scenario_planner import NOW, _forecast, _mode_request, _quote, _request


def test_sc09_remaining_return_uses_ratio_not_subtraction(us_instrument):
    base=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    quote=_quote(us_instrument,price=105.0,observed_at=NOW-timedelta(minutes=5),source="nasdaq")
    scenario=ScenarioPlanner().build(_mode_request(base,DecisionMode.PRE,quote=quote))
    remaining=scenario.horizon_assessments[0].remaining_distribution
    assert remaining.p10 == pytest.approx(-.0857142857)
    assert remaining.p50 == pytest.approx(-.0190476190)
    assert remaining.p90 == pytest.approx(.0285714286)
    assert scenario.current_overlay.realized_return_from_origin == pytest.approx(.05)


def test_sc10_above_interval_waits_for_pullback(us_instrument):
    base=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    quote=_quote(us_instrument,price=110.0,observed_at=NOW-timedelta(minutes=5),source="nasdaq")
    scenario=ScenarioPlanner().build(_mode_request(base,DecisionMode.PRE,quote=quote))
    assert scenario.horizon_assessments[0].price_location is PriceLocation.ABOVE_P90
    assert scenario.status is ScenarioStatus.DEGRADED
    assert scenario.entry_posture is EntryPosture.WAIT_PULLBACK
    assert StrategyFamily.TREND_CONTINUATION in scenario.blocked_strategy_families
    assert StrategyFamily.BREAKOUT_CONFIRMATION in scenario.blocked_strategy_families


def test_sc11_explicit_fact_update_degrades_without_mutating_forecast(us_instrument):
    base=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    as_of=NOW+timedelta(hours=1)
    update=ScenarioFactUpdate(
        ScenarioFactKind.NEWS,"news-1",NOW+timedelta(minutes=30),"finnhub","a"*64,
        ("news.count_1d","news.count_7d"),
    )
    request=_mode_request(base,DecisionMode.PRE,as_of=as_of,news_sentiment=.2,fact_updates=(update,))
    before=tuple(base.forecasts)
    scenario=ScenarioPlanner().build(request)
    assert request.forecasts == before
    assert scenario.current_overlay.unmodeled_fact_update is True
    assert scenario.current_overlay.fact_update_count == 1
    assert scenario.status is ScenarioStatus.DEGRADED
    assert scenario.entry_posture is EntryPosture.WAIT_CONFIRMATION


def test_sc11_feature_time_change_without_update_is_not_unmodeled(us_instrument):
    base=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    as_of=NOW+timedelta(hours=1)
    quote=_quote(us_instrument,observed_at=as_of-timedelta(minutes=5),source="nasdaq")
    request=_mode_request(base,DecisionMode.PRE,quote=quote,as_of=as_of,news_sentiment=.1)
    scenario=ScenarioPlanner().build(request)
    assert scenario.current_overlay.unmodeled_fact_update is False
    assert scenario.current_overlay.fact_update_count == 0
    assert scenario.status is ScenarioStatus.READY


def test_sc11_news_update_records_all_actual_changed_features(us_instrument):
    as_of=NOW+timedelta(hours=1)
    item=NewsSnapshot(
        us_instrument,"new filing","finnhub",NOW+timedelta(minutes=10),
        NOW+timedelta(minutes=20),NOW+timedelta(minutes=20),None,False,None,None,None,
    )
    updates=build_fact_updates(NOW,as_of,(item,),None,None)
    assert len(updates) == 1
    assert updates[0].affected_features == tuple(sorted(REGISTERED_NEWS_FEATURES))


@pytest.mark.parametrize("source",["nasdaq","nasdaq.com","yfinance"])
def test_sc12_us_pre_accepts_only_recognized_extended_sources(us_instrument,source):
    base=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    quote=_quote(us_instrument,observed_at=NOW-timedelta(minutes=5),source=source)
    scenario=ScenarioPlanner().build(_mode_request(base,DecisionMode.PRE,quote=quote))
    assert scenario.current_overlay.price_state is CurrentPriceState.FRESH_QUOTE
    assert scenario.current_overlay.price_source == source
    assert scenario.origin_session_date == base.origin_snapshot.latest_bar_date
    assert scenario.horizon_assessments[0].forecast_event_key == base.forecasts[0].event_key


def test_sc12_us_pre_rejects_tickflow_even_when_timestamp_is_fresh(us_instrument):
    base=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    quote=_quote(us_instrument,observed_at=NOW-timedelta(minutes=1),source="tickflow")
    scenario=ScenarioPlanner().build(_mode_request(base,DecisionMode.PRE,quote=quote))
    assert scenario.current_overlay.price_state is CurrentPriceState.STALE_OR_MISSING
    assert scenario.status is ScenarioStatus.DEGRADED
    assert scenario.entry_posture is EntryPosture.WAIT_CONFIRMATION


def test_sc13_a_pre_expected_missing_preserves_forecast_support(a_instrument):
    base=_request(a_instrument,[_forecast(a_instrument,h) for h in (1,3,5,10)])
    scenario=ScenarioPlanner().build(_mode_request(base,DecisionMode.PRE))
    assert scenario.current_overlay.price_state is CurrentPriceState.EXPECTED_MISSING
    assert scenario.forecast_support is ForecastSupportLevel.CONFIRMED
    assert scenario.status is ScenarioStatus.READY
    assert scenario.entry_posture is EntryPosture.WAIT_CONFIRMATION


def test_sc14_intraday_stale_or_wrong_source_blocks_new_entries(us_instrument):
    base=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    as_of=base.decision_session.regular_open+timedelta(minutes=30)
    missing=ScenarioPlanner().build(_mode_request(base,DecisionMode.INTRADAY,as_of=as_of))
    wrong_quote=_quote(us_instrument,observed_at=as_of-timedelta(minutes=1),session=TradingSession.REGULAR,source="nasdaq")
    wrong=ScenarioPlanner().build(_mode_request(base,DecisionMode.INTRADAY,quote=wrong_quote,as_of=as_of))
    assert missing.status is ScenarioStatus.BLOCKED
    assert wrong.current_overlay.price_state is CurrentPriceState.STALE_OR_MISSING
    assert wrong.status is ScenarioStatus.BLOCKED
    assert wrong.entry_posture is EntryPosture.BLOCKED
    assert StrategyFamily.PROTECTIVE_EXIT in wrong.allowed_strategy_families


@pytest.mark.parametrize("instrument_fixture",["us_instrument","a_instrument"])
def test_sc14_intraday_tickflow_quote_is_accepted_for_both_markets(request,instrument_fixture):
    instrument=request.getfixturevalue(instrument_fixture)
    base=_request(instrument,[_forecast(instrument,h) for h in (1,3,5,10)])
    as_of=base.decision_session.regular_open+timedelta(minutes=30)
    quote=_quote(instrument,observed_at=as_of-timedelta(minutes=1),session=TradingSession.REGULAR,source="tickflow")
    scenario=ScenarioPlanner().build(_mode_request(base,DecisionMode.INTRADAY,quote=quote,as_of=as_of))
    assert scenario.current_overlay.price_state is CurrentPriceState.FRESH_QUOTE
    assert scenario.status is ScenarioStatus.READY
