from contracts import HorizonAlignment, ScenarioBias, ScenarioState
from scenario import ScenarioPlanner
from test_scenario_planner import _forecast, _request

def test_sc18_market_independent_aggregation(a_instrument, us_instrument):
    a=ScenarioPlanner().build(_request(a_instrument,[_forecast(a_instrument,h) for h in (1,3,5,10)]))
    us=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)]))
    assert (a.bias,a.alignment,a.state)==(us.bias,us.alignment,us.state)==(ScenarioBias.BULLISH,HorizonAlignment.ALIGNED,ScenarioState.BULLISH_CONTINUATION)
    assert a.status == us.status
    assert a.entry_posture == us.entry_posture
    assert a.exit_posture == us.exit_posture
    assert a.allowed_strategy_families == us.allowed_strategy_families
