from datetime import timedelta
import json

import pytest

from tradehelper_v2.contracts import ContractViolation
from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.scenario import ScenarioPlanner
from test_scenario_planner import NOW, _forecast, _request


def test_sc19_scenario_persistence_is_idempotent_across_restart(tmp_path,us_instrument):
    request=_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)])
    first=ScenarioPlanner().build(request,generated_at=NOW)
    second=ScenarioPlanner().build(request,generated_at=NOW+timedelta(seconds=1))
    path=tmp_path/"v2.db"
    repo=SQLiteRepository(path)
    assert repo.save_trading_scenario(first).inserted == 1
    assert repo.save_trading_scenario(second).idempotent == 1
    repo.close()

    reopened=SQLiteRepository(path)
    restored=reopened.get_trading_scenario(first.scenario_id)
    assert restored is not None and restored.scenario_id == first.scenario_id
    assert reopened.list_trading_scenarios(us_instrument,first.mode,first.decision_session.session_date)[0].event_key == first.event_key
    reopened.close()


def test_sc19_conflicting_existing_event_is_quarantined_without_overwrite(tmp_path,us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)]))
    path=tmp_path/"conflict.db"
    repo=SQLiteRepository(path)
    assert repo.save_trading_scenario(scenario).inserted == 1
    row=repo._fetchone("SELECT payload_json FROM trading_scenarios WHERE scenario_id=?",(scenario.scenario_id,))
    payload=json.loads(row["payload_json"])
    payload["bias"]="bearish"
    with repo._transaction() as connection:
        connection.execute("UPDATE trading_scenarios SET payload_json=? WHERE scenario_id=?",(json.dumps(payload),scenario.scenario_id))
    result=repo.save_trading_scenario(scenario)
    assert result.conflicts == 1
    quarantine=repo._fetchone("SELECT COUNT(*) AS count FROM quarantine_records WHERE record_type='trading_scenario_conflict'",())
    assert quarantine["count"] == 1
    repo.close()


def test_sc19_read_rejects_index_column_payload_mismatch(tmp_path,us_instrument):
    scenario=ScenarioPlanner().build(_request(us_instrument,[_forecast(us_instrument,h) for h in (1,3,5,10)]))
    repo=SQLiteRepository(tmp_path/"corrupt.db")
    repo.save_trading_scenario(scenario)
    with repo._transaction() as connection:
        connection.execute("UPDATE trading_scenarios SET quality_hash=? WHERE scenario_id=?",("0"*64,scenario.scenario_id))
    with pytest.raises(ContractViolation):
        repo.get_trading_scenario(scenario.scenario_id)
    repo.close()
