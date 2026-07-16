"""LL20--LL29：四状态必须由冻结事实决定。"""
from types import SimpleNamespace
from tradehelper_v2.contracts import CandidateEligibility, HypothesisKind, HypothesisValidationStatus
from tradehelper_v2.research.validator import DeterministicHypothesisValidator

def _hypothesis(context, fact, *, payload):
    return SimpleNamespace(hypothesis_id="h",context_id=context.context_id,evidence_refs=(fact.fact_id,),payload=tuple(payload.items()),kind=HypothesisKind.FORECAST_PATTERN)

def test_ll20_snapshot_true_is_confirmed(us_instrument,now):
    from test_research_parser import _context_response
    context,_,fact=_context_response(us_instrument,now); value=DeterministicHypothesisValidator().validate(_hypothesis(context,fact,payload={"predicate":{"op":"gte","fact_ref":fact.fact_id,"constant":50}}),context,evaluated_at=now)
    assert value.status is HypothesisValidationStatus.CONFIRMED

def test_ll21_snapshot_false_is_refuted(us_instrument,now):
    from test_research_parser import _context_response
    context,_,fact=_context_response(us_instrument,now); value=DeterministicHypothesisValidator().validate(_hypothesis(context,fact,payload={"predicate":{"op":"gte","fact_ref":fact.fact_id,"constant":70}}),context,evaluated_at=now)
    assert value.status is HypothesisValidationStatus.REFUTED

def test_ll23_crossing_is_pending(us_instrument,now):
    from test_research_parser import _context_response
    context,_,fact=_context_response(us_instrument,now); value=DeterministicHypothesisValidator().validate(_hypothesis(context,fact,payload={"predicate":{"op":"crosses_above","fact_ref":fact.fact_id,"constant":70}}),context,evaluated_at=now)
    assert value.status is HypothesisValidationStatus.PENDING and value.candidate_eligibility is CandidateEligibility.OBSERVATION_ONLY

def test_predicate_cannot_hide_a_stale_fact_outside_evidence(us_instrument,now):
    from research_helpers import context_response, fact, forecast_item, response_json
    from tradehelper_v2.contracts import ContractViolation
    from tradehelper_v2.research.parser import StrictHypothesisParser
    import pytest
    good=fact(us_instrument,now)
    stale=fact(us_instrument,now,key="feature.closed.hidden",value=None,status="stale")
    context,response,_=context_response(us_instrument,now,facts=(good,stale))
    item=forecast_item(us_instrument,good.fact_id,predicate={"op":"gte","fact_ref":stale.fact_id,"constant":1})
    with pytest.raises(ContractViolation):
        StrictHypothesisParser().parse(content=response_json(context,[item]),context=context,response=response)
