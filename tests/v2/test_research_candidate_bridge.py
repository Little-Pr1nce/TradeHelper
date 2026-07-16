"""LL30--LL39：研究只能创建受控 V2-9 candidate。"""
from types import SimpleNamespace
from tradehelper_v2.contracts import CandidateEligibility, CandidateLifecycle, HypothesisKind, HypothesisValidationStatus
from tradehelper_v2.research.bridge import CandidateBridge

def test_ll30_registered_model_creates_candidate(us_instrument,now):
    hypothesis=SimpleNamespace(hypothesis_id="h",kind=HypothesisKind.MODEL_CONFIGURATION,payload=(("registered_model_family","analog"),))
    validation=SimpleNamespace(candidate_eligibility=CandidateEligibility.ELIGIBLE_FOR_OOF,status=HypothesisValidationStatus.CONFIRMED)
    link,candidate=CandidateBridge().bridge(hypothesis,validation,market=us_instrument.market,scope_key=us_instrument.stable_key,base_version="base",search_space_hash="a"*64,created_at=now)
    assert candidate is not None and candidate.lifecycle is CandidateLifecycle.CANDIDATE and link.candidate_id==candidate.candidate_id

def test_ll32_current_observation_is_not_candidate(us_instrument,now):
    hypothesis=SimpleNamespace(hypothesis_id="h",kind=HypothesisKind.FORECAST_PATTERN,payload=())
    validation=SimpleNamespace(candidate_eligibility=CandidateEligibility.OBSERVATION_ONLY,status=HypothesisValidationStatus.CONFIRMED)
    link,candidate=CandidateBridge().bridge(hypothesis,validation,market=us_instrument.market,scope_key=us_instrument.stable_key,base_version="base",search_space_hash="a"*64,created_at=now)
    assert candidate is None and link.eligibility is CandidateEligibility.OBSERVATION_ONLY

def test_model_candidate_preserves_declared_scope_and_limit(us_instrument,now):
    hypothesis=SimpleNamespace(hypothesis_id="h",business_key="b",response_id="r",context_id="c",kind=HypothesisKind.MODEL_CONFIGURATION,payload=(("scope","market"),("registered_model_family","analog"),("registered_feature_set_id","tech"),("registered_hyperparameter_overrides",(("k",40),))))
    validation=SimpleNamespace(candidate_eligibility=CandidateEligibility.ELIGIBLE_FOR_OOF,status=HypothesisValidationStatus.CONFIRMED)
    link,candidate=CandidateBridge().bridge(hypothesis,validation,market=us_instrument.market,scope_key="US",base_version="base",search_space_hash="a"*64,created_at=now)
    assert candidate.scope.value=="market"
    limited,candidate=CandidateBridge().bridge(hypothesis,validation,market=us_instrument.market,scope_key="US",base_version="base",search_space_hash="a"*64,created_at=now,existing_candidate_count=20)
    assert candidate is None and limited.eligibility is CandidateEligibility.REJECTED


def test_stock_candidate_scope_is_bound_to_hypothesis_instrument(us_instrument,now):
    hypothesis=SimpleNamespace(hypothesis_id="h",business_key="b",instrument=us_instrument,kind=HypothesisKind.MODEL_CONFIGURATION,payload=(("scope","stock"),("registered_model_family","analog"),("resolved_scope_key",us_instrument.stable_key)))
    validation=SimpleNamespace(candidate_eligibility=CandidateEligibility.ELIGIBLE_FOR_OOF,status=HypothesisValidationStatus.CONFIRMED)
    _,candidate=CandidateBridge().bridge(hypothesis,validation,market=us_instrument.market,scope_key="WRONG",base_version="base",search_space_hash="a"*64,created_at=now)
    assert candidate.scope_key==us_instrument.stable_key
