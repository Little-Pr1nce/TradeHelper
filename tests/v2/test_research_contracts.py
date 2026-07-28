"""LL00：研究合同的冻结、时点和身份。"""
from dataclasses import replace
from datetime import timedelta
import pytest
from contracts import ContractViolation, ResearchFact, ResearchScope, stable_hash

def _fact(instrument, now, *, key="feature.closed.rsi_14", value=55.0, status="available"):
    identity={"instrument":instrument,"key":key,"value":value,"status":status,"available_at":now,"source_refs":("fixture",),"source_payload_hash":None}
    return ResearchFact(stable_hash(identity),instrument,key,value,"number",None,status,now,("fixture",),None)

def test_ll00_fact_identity_is_stable_and_time_aware(us_instrument, now):
    fact=_fact(us_instrument,now)
    assert fact.fact_id == _fact(us_instrument,now).fact_id
    with pytest.raises(ContractViolation): replace(fact, available_at=now+timedelta(seconds=1))

def test_ll04_financial_value_requires_source(us_instrument, now):
    identity={"instrument":us_instrument,"key":"feature.fund.pe_ttm","value":10.0,"status":"available","available_at":now,"source_refs":("fixture",),"source_payload_hash":None}
    with pytest.raises(ContractViolation): ResearchFact(stable_hash(identity),us_instrument,"feature.fund.pe_ttm",10.0,"number",None,"available",now,("fixture",),None)

def test_research_fact_rejects_non_hash_source_payload(us_instrument,now):
    identity={"instrument":us_instrument,"key":"feature.closed.rsi_14","value":55.0,"status":"available","available_at":now,"source_refs":("fixture",),"source_payload_hash":"not-a-hash"}
    with pytest.raises(ContractViolation):
        ResearchFact(stable_hash(identity),us_instrument,"feature.closed.rsi_14",55.0,"number",None,"available",now,("fixture",),"not-a-hash")
