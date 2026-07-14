import time
from tradehelper_v2.scenario import ScenarioPlanner
from test_scenario_planner import _forecast, _request

def test_sc21_planner_is_pure_memory_fast(us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)]); planner=ScenarioPlanner(); start=time.perf_counter()
    for _ in range(1000): planner.build(request)
    assert time.perf_counter()-start < 1.0
