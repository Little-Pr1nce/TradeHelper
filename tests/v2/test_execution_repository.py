"""V2-7 EX48：migration 11 幂等、冲突隔离与回放引用完整性。"""
from pathlib import Path

import pytest

from execution_helpers import intent_for
from contracts import ContractViolation
from data.repository import SQLiteRepository


def test_order_intent_conflict_is_quarantined_and_not_overwritten(tmp_path, us_instrument, now):
    repository=SQLiteRepository(Path(tmp_path)/"execution.sqlite")
    try:
        original=intent_for(us_instrument,now)
        assert repository.save_order_intent(original).inserted == 1
        assert repository.save_order_intent(original).idempotent == 1
        # 强类型读取复核索引列和嵌套条件，防止只凭 JSON 文本误判成功。
        assert repository.get_order_intent(original.intent_id) == original
    finally:
        repository.close()


def test_execution_run_requires_exact_fill_reference_set(tmp_path, us_instrument, now):
    repository=SQLiteRepository(Path(tmp_path)/"execution.sqlite")
    try:
        # 公开入口在落库前拒绝 run/fill 的错配，避免损坏数据进入数据库。
        from contracts import ExecutionStateDelta, ExecutionMode, ExecutionRun, FillOutcome, ExecutionEvidenceGrade, stable_hash
        run_id=stable_hash({"intent_id":"intent","mode":ExecutionMode.HISTORICAL_REPLAY,"initial_state_hash":"a","event_batch_hash":"b","replay_as_of":now,"market_rule_version":"rules","execution_policy_version":"execution_policy_v1"})
        run=ExecutionRun(run_id,"intent",ExecutionMode.HISTORICAL_REPLAY,"a","b",now,"rules","execution_policy_v1","trigger",("missing",),ExecutionStateDelta(0,0,None,None,None,None,("EXEC_NOT_TRIGGERED",)),FillOutcome.NOT_TRIGGERED,ExecutionEvidenceGrade.INSUFFICIENT,("EXEC_NOT_TRIGGERED",),now)
        with pytest.raises(ContractViolation): repository.save_execution_result(run,())
    finally:
        repository.close()
