"""LL47：response revision 的幂等与冲突隔离。"""
from pathlib import Path
import json
from datetime import date
from types import SimpleNamespace
import pytest
from test_research_parser import _context_response
from contracts import CandidateEligibility,ContractViolation,HypothesisKind,HypothesisOutcome,HypothesisOutcomeStatus,HypothesisValidationStatus,stable_hash
from data.repository import SQLiteRepository
from research.bridge import CandidateBridge
from research.parser import StrictHypothesisParser
from research.validator import DeterministicHypothesisValidator

def test_response_revision_is_idempotent_and_conflicts_are_quarantined(tmp_path,us_instrument,now):
    _,response,_=_context_response(us_instrument,now); path=Path(tmp_path)/"research.sqlite"; repo=SQLiteRepository(path)
    try:
        assert repo.save_research_response(response).inserted == 1
        assert repo.save_research_response(response).idempotent == 1
    finally: repo.close()
    repo=SQLiteRepository(path)
    try: assert repo.get_research_response(response.response_id) == response
    finally: repo.close()


def test_ll47_atomic_result_revision_quarantine_and_strong_restore(tmp_path,us_instrument,now):
    context,response,fact=_context_response(us_instrument,now)
    body={"schema_version":1,"context_id":context.context_id,"hypotheses":[{"kind":"forecast_pattern","instrument_key":us_instrument.stable_key,"title":"predicate","thesis":"research only","evidence_refs":[fact.fact_id],"payload":{"predicate":{"op":"gte","fact_ref":fact.fact_id,"constant":50},"expected_direction":"bullish","horizons":[1]}}]}
    hypotheses=StrictHypothesisParser().parse(content=json.dumps(body,separators=(",",":")),context=context,response=response)
    validations=tuple(DeterministicHypothesisValidator().validate(item,context,evaluated_at=now) for item in hypotheses)
    links=tuple(CandidateBridge().bridge(item,validation,market=us_instrument.market,scope_key=us_instrument.stable_key,base_version="base",search_space_hash="a"*64,created_at=now)[0] for item,validation in zip(hypotheses,validations))
    path=Path(tmp_path)/"atomic.sqlite"; repo=SQLiteRepository(path)
    try: repo.save_research_result(context,response,hypotheses,validations,links)
    finally: repo.close()
    repo=SQLiteRepository(path)
    try:
        assert repo.get_research_context(context.context_id) == context
        assert repo.get_research_hypothesis(hypotheses[0].hypothesis_id).hypothesis_id == hypotheses[0].hypothesis_id
        assert repo.get_hypothesis_validation(validations[0].validation_id) == validations[0]
        assert repo.get_hypothesis_candidate_link(links[0].link_id) == links[0]
        repo._connection.execute("UPDATE research_contexts SET payload_hash=? WHERE context_id=?",("0"*64,context.context_id))
        repo._connection.commit()
        with pytest.raises(ContractViolation):
            repo.save_research_result(context,response,hypotheses,validations,links)
        assert repo._connection.execute("SELECT COUNT(*) FROM quarantine_records WHERE record_type='research_record_conflict'").fetchone()[0]==1
    finally: repo.close()


def test_research_outcome_cannot_cross_instrument(tmp_path,us_instrument,a_instrument,now):
    context,response,fact=_context_response(us_instrument,now)
    body={"schema_version":1,"context_id":context.context_id,"hypotheses":[{"kind":"forecast_pattern","instrument_key":us_instrument.stable_key,"title":"predicate","thesis":"research only","evidence_refs":[fact.fact_id],"payload":{"predicate":{"op":"gte","fact_ref":fact.fact_id,"constant":50},"expected_direction":"bullish","horizons":[1]}}]}
    hypotheses=StrictHypothesisParser().parse(content=json.dumps(body,separators=(",",":")),context=context,response=response)
    validations=tuple(DeterministicHypothesisValidator().validate(item,context,evaluated_at=now) for item in hypotheses)
    links=tuple(CandidateBridge().bridge(item,validation,market=us_instrument.market,scope_key=us_instrument.stable_key,base_version="base",search_space_hash="a"*64,created_at=now)[0] for item,validation in zip(hypotheses,validations))
    repo=SQLiteRepository(Path(tmp_path)/"cross.sqlite")
    try:
        repo.save_research_result(context,response,hypotheses,validations,links)
        identity={"hypothesis":hypotheses[0].hypothesis_id,"event":"e","instrument":a_instrument,"origin":date.today(),"target":None,"horizon":None,"trigger":HypothesisValidationStatus.CONFIRMED,"expected":None,"actual":None,"actual_return":None,"direction_correct":None,"maturity":None,"forecast":None,"candidate":None,"promotions":(),"status":HypothesisOutcomeStatus.PENDING,"evidence_grade":"pending"}
        outcome=HypothesisOutcome(stable_hash(identity),hypotheses[0].hypothesis_id,"e",a_instrument,date.today(),None,None,HypothesisValidationStatus.CONFIRMED,None,None,None,None,None,None,None,(),HypothesisOutcomeStatus.PENDING,"pending",now,now)
        with pytest.raises(ContractViolation):
            repo.save_hypothesis_outcome(outcome)
    finally:
        repo.close()


def test_research_outcome_candidate_must_be_linked_to_hypothesis(tmp_path,us_instrument,now):
    context,response,fact=_context_response(us_instrument,now)
    body={"schema_version":1,"context_id":context.context_id,"hypotheses":[{"kind":"forecast_pattern","instrument_key":us_instrument.stable_key,"title":"predicate","thesis":"research only","evidence_refs":[fact.fact_id],"payload":{"predicate":{"op":"gte","fact_ref":fact.fact_id,"constant":50},"expected_direction":"bullish","horizons":[1]}}]}
    hypotheses=StrictHypothesisParser().parse(content=json.dumps(body,separators=(",",":")),context=context,response=response)
    validations=tuple(DeterministicHypothesisValidator().validate(item,context,evaluated_at=now) for item in hypotheses)
    links=tuple(CandidateBridge().bridge(item,validation,market=us_instrument.market,scope_key=us_instrument.stable_key,base_version="base",search_space_hash="a"*64,created_at=now)[0] for item,validation in zip(hypotheses,validations))
    candidate_hypothesis=SimpleNamespace(hypothesis_id="other",business_key="other",kind=HypothesisKind.MODEL_CONFIGURATION,payload=(("scope","stock"),("registered_model_family","analog")))
    candidate_validation=SimpleNamespace(candidate_eligibility=CandidateEligibility.ELIGIBLE_FOR_OOF)
    _,candidate=CandidateBridge().bridge(candidate_hypothesis,candidate_validation,market=us_instrument.market,scope_key=us_instrument.stable_key,base_version="base",search_space_hash="a"*64,created_at=now)
    repo=SQLiteRepository(Path(tmp_path)/"candidate_link.sqlite")
    try:
        with pytest.raises(ContractViolation):
            repo.save_research_result(context,response,hypotheses,validations,links,candidates=(candidate,))
        repo.save_research_result(context,response,hypotheses,validations,links)
        repo.save_learning_candidate(candidate)
        identity={"hypothesis":hypotheses[0].hypothesis_id,"event":"candidate-e","instrument":us_instrument,"origin":date.today(),"target":None,"horizon":None,"trigger":HypothesisValidationStatus.CONFIRMED,"expected":None,"actual":None,"actual_return":None,"direction_correct":None,"maturity":None,"forecast":None,"candidate":candidate.candidate_id,"promotions":(),"status":HypothesisOutcomeStatus.PENDING,"evidence_grade":"pending"}
        outcome=HypothesisOutcome(stable_hash(identity),hypotheses[0].hypothesis_id,"candidate-e",us_instrument,date.today(),None,None,HypothesisValidationStatus.CONFIRMED,None,None,None,None,None,None,candidate.candidate_id,(),HypothesisOutcomeStatus.PENDING,"pending",now,now)
        with pytest.raises(ContractViolation):
            repo.save_hypothesis_outcome(outcome)
    finally:
        repo.close()
