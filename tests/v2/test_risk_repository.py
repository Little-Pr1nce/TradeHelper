from datetime import timedelta
import json

from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.risk import RiskOfficer
from risk_helpers import request_for


def test_rk38_risk_records_are_idempotent_and_restore(tmp_path, us_instrument):
    request = request_for(us_instrument); bundle = RiskOfficer().assess(request, generated_at=request.as_of)
    repo = SQLiteRepository(tmp_path / "risk.db")
    assert repo.save_frozen_account_valuation(request.valuation).inserted == 1
    assert repo.get_frozen_account_valuation(request.valuation.valuation_id) == request.valuation
    decision = bundle.decisions[0]
    assert repo.save_execution_decision(decision).inserted == 1
    assert repo.get_execution_decision(decision.decision_id) == decision
    assert repo.save_risk_decision_bundle(bundle).inserted == 1
    assert repo.get_risk_decision_bundle(bundle.risk_bundle_id) == bundle
    repo.close()
    reopened = SQLiteRepository(tmp_path / "risk.db")
    assert reopened.get_risk_decision_bundle(bundle.risk_bundle_id) == bundle
    reopened.close()


def test_rk38_conflicting_stored_payload_is_quarantined_not_overwritten(tmp_path, us_instrument):
    request = request_for(us_instrument); bundle = RiskOfficer().assess(request, generated_at=request.as_of)
    decision = bundle.decisions[0]
    repo = SQLiteRepository(tmp_path / "risk-conflict.db")
    assert repo.save_execution_decision(decision).inserted == 1
    payload = json.loads(repo._connection.execute(
        "SELECT payload_json FROM execution_decisions WHERE decision_id=?", (decision.decision_id,)
    ).fetchone()[0])
    payload["reason_codes"] = ["RISK_EVIDENCE_CONFLICT"]
    repo._connection.execute(
        "UPDATE execution_decisions SET payload_json=? WHERE decision_id=?",
        (json.dumps(payload, sort_keys=True), decision.decision_id),
    )
    repo._connection.commit()
    result = repo.save_execution_decision(decision)
    assert result.conflicts == 1 and result.inserted == 0
    assert repo._connection.execute(
        "SELECT COUNT(*) FROM quarantine_records WHERE record_type='execution_decision_conflict'"
    ).fetchone()[0] == 1
    repo.close()
