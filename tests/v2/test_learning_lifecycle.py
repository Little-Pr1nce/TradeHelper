"""LE54-LE58：受控候选边界、阶段晋升和 drift 行为。"""
from decimal import Decimal
from pathlib import Path

import pytest
from tradehelper_v2.contracts import (
    CandidateKind,
    CandidateLifecycle,
    CandidateScope,
    ContractViolation,
    EvidenceOrigin,
    LearningCandidateVersion,
    PromotionDecision,
    PromotionEvent,
    stable_hash,
)
from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.learning.lifecycle import next_lifecycle
from tradehelper_v2.learning.optimizer import validate_candidate_parameters
from tradehelper_v2.learning.optimizer import forecast_promotion_decision, strategy_promotion_decision

def test_candidate_space_rejects_unknown_or_out_of_bound_fields():
    space={'threshold':{'minimum':Decimal('0.1'),'maximum':Decimal('0.5'),'step':Decimal('0.1')}}
    assert validate_candidate_parameters(space,{'threshold':Decimal('0.2')})==(('threshold',Decimal('0.2')),)
    with pytest.raises(ContractViolation): validate_candidate_parameters(space,{'threshold':Decimal('0.55')})

def test_lifecycle_requires_shadow_before_champion_and_preserves_drift():
    assert next_lifecycle(CandidateLifecycle.CANDIDATE,PromotionDecision.PROMOTE_TO_CHALLENGER) is CandidateLifecycle.CHALLENGER
    assert next_lifecycle(CandidateLifecycle.CHALLENGER,PromotionDecision.PROMOTE_TO_SHADOW) is CandidateLifecycle.SHADOW
    assert next_lifecycle(CandidateLifecycle.SHADOW,PromotionDecision.PROMOTE_TO_CHAMPION) is CandidateLifecycle.CHAMPION
    assert next_lifecycle(CandidateLifecycle.CHAMPION,PromotionDecision.SUSPEND_NEW_RISK) is CandidateLifecycle.DRIFTED

def test_promotion_keeps_calibration_and_drawdown_guardrails():
    assert forecast_promotion_decision(paired_brier_improvement=.01,log_loss_ratio=1.03,ece=.1,baseline_ece=.1,interval_coverage=.8,confirmation_samples=25,direction_classes=('up','down'))=='reject'
    assert strategy_promotion_decision(filled_oof_samples=30,fold_excess_returns=(.01,.02,.01),mean_net_return=.01,bootstrap_lower_80=0,baseline_return=.1,candidate_return=.09,drawdown_reduction=.3,sharpe_improvement=.2)=='promote_to_challenger'


def _candidate(instrument, now, parameter_hash, lifecycle):
    projection=f"{instrument.stable_key}|h5|forecast"
    identity={
        "kind":CandidateKind.FORECAST_CONFIGURATION,
        "scope":CandidateScope.STOCK,
        "scope_key":instrument.stable_key,
        "market":instrument.market,
        "profile":None,
        "base_version":"forecast-v1",
        "parameter_hash":parameter_hash,
        "search_space_hash":"f"*64,
        "origin":EvidenceOrigin.RECONSTRUCTED_OOF,
        "lifecycle":lifecycle,
        "projection_key":projection,
    }
    return LearningCandidateVersion(
        stable_hash(identity),
        CandidateKind.FORECAST_CONFIGURATION,
        CandidateScope.STOCK,
        instrument.stable_key,
        instrument.market,
        None,
        "forecast-v1",
        parameter_hash,
        "f"*64,
        lifecycle,
        EvidenceOrigin.RECONSTRUCTED_OOF,
        now,
        now,
        ("LEARNING_CANDIDATE_WITHIN_BOUNDS",),
        projection,
    )


def _promotion(previous, candidate, decision, now, *, deployment=None, samples=20, guarded=True):
    reasons={
        PromotionDecision.PROMOTE_TO_CHALLENGER:("LEARNING_SELECTION_PASSED",),
        PromotionDecision.PROMOTE_TO_SHADOW:("LEARNING_CONFIRMATION_PASSED",),
        PromotionDecision.PROMOTE_TO_CHAMPION:("LEARNING_SHADOW_PASSED","LEARNING_PROMOTED"),
        PromotionDecision.ROLLBACK:("LEARNING_ROLLED_BACK",),
    }[decision]
    identity={
        "candidate":candidate.candidate_id,
        "projection":candidate.projection_key,
        "decision":decision,
        "previous":previous.candidate_id,
        "deployment_candidate_id":deployment,
        "evidence":"e"*64,
        "samples":samples,
        "hard_guardrails_ok":guarded,
        "decided_at":now,
        "reasons":tuple(sorted(reasons)),
    }
    return PromotionEvent(
        stable_hash(identity),
        candidate.candidate_id,
        candidate.projection_key,
        decision,
        previous.candidate_id,
        "e"*64,
        now,
        now,
        reasons,
        samples,
        guarded,
        deployment,
    )


def _promote_chain(repository, instrument, now, parameter_hash):
    current=_candidate(instrument,now,parameter_hash,CandidateLifecycle.CANDIDATE)
    repository.save_learning_candidate(current)
    for decision,lifecycle in (
        (PromotionDecision.PROMOTE_TO_CHALLENGER,CandidateLifecycle.CHALLENGER),
        (PromotionDecision.PROMOTE_TO_SHADOW,CandidateLifecycle.SHADOW),
        (PromotionDecision.PROMOTE_TO_CHAMPION,CandidateLifecycle.CHAMPION),
    ):
        candidate=_candidate(instrument,now,parameter_hash,lifecycle)
        deployment=candidate.candidate_id if lifecycle is CandidateLifecycle.CHAMPION else None
        repository.promote_learning_candidate(
            candidate,
            _promotion(current,candidate,decision,now,deployment=deployment),
        )
        current=candidate
    return current


def test_repository_cannot_bypass_lifecycle_and_rollback_redeploys_previous_champion(tmp_path, us_instrument, now):
    repository=SQLiteRepository(Path(tmp_path)/"lifecycle.sqlite")
    try:
        with pytest.raises(ContractViolation):
            repository.save_learning_candidate(
                _candidate(us_instrument,now,"0"*64,CandidateLifecycle.CHAMPION)
            )
        previous=_promote_chain(repository,us_instrument,now,"1"*64)
        current=_promote_chain(repository,us_instrument,now,"2"*64)
        assert repository.get_learning_deployment(current.projection_key)[0].candidate_id == current.candidate_id
        rolled_back=_candidate(us_instrument,now,"2"*64,CandidateLifecycle.ROLLED_BACK)
        repository.promote_learning_candidate(
            rolled_back,
            _promotion(
                current,
                rolled_back,
                PromotionDecision.ROLLBACK,
                now,
                deployment=previous.candidate_id,
                guarded=False,
            ),
        )
        deployment=repository.get_learning_deployment(current.projection_key)
        assert deployment is not None
        assert deployment[0].candidate_id == previous.candidate_id
    finally:
        repository.close()
