"""LE03/LE04/LE59：学习事实幂等、冲突隔离与重启恢复。"""
from pathlib import Path
from dataclasses import replace

import pytest
from data.repository import SQLiteRepository
from learning import LearningEngine, MaturityResolver
from test_learning_smoke import _forecast
from contracts import AdjustmentMode, CanonicalBar
from contracts import ContractViolation, LedgerKind, LearningMetricSnapshot, Market, stable_hash
from learning.replay import FoldDefinition

def test_maturity_evidence_round_trips_and_is_idempotent(tmp_path,us_instrument,now):
    forecast=_forecast(us_instrument,now)
    bar=CanonicalBar(us_instrument,forecast.target_session_date,100.,102.,99.,101.,100,AdjustmentMode.FRONT_ADJUSTED,'fixture',now)
    evidence=MaturityResolver().resolve(forecast,(bar,),evaluated_at=now)
    path=Path(tmp_path)/'learning.sqlite'; repository=SQLiteRepository(path)
    try:
        assert repository.save_maturity_evidence(evidence).inserted==1
        assert repository.save_maturity_evidence(evidence).idempotent==1
        assert repository.get_maturity_evidence(evidence.evidence_id)==evidence
        outcome=LearningEngine().evaluate_forecast(forecast,(bar,),evaluated_at=now)
        assert repository.save_forecast_outcome(outcome).inserted==1
        assert repository.get_forecast_outcome(outcome.forecast_outcome_id)==outcome
        revised=CanonicalBar(us_instrument,forecast.target_session_date,100.,104.,99.,103.,100,AdjustmentMode.FRONT_ADJUSTED,'revision',now)
        revision=MaturityResolver().resolve(forecast,(revised,),evaluated_at=now,previous=evidence)
        assert repository.save_maturity_evidence(revision).inserted==1
        assert repository.list_active_maturity_evidence(us_instrument)==(revision,)
    finally: repository.close()
    reopened=SQLiteRepository(path)
    try:
        recovered=reopened.get_maturity_evidence(evidence.evidence_id)
        assert recovered is not None and recovered.status.value == 'superseded'
    finally: reopened.close()


def test_learning_metric_and_fold_round_trip(tmp_path, now):
    metrics=(("brier", .12), ("ece", .03)); snapshot=LearningMetricSnapshot(
        stable_hash({"ledger":LedgerKind.FORECAST,"scope":"US:XNAS:AAPL|1","cutoff":now,"sample_count":20,"metrics":metrics}),
        LedgerKind.FORECAST,"US:XNAS:AAPL|1",now,20,metrics,now,
    )
    train_start=now.date()-__import__('datetime').timedelta(days=60); train_end=now.date()-__import__('datetime').timedelta(days=40)
    embargo_start=train_end+__import__('datetime').timedelta(days=1); embargo_end=embargo_start+__import__('datetime').timedelta(days=9); test_start=embargo_end+__import__('datetime').timedelta(days=1); test_end=test_start+__import__('datetime').timedelta(days=4)
    payload={"market":Market.US,"scope":"stock","scope_key":"US:XNAS:AAPL","train":(train_start,train_end),"embargo":(embargo_start,embargo_end),"test":(test_start,test_end),"cutoff":train_end,"training":"a"*64}
    fold=FoldDefinition(stable_hash(payload),Market.US,"stock","US:XNAS:AAPL",train_start,train_end,embargo_start,embargo_end,test_start,test_end,train_end,"a"*64)
    repository=SQLiteRepository(Path(tmp_path)/"metric-fold.sqlite")
    try:
        assert repository.save_learning_metric_snapshot(snapshot).inserted == 1
        assert repository.get_learning_metric_snapshot(snapshot.snapshot_id) == snapshot
        later=now+__import__('datetime').timedelta(days=1)
        later_identity={"ledger":LedgerKind.FORECAST,"scope":snapshot.scope_key,"cutoff":later,"sample_count":30,"metrics":metrics}
        later_snapshot=LearningMetricSnapshot(stable_hash(later_identity),LedgerKind.FORECAST,snapshot.scope_key,later,30,metrics,later)
        repository.save_learning_metric_snapshot(later_snapshot)
        assert repository.list_latest_learning_metric_snapshots("US:XNAS:AAPL") == (later_snapshot,)
        assert repository.save_learning_fold("run",fold,generated_at=now).inserted == 1
        assert repository.get_learning_fold(fold.fold_id) == fold
    finally: repository.close()


def test_same_target_session_keeps_distinct_forecast_origins(tmp_path, us_instrument, now):
    _,_,first=_maturity_for_repository(us_instrument,now)
    earlier_origin=first.origin_session_date-__import__('datetime').timedelta(days=1)
    identity={
        "instrument":first.instrument,
        "origin":earlier_origin,
        "target":first.target_session_date,
        "reference_adjustment_mode":first.reference_adjustment_mode,
        "reference":first.reference_price,
        "target_bar_key":first.target_bar_key,
        "target_price":first.target_price,
        "revision":first.revision,
        "supersedes":first.supersedes_evidence_id,
    }
    second=__import__('dataclasses').replace(first,origin_session_date=earlier_origin,evidence_id=stable_hash(identity))
    repository=SQLiteRepository(Path(tmp_path)/"origin-identity.sqlite")
    try:
        assert repository.save_maturity_evidence(first).inserted == 1
        assert repository.save_maturity_evidence(second).inserted == 1
        active=repository.list_active_maturity_evidence(us_instrument)
        assert {item.origin_session_date for item in active} == {first.origin_session_date,earlier_origin}
    finally:
        repository.close()


def test_maturity_revision_cannot_skip_or_branch_from_superseded_fact(
    tmp_path,
    us_instrument,
    now,
):
    forecast,_,first=_maturity_for_repository(us_instrument,now)
    revised_bar=CanonicalBar(
        us_instrument,forecast.target_session_date,100.,104.,99.,103.,100,
        AdjustmentMode.FRONT_ADJUSTED,"revision",now,
    )
    second=MaturityResolver().resolve(forecast,(revised_bar,),evaluated_at=now,previous=first)
    repository=SQLiteRepository(Path(tmp_path)/"revision-chain.sqlite")
    try:
        repository.save_maturity_evidence(first)
        repository.save_maturity_evidence(second)
        invalid_identity={
            "instrument":second.instrument,
            "origin":second.origin_session_date,
            "target":second.target_session_date,
            "reference_adjustment_mode":second.reference_adjustment_mode,
            "reference":second.reference_price,
            "target_bar_key":second.target_bar_key,
            "target_price":second.target_price,
            "revision":4,
            "supersedes":first.evidence_id,
        }
        invalid=replace(second,revision=4,supersedes_evidence_id=first.evidence_id,evidence_id=stable_hash(invalid_identity))
        with pytest.raises(ContractViolation):
            repository.save_maturity_evidence(invalid)
        assert repository.get_maturity_evidence(invalid.evidence_id) is None
    finally:
        repository.close()


def _maturity_for_repository(instrument, now):
    forecast=_forecast(instrument,now)
    bar=CanonicalBar(instrument,forecast.target_session_date,100.,102.,99.,101.,100,AdjustmentMode.FRONT_ADJUSTED,'fixture',now)
    return forecast,bar,MaturityResolver().resolve(forecast,(bar,),evaluated_at=now)
