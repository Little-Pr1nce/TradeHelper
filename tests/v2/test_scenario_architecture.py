import ast
import json
from pathlib import Path

from tradehelper_v2.contracts import canonical_json
from tradehelper_v2.scenario import ScenarioPlanner
from test_scenario_planner import _forecast, _request

ROOT=Path(__file__).parents[2] / "tradehelper_v2" / "scenario"
def test_sc20_scenario_layer_has_no_strategy_or_v1_imports():
    forbidden={"core","services","strategies","risk","execution","portfolio","learning","report","ui","openai"}
    found=[]
    paths=tuple(ROOT.glob("*.py"))+(ROOT.parent / "contracts" / "scenario.py",)
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            module=getattr(node,"module","") or ""
            if module.split(".")[0] in forbidden: found.append(module)
    assert found==[]

def test_sc20_serialized_scenario_contains_no_trade_or_account_fields(us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)]))
    payload=json.loads(canonical_json(scenario))
    forbidden={"action","trigger_price","stop_loss","take_profit","position_pct","shares","cash","max_loss_amount","account_equity"}
    def keys(value):
        if isinstance(value,dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value,list):
            return set().union(*(keys(item) for item in value)) if value else set()
        return set()
    assert keys(payload).isdisjoint(forbidden)
