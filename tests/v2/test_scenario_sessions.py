from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from tradehelper_v2.contracts import DecisionMode, DecisionSession, Exchange, Market, TradingSession
from tradehelper_v2.data.calendar import StaticTradingCalendar, TradingCalendarUnavailable
from tradehelper_v2.scenario import ScenarioPlanner
from test_scenario_planner import _forecast, _mode_request, _quote, _request

def test_sc15_injected_session_window_preserves_breaks_and_next_session():
    session=DecisionSession(Market.A,Exchange.XSHG,date(2026,7,13),datetime(2026,7,13,1,30,tzinfo=timezone.utc),datetime(2026,7,13,7,tzinfo=timezone.utc),((datetime(2026,7,13,3,30,tzinfo=timezone.utc),datetime(2026,7,13,5,tzinfo=timezone.utc)),),"fixture")
    calendar=StaticTradingCalendar((date(2026,7,10),date(2026,7,13)),windows={(Market.A,Exchange.XSHG,date(2026,7,13)):session})
    assert calendar.next_session(Market.A,Exchange.XSHG,date(2026,7,10)) == date(2026,7,13)
    assert calendar.session_window(Market.A,Exchange.XSHG,date(2026,7,13)).breaks == session.breaks

def test_sc15_half_day_close_controls_scenario_expiry(us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    half_day=replace(request.decision_session,regular_close=request.decision_session.regular_open+timedelta(hours=3,minutes=30),source="half-day-fixture")
    scenario=ScenarioPlanner().build(replace(request,decision_session=half_day))
    assert scenario.valid_from == half_day.regular_open
    assert scenario.expires_at == half_day.regular_close

def test_sc15_intraday_break_moves_valid_from_to_next_segment(a_instrument):
    request=_request(a_instrument,[_forecast(a_instrument,h) for h in (1,3,5,10)])
    break_start=request.decision_session.regular_open+timedelta(hours=2)
    break_end=break_start+timedelta(hours=1,minutes=30)
    session=replace(request.decision_session,breaks=((break_start,break_end),))
    as_of=break_start+timedelta(minutes=10)
    quote=_quote(a_instrument,observed_at=as_of-timedelta(minutes=1),session=TradingSession.REGULAR,source="tickflow")
    intraday=_mode_request(replace(request,decision_session=session),DecisionMode.INTRADAY,quote=quote,as_of=as_of)
    scenario=ScenarioPlanner().build(intraday)
    assert scenario.valid_from == break_end
    assert scenario.expires_at == session.regular_close

def test_sc15_calendar_failure_is_not_silently_reported_as_closed():
    calendar=StaticTradingCalendar((date(2026,7,13),))
    as_of=datetime(2026,7,13,15,tzinfo=timezone.utc)
    with pytest.raises(TradingCalendarUnavailable):
        calendar.session_containing(Market.US,Exchange.XNAS,as_of)
