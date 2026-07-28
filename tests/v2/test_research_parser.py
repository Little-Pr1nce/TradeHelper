"""LL10--LL19：严格 JSON 解析边界。"""
import json
import pytest
from contracts import (InvocationStatus, RawResearchResponse, ResearchFact, ResearchScope, stable_hash)
from research.context import ResearchContextBuilder
from research.parser import StrictHypothesisParser

def _context_response(instrument, now):
    fact_payload={"instrument":instrument,"key":"feature.closed.rsi_14","value":60.0,"status":"available","available_at":now,"source_refs":("fixture",),"source_payload_hash":None}; fact=ResearchFact(stable_hash(fact_payload),instrument,"feature.closed.rsi_14",60.0,"number",None,"available",now,("fixture",),None)
    builder=ResearchContextBuilder(); manifest=builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=instrument.market,cutoff_at=now,instruments=(instrument,),facts=(fact,),generated_at=now); context=builder.build_context(scope=ResearchScope.SINGLE_STOCK,market=instrument.market,mode="eod",cutoff_at=now,manifest=manifest,instrument_roles=((instrument,"subject"),),generated_at=now)
    content='{}'; payload={"request":"request","context":context.context_id,"revision":1,"provider":"fake","model":"fake","content_hash":stable_hash(content),"finish":"stop","status":InvocationStatus.SUCCEEDED,"prompt_version":"p","prompt_hash":"a"*64}; response=RawResearchResponse(stable_hash(payload),"request",context.context_id,1,"fake","fake",content,stable_hash(content),"stop",InvocationStatus.SUCCEEDED,now,"p","a"*64)
    return context,response,fact

def test_ll10_markdown_is_rejected(us_instrument,now):
    context,response,_=_context_response(us_instrument,now)
    with pytest.raises(Exception): StrictHypothesisParser().parse(content='```json\n{}\n```',context=context,response=response)

def test_ll11_context_id_must_match(us_instrument,now):
    context,response,_=_context_response(us_instrument,now)
    with pytest.raises(Exception): StrictHypothesisParser().parse(content=json.dumps({"schema_version":1,"context_id":"other","hypotheses":[]}),context=context,response=response)

def test_ll12_forbidden_execution_field_is_rejected(us_instrument,now):
    context,response,fact=_context_response(us_instrument,now); body={"schema_version":1,"context_id":context.context_id,"hypotheses":[{"kind":"forecast_pattern","instrument_key":us_instrument.stable_key,"title":"x","thesis":"x","evidence_refs":[fact.fact_id],"payload":{"shares":100}}]}
    with pytest.raises(Exception): StrictHypothesisParser().parse(content=json.dumps(body),context=context,response=response)
