"""V2-10 Golden Case 的冻结事实构造器；不依赖外部行情或 LLM。"""
from __future__ import annotations

import json

from tradehelper_v2.contracts import InvocationStatus, ResearchFact, ResearchScope, RawResearchResponse, stable_hash
from tradehelper_v2.research.context import ResearchContextBuilder


def fact(instrument, now, *, key="feature.closed.rsi_14", value=60.0, status="available", source_refs=("fixture",), source_hash=None, available_at=None):
    available_at=available_at or now
    if status!="available":
        value=None
    payload={"instrument":instrument,"key":key,"value":value,"status":status,"available_at":available_at,"source_refs":tuple(source_refs),"source_payload_hash":source_hash}
    return ResearchFact(stable_hash(payload),instrument,key,value,"number",None,status,available_at,tuple(source_refs),source_hash)


def context_response(instrument, now, *, scope=ResearchScope.SINGLE_STOCK, facts=None, artifact_refs=(), portfolio_bundle_id=None):
    facts=tuple(facts or (fact(instrument,now),))
    builder=ResearchContextBuilder()
    manifest=builder.build_manifest(scope=scope,market=instrument.market,cutoff_at=now,instruments=(instrument,),facts=facts,artifact_refs=artifact_refs,generated_at=now)
    context=builder.build_context(scope=scope,market=instrument.market,mode="eod",cutoff_at=now,manifest=manifest,instrument_roles=((instrument,"subject" if scope is ResearchScope.SINGLE_STOCK else "watchlist"),),portfolio_bundle_id=portfolio_bundle_id,generated_at=now)
    content="{}"; identity={"request":"request","context":context.context_id,"revision":1,"provider":"fake","model":"fake","content_hash":stable_hash(content),"finish":"stop","status":InvocationStatus.SUCCEEDED,"prompt_version":"p","prompt_hash":"a"*64}
    response=RawResearchResponse(stable_hash(identity),"request",context.context_id,1,"fake","fake",content,stable_hash(content),"stop",InvocationStatus.SUCCEEDED,now,"p","a"*64)
    return context,response,facts


def response_json(context, hypotheses):
    return json.dumps({"schema_version":1,"context_id":context.context_id,"hypotheses":hypotheses},separators=(",",":"))


def forecast_item(instrument, fact_id, *, predicate=None):
    return {"kind":"forecast_pattern","instrument_key":instrument.stable_key,"title":"Frozen predicate","thesis":"Research explanation only.","evidence_refs":[fact_id],"payload":{"predicate":predicate or {"op":"gte","fact_ref":fact_id,"constant":50},"expected_direction":"bullish","horizons":[1]}}
