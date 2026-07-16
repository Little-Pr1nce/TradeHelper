"""LL01--LL09：冻结上下文、市场和最小披露。"""
import json
import pytest
from tradehelper_v2.contracts import ContractViolation, DecisionMode, FeatureEvidenceMode, FeatureSnapshot, FeatureStatus, FeatureValue, Market, ResearchFact, ResearchScope, stable_hash
from tradehelper_v2.research.context import ResearchContextBuilder
from tradehelper_v2.research.prompt import build_prompt_chunks

def _fact(instrument, now):
    payload={"instrument":instrument,"key":"feature.closed.rsi_14","value":50.0,"status":"available","available_at":now,"source_refs":("fixture",),"source_payload_hash":None}
    return ResearchFact(stable_hash(payload),instrument,"feature.closed.rsi_14",50.0,"number",None,"available",now,("fixture",),None)

def test_ll01_context_rejects_future_fact(us_instrument, now):
    builder=ResearchContextBuilder(); fact=_fact(us_instrument,now)
    manifest=builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=us_instrument.market,cutoff_at=now,instruments=(us_instrument,),facts=(fact,),generated_at=now)
    assert manifest.facts == (fact,)

def test_ll09_market_isolation_uses_same_contract(us_instrument, a_instrument, now):
    builder=ResearchContextBuilder()
    assert builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=us_instrument.market,cutoff_at=now,instruments=(us_instrument,),facts=(_fact(us_instrument,now),),generated_at=now).market != builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=a_instrument.market,cutoff_at=now,instruments=(a_instrument,),facts=(_fact(a_instrument,now),),generated_at=now).market

def test_manifest_rejects_foreign_market_fact(us_instrument,a_instrument,now):
    with pytest.raises(ContractViolation):
        ResearchContextBuilder().build_manifest(scope=ResearchScope.SINGLE_STOCK,market=Market.US,cutoff_at=now,instruments=(us_instrument,),facts=(_fact(a_instrument,now),),generated_at=now)

def test_context_scope_and_roles_must_match_manifest(us_instrument,now):
    builder=ResearchContextBuilder()
    manifest=builder.build_manifest(scope=ResearchScope.PORTFOLIO,market=Market.US,cutoff_at=now,instruments=(us_instrument,),facts=(_fact(us_instrument,now),),generated_at=now)
    with pytest.raises(ContractViolation):
        builder.build_context(scope=ResearchScope.SINGLE_STOCK,market=Market.US,mode="eod",cutoff_at=now,manifest=manifest,instrument_roles=((us_instrument,"subject"),),generated_at=now)

def test_context_artifact_references_must_be_frozen(us_instrument,now):
    builder=ResearchContextBuilder()
    manifest=builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=Market.US,cutoff_at=now,instruments=(us_instrument,),facts=(_fact(us_instrument,now),),generated_at=now)
    with pytest.raises(ContractViolation):
        builder.build_context(scope=ResearchScope.SINGLE_STOCK,market=Market.US,mode="eod",cutoff_at=now,manifest=manifest,instrument_roles=((us_instrument,"subject"),),forecast_event_keys=("foreign-forecast",),generated_at=now)

def test_portfolio_prompt_is_stably_chunked_and_fact_bounded(now):
    from tradehelper_v2.contracts import Exchange, InstrumentId
    instruments=tuple(InstrumentId(f"T{i:02d}",Market.US,Exchange.XNAS) for i in range(11))
    global_payload={"instrument":None,"key":"portfolio.bundle.market","value":"US","status":"available","available_at":now,"source_refs":("portfolio",),"source_payload_hash":"a"*64}
    global_fact=ResearchFact(stable_hash(global_payload),None,"portfolio.bundle.market","US","text",None,"available",now,("portfolio",),"a"*64)
    facts=tuple(_fact(instrument,now) for instrument in instruments)+(global_fact,)
    builder=ResearchContextBuilder()
    manifest=builder.build_manifest(scope=ResearchScope.PORTFOLIO,market=Market.US,cutoff_at=now,instruments=instruments,facts=facts,generated_at=now)
    context=builder.build_context(scope=ResearchScope.PORTFOLIO,market=Market.US,mode="eod",cutoff_at=now,manifest=manifest,instrument_roles=tuple((item,"holding" if index<2 else "watchlist") for index,item in enumerate(reversed(instruments))),generated_at=now)
    chunks=build_prompt_chunks(context)
    assert tuple(len(item[0]) for item in chunks)==(10,1)
    assert chunks==build_prompt_chunks(context)
    assert all(sum(item["fact_id"]==global_fact.fact_id for item in json.loads(chunk[1])["facts"])==1 for chunk in chunks)


def test_project_upstream_feature_snapshot_uses_registered_namespace(us_instrument,now):
    value=FeatureValue("closed.rsi_14",55.0,FeatureStatus.AVAILABLE,"index",14,now,("fixture",),True,None)
    snapshot=FeatureSnapshot(us_instrument,DecisionMode.EOD,now,now.date(),None,"2.2.0",FeatureEvidenceMode.RECONSTRUCTED_HISTORY,(value,),"a"*64,"b"*64,now)
    builder=ResearchContextBuilder()
    facts=builder.project_upstream_facts(feature_snapshots=(snapshot,))
    assert len(facts)==1 and facts[0].key=="feature.closed.rsi_14" and facts[0].source_refs==(snapshot.feature_hash,)
    manifest=builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=Market.US,cutoff_at=now,instruments=(us_instrument,),facts=facts,generated_at=now)
    assert manifest.facts==facts
