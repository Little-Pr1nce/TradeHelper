"""将成熟策略账投影为 V2-6 可消费的 PlanEvidenceSnapshot。"""
from __future__ import annotations

from statistics import mean

from contracts import (
    EvidenceOrigin,
    EvidenceStatus,
    OutcomeStatus,
    PlanEvidenceSnapshot,
    stable_hash,
)

from .metrics import block_bootstrap_interval


def _matches_identity(
    item,
    *,
    instrument,
    strategy_id,
    strategy_version,
    parameter_hash,
    profile,
    evaluation_horizon,
):
    return (
        item.instrument == instrument
        and item.strategy_id == strategy_id
        and item.strategy_version == strategy_version
        and item.parameter_hash == parameter_hash
        and item.profile == profile.value
        and (evaluation_horizon is None or item.evaluation_horizon == evaluation_horizon)
    )


def _evidence_status(count, expected, low, high):
    if expected is None:
        return EvidenceStatus.UNAVAILABLE
    if count < 10:
        return EvidenceStatus.UNAVAILABLE
    if high < 0 or (count >= 10 and expected < 0) or (count >= 30 and expected <= 0):
        return EvidenceStatus.NEGATIVE
    if count >= 30 and expected > 0 and low >= 0:
        return EvidenceStatus.RELIABLE_POSITIVE
    if 10 <= count <= 29:
        return EvidenceStatus.INSUFFICIENT_SAMPLE
    if count >= 30 and expected > 0:
        return EvidenceStatus.POSITIVE_UNCERTAIN
    return EvidenceStatus.UNAVAILABLE


def plan_evidence(
    *,
    instrument,
    strategy_id,
    strategy_version,
    parameter_hash,
    profile,
    outcomes,
    cutoff_at,
    generated_at,
    evaluation_horizon=None,
):
    """只使用同一股票/策略/profile/周期的成熟 OOF 成交结果计算证据。

    在线 issued 样本保留在 ``sample_count`` 中用于覆盖率观察，但不会进入
    可执行证据的收益、置信区间或 ``oof_sample_count``。
    """
    matching = tuple(
        item
        for item in outcomes
        if _matches_identity(
            item,
            instrument=instrument,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            parameter_hash=parameter_hash,
            profile=profile,
            evaluation_horizon=evaluation_horizon,
        )
        and getattr(item, "evaluated_at", item.generated_at) <= cutoff_at
    )
    sliced = tuple(item for item in matching if item.status is OutcomeStatus.MATURED)
    filled = tuple(
        item
        for item in sliced
        if item.fill_outcome in {"filled", "partial"} and item.net_return is not None
    )
    oof = tuple(item for item in filled if item.evidence_origin is EvidenceOrigin.RECONSTRUCTED_OOF)
    values = tuple(float(item.net_return) for item in oof)
    count = len(oof)
    expected = mean(values) if values else None
    interval = block_bootstrap_interval(values, seed=int(parameter_hash[:8], 16)) if values else None
    low = None if interval is None else interval[0]
    high = None if interval is None else interval[1]
    win_rate = None if not values else sum(value > 0 for value in values) / count
    adverse_values = tuple(float(item.mae) for item in oof if item.mae is not None)
    max_adverse = None if not adverse_values else min(adverse_values)
    status = _evidence_status(count, expected, low, high)
    if any(item.status is OutcomeStatus.CONFLICTING for item in matching):
        status = EvidenceStatus.CONFLICTING
    metrics = (expected, low, high, win_rate, max_adverse)
    if status is not EvidenceStatus.UNAVAILABLE and any(value is None for value in metrics):
        status = EvidenceStatus.CONFLICTING
    identity = {
        "instrument": instrument,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "parameter_hash": parameter_hash,
        "profile": profile,
        "sample_count": len(filled),
        "oof_sample_count": count,
        "metrics": metrics,
        "status": status,
        "source_ledger_version": "learning_ledger_v1",
        "data_cutoff_at": cutoff_at,
        "evaluated_at": cutoff_at,
    }
    return PlanEvidenceSnapshot(
        stable_hash(identity),
        instrument,
        strategy_id,
        strategy_version,
        parameter_hash,
        profile,
        len(filled),
        count,
        *metrics,
        status,
        "learning_ledger_v1",
        cutoff_at,
        cutoff_at,
        generated_at,
    )
