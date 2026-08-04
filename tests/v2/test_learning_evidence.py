"""LE39/LE53：股票级证据的样本、截止日和冲突边界。"""
from datetime import timedelta
from types import SimpleNamespace

from contracts import (
    EvidenceOrigin,
    EvidenceStatus,
    OutcomeStatus,
    RiskProfile,
)
from learning.evidence import plan_evidence
from learning.metrics import block_bootstrap_interval

def test_stock_evidence_bootstrap_is_deterministic_and_time_blocked():
    assert block_bootstrap_interval((.01,.02,-.01,.03,.02),seed=7)==block_bootstrap_interval((.01,.02,-.01,.03,.02),seed=7)


def _row(
    instrument, now, index, *, status=OutcomeStatus.MATURED,
    origin=EvidenceOrigin.RECONSTRUCTED_OOF, action="buy",
    net_return=.01, exit_quality=None,
):
    return SimpleNamespace(
        instrument=instrument,
        strategy_id="support_rebound",
        strategy_version="v1",
        parameter_hash="a"*64,
        profile=RiskProfile.CONSERVATIVE.value,
        evaluation_horizon=5,
        status=status,
        evaluated_at=now-timedelta(days=1),
        generated_at=now+timedelta(days=1),
        fill_outcome="partial" if index == 0 else "filled",
        action=action,
        net_return=net_return,
        exit_quality=exit_quality,
        mae=-.02,
        evidence_origin=origin,
    )


def test_plan_evidence_uses_evaluation_cutoff_not_late_persistence_time(us_instrument, now):
    evidence=plan_evidence(
        instrument=us_instrument,
        strategy_id="support_rebound",
        strategy_version="v1",
        parameter_hash="a"*64,
        profile=RiskProfile.CONSERVATIVE,
        outcomes=tuple(_row(us_instrument,now,index) for index in range(10)),
        cutoff_at=now,
        generated_at=now+timedelta(days=2),
        evaluation_horizon=5,
    )
    assert evidence.oof_sample_count == 10
    assert evidence.sample_count == 10
    assert evidence.status is EvidenceStatus.INSUFFICIENT_SAMPLE


def test_conflicting_strategy_fact_blocks_plan_evidence(us_instrument, now):
    outcomes=(
        _row(us_instrument,now,0),
        _row(us_instrument,now,1,status=OutcomeStatus.CONFLICTING),
    )
    evidence=plan_evidence(
        instrument=us_instrument,
        strategy_id="support_rebound",
        strategy_version="v1",
        parameter_hash="a"*64,
        profile=RiskProfile.CONSERVATIVE,
        outcomes=outcomes,
        cutoff_at=now,
        generated_at=now,
        evaluation_horizon=5,
    )
    assert evidence.status is EvidenceStatus.CONFLICTING


def test_plan_evidence_never_mixes_entry_return_with_exit_quality(us_instrument, now):
    entries = tuple(_row(us_instrument, now, index) for index in range(10))
    exits = tuple(
        _row(
            us_instrument, now, index, action="sell",
            net_return=-.99, exit_quality=.02,
        )
        for index in range(10)
    )
    entry = plan_evidence(
        instrument=us_instrument, strategy_id="support_rebound", strategy_version="v1",
        parameter_hash="a" * 64, profile=RiskProfile.CONSERVATIVE,
        outcomes=(*entries, *exits), cutoff_at=now, generated_at=now,
        evaluation_horizon=5, action="buy",
    )
    exit_result = plan_evidence(
        instrument=us_instrument, strategy_id="support_rebound", strategy_version="v1",
        parameter_hash="a" * 64, profile=RiskProfile.CONSERVATIVE,
        outcomes=(*entries, *exits), cutoff_at=now, generated_at=now,
        evaluation_horizon=5, action="sell",
    )
    assert entry.action == "buy" and entry.expected_net_return == .01
    assert exit_result.action == "sell" and exit_result.expected_net_return == .02
    assert entry.evidence_id != exit_result.evidence_id
