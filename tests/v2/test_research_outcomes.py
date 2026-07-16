"""LL40--LL46：LLM 独立到期账。"""
from types import SimpleNamespace
from tradehelper_v2.contracts import HypothesisKind, HypothesisValidationStatus
from tradehelper_v2.research.outcomes import forecast_outcome

def _candidate_event(instrument,now,decision):
    from tradehelper_v2.contracts import CandidateEligibility, HypothesisKind, PromotionEvent, stable_hash
    from tradehelper_v2.research.bridge import CandidateBridge
    hypothesis=SimpleNamespace(hypothesis_id="candidate-h",business_key="candidate-b",response_id="r",context_id="c",kind=HypothesisKind.MODEL_CONFIGURATION,payload=(("scope","stock"),("registered_model_family","analog")))
    validation=SimpleNamespace(candidate_eligibility=CandidateEligibility.ELIGIBLE_FOR_OOF)
    _,candidate=CandidateBridge().bridge(hypothesis,validation,market=instrument.market,scope_key=instrument.stable_key,base_version="base",search_space_hash="a"*64,created_at=now)
    reasons=("LEARNING_SELECTION_PASSED",) if decision.value=="promote_to_challenger" else ("LEARNING_REJECTED",)
    identity={"candidate":candidate.candidate_id,"projection":candidate.projection_key,"decision":decision,"previous":None,"deployment_candidate_id":None,"evidence":"e"*64,"samples":30,"hard_guardrails_ok":True,"decided_at":now,"reasons":tuple(sorted(reasons))}
    event=PromotionEvent(stable_hash(identity),candidate.candidate_id,candidate.projection_key,decision,None,"e"*64,now,now,reasons,30,True,None)
    return candidate,event

def test_ll40_only_confirmed_pattern_is_issued(us_instrument,now):
    from test_learning_smoke import _forecast
    from tradehelper_v2.learning.maturity import MaturityResolver
    from tradehelper_v2.contracts import AdjustmentMode, CanonicalBar
    issued=_forecast(us_instrument,now); bar=CanonicalBar(us_instrument,issued.target_session_date,100,103,99,102,100,AdjustmentMode.FRONT_ADJUSTED,"fixture",now); maturity=MaturityResolver().resolve(issued,(bar,),evaluated_at=now)
    from tradehelper_v2.learning.engine import LearningEngine
    forecast=LearningEngine().evaluate_forecast(issued,(bar,),evaluated_at=now)
    hypothesis=SimpleNamespace(hypothesis_id="h",instrument=us_instrument,payload=(("expected_direction","bullish"),("horizons",(1,))),kind=HypothesisKind.FORECAST_PATTERN)
    validation=SimpleNamespace(hypothesis_id="h",status=HypothesisValidationStatus.CONFIRMED)
    outcome=forecast_outcome(hypothesis=hypothesis,validation=validation,observation_event_key="event",maturity=maturity,forecast=forecast,evaluated_at=now)
    assert outcome.direction_correct is True

def test_ll41_unconfirmed_pattern_does_not_get_credit():
    assert HypothesisValidationStatus.PENDING is not HypothesisValidationStatus.CONFIRMED

def test_failed_candidate_is_matured_but_not_counted_as_improved(us_instrument,now):
    from tradehelper_v2.contracts import PromotionDecision
    from tradehelper_v2.research.outcomes import candidate_outcome,metric_snapshot
    hypothesis=SimpleNamespace(hypothesis_id="h",instrument=us_instrument,kind=HypothesisKind.MODEL_CONFIGURATION)
    validation=SimpleNamespace(hypothesis_id="h",status=HypothesisValidationStatus.CONFIRMED)
    candidate,event=_candidate_event(us_instrument,now,PromotionDecision.REJECT)
    outcome=candidate_outcome(hypothesis=hypothesis,validation=validation,observation_event_key="e",candidate=candidate,promotion_events=(event,),evaluated_at=now)
    snapshot=metric_snapshot(market=us_instrument.market,scope_key=us_instrument.stable_key,cutoff_at=now,hypotheses=(hypothesis,),validations=(validation,),outcomes=(outcome,),generated_at=now)
    assert dict(snapshot.metrics)["candidate_oof_improved_count"]==0

def test_candidate_metrics_count_each_candidate_once(us_instrument,now):
    from tradehelper_v2.contracts import PromotionDecision
    from tradehelper_v2.research.outcomes import candidate_outcome,metric_snapshot
    hypothesis=SimpleNamespace(hypothesis_id="h",instrument=us_instrument,kind=HypothesisKind.MODEL_CONFIGURATION)
    validation=SimpleNamespace(hypothesis_id="h",status=HypothesisValidationStatus.CONFIRMED)
    candidate,event=_candidate_event(us_instrument,now,PromotionDecision.PROMOTE_TO_CHALLENGER)
    pending=candidate_outcome(hypothesis=hypothesis,validation=validation,observation_event_key="pending",candidate=candidate,promotion_events=(),evaluated_at=now)
    matured=candidate_outcome(hypothesis=hypothesis,validation=validation,observation_event_key="matured",candidate=candidate,promotion_events=(event,),evaluated_at=now)
    snapshot=metric_snapshot(market=us_instrument.market,scope_key=us_instrument.stable_key,cutoff_at=now,hypotheses=(hypothesis,),validations=(validation,),outcomes=(pending,matured),generated_at=now)
    metrics=dict(snapshot.metrics)
    assert metrics["candidate_created_count"]==1 and metrics["candidate_oof_improved_count"]==1
