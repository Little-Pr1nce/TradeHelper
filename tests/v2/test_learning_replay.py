"""LE50：OOF 只能使用顺序、purged 的时间折和固定主链。"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from learning_replay_helpers import linked_full_chain_runner
from tradehelper_v2.contracts import ContractViolation, EvidenceOrigin, Market, OutcomeStatus, stable_hash
from tradehelper_v2.learning.replay import FoldDefinition, ReplayAccountPolicy, WalkForwardReplayer, validate_folds


def _fold(index, training_keys=()):
    train_start=date(2024,1,1); offset=timedelta(days=index*20); train_end=date(2024,1,10)+offset
    embargo_start=train_end+timedelta(days=1); embargo_end=embargo_start+timedelta(days=9)
    test_start=embargo_end+timedelta(days=1); test_end=test_start+timedelta(days=4)
    training_hash=stable_hash(tuple(training_keys))
    payload={"market":Market.US,"scope":"stock","scope_key":"US:XNAS:AAPL","train":(train_start,train_end),"embargo":(embargo_start,embargo_end),"test":(test_start,test_end),"cutoff":train_end,"training":training_hash}
    return FoldDefinition(stable_hash(payload),Market.US,"stock","US:XNAS:AAPL",train_start,train_end,embargo_start,embargo_end,test_start,test_end,train_end,training_hash)


def _event(key, origin, target, *, status=OutcomeStatus.MATURED, available=None):
    return SimpleNamespace(
        event_key=key, origin_session_date=origin, target_session_date=target, status=status,
        available_at=available or datetime.combine(target,datetime.min.time(),tzinfo=timezone.utc),
    )


def _runner(seen):
    return linked_full_chain_runner(seen)


def test_replay_requires_three_sequential_folds():
    assert len(validate_folds((_fold(0),_fold(1),_fold(2))))==3


def test_replay_account_policy_requires_explicit_non_default_account_path():
    policy=ReplayAccountPolicy('replay_account_policy_v1','standardized_research_notional',Decimal('100000'),'USD',())
    assert policy.initial_cash==Decimal('100000')


def test_walk_forward_replayer_only_selects_matured_available_training_prefix():
    training=(
        _event("e1",date(2024,1,2),date(2024,1,4)),
        _event("e2",date(2024,2,6),date(2024,2,8)),
        _event("future-known",date(2024,1,3),date(2024,1,5),available=datetime(2025,1,1,tzinfo=timezone.utc)),
        _event("pending",date(2024,1,4),date(2024,1,5),status=OutcomeStatus.PENDING),
    )
    folds=(_fold(0,("e1",)),_fold(1,("e1",)),_fold(2,("e1","e2")))
    tests=tuple(
        _event(
            f"test-{index}",
            fold.test_start,
            fold.test_end,
            status=OutcomeStatus.PENDING,
        )
        for index,fold in enumerate(folds)
    )
    seen=[]
    policy=ReplayAccountPolicy('replay_account_policy_v1','standardized_research_notional',Decimal('100000'),'USD',())
    WalkForwardReplayer().run(folds,training+tests,_runner(seen),account_policy=policy)
    assert [tuple(item.event_key for item in selected) for _,selected,_ in seen] == [("e1",),("e1",),("e1","e2")]


def test_walk_forward_replayer_rejects_arbitrary_callback_and_honors_cancellation():
    policy=ReplayAccountPolicy('replay_account_policy_v1','standardized_research_notional',Decimal('100000'),'USD',())
    with pytest.raises(ContractViolation):
        WalkForwardReplayer().run((_fold(0),_fold(1),_fold(2)),(),lambda *_: (),account_policy=policy)
    called=[]
    WalkForwardReplayer().run((_fold(0),_fold(1),_fold(2)),(),_runner(called),account_policy=policy,cancelled=lambda: True)
    assert called==[]


def test_full_chain_runner_rejects_cross_layer_identity_mismatch():
    fold=_fold(0)
    event=_event("test",fold.test_start,fold.test_end,status=OutcomeStatus.PENDING)
    policy=ReplayAccountPolicy('replay_account_policy_v1','standardized_research_notional',Decimal('100000'),'USD',())
    with pytest.raises(ContractViolation,match="scenario stage"):
        linked_full_chain_runner(corrupt_scenario=True).run_fold(fold,(),(event,),policy)
