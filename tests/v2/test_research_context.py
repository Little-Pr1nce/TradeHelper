"""LL01--LL09：冻结上下文、市场和最小披露。"""
from datetime import timedelta
import json
import pytest
from contracts import ContractViolation, DecisionMode, FeatureEvidenceMode, FeatureSnapshot, FeatureStatus, FeatureValue, FundamentalSnapshot, FundamentalValue, Market, NewsSnapshot, QualityStatus, ResearchFact, ResearchScope, stable_hash
from research.context import MAX_NEWS_ITEMS_PER_INSTRUMENT, ResearchContextBuilder
from research.prompt import build_prompt_chunks

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
    from contracts import Exchange, InstrumentId
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
    payload=json.loads(chunks[0][1])
    assert payload["challenge_reference_catalog"]["fact_source_ref_type"]=="artifact"
    system_schema=next(item for item in payload["output_schema"]["properties"]["hypotheses"]["items"]["oneOf"] if item["properties"]["kind"].get("const")=="system_challenge")
    assert set(system_schema["properties"]["payload"]["properties"]["challenged_artifact_type"]["enum"])=={"forecast","scenario","strategy","risk","portfolio","learning","artifact"}


def test_prompt_redacts_account_segment_without_trailing_dot(us_instrument,now):
    fact_payload={"instrument":us_instrument,"key":"feature.context.account","value":"private","status":"available","available_at":now,"source_refs":("fixture",),"source_payload_hash":"a"*64}
    sensitive=ResearchFact(stable_hash(fact_payload),us_instrument,"feature.context.account","private","text",None,"available",now,("fixture",),"a"*64)
    builder=ResearchContextBuilder()
    manifest=builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=Market.US,cutoff_at=now,instruments=(us_instrument,),facts=(sensitive,),generated_at=now)
    context=builder.build_context(scope=ResearchScope.SINGLE_STOCK,market=Market.US,mode="eod",cutoff_at=now,manifest=manifest,instrument_roles=((us_instrument,"subject"),),generated_at=now)
    assert "private" not in build_prompt_chunks(context)[0][1]


def test_project_upstream_feature_snapshot_uses_registered_namespace(us_instrument,now):
    value=FeatureValue("closed.rsi_14",55.0,FeatureStatus.AVAILABLE,"index",14,now,("fixture",),True,None)
    snapshot=FeatureSnapshot(us_instrument,DecisionMode.EOD,now,now.date(),None,"2.2.0",FeatureEvidenceMode.RECONSTRUCTED_HISTORY,(value,),"a"*64,"b"*64,now)
    builder=ResearchContextBuilder()
    facts=builder.project_upstream_facts(feature_snapshots=(snapshot,))
    assert len(facts)==1 and facts[0].key=="feature.closed.rsi_14" and facts[0].source_refs==(snapshot.feature_hash,)
    manifest=builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=Market.US,cutoff_at=now,instruments=(us_instrument,),facts=facts,generated_at=now)
    assert manifest.facts==facts


def test_news_and_fundamental_snapshots_are_projected_with_sources_and_limits(us_instrument,now):
    news=tuple(
        NewsSnapshot(
            us_instrument,f"Headline {index}","finnhub",now-timedelta(hours=index+1),
            now-timedelta(hours=index),now,"  summary\ntext  ",False,
            "positive",0.8,0.9,
        )
        for index in range(MAX_NEWS_ITEMS_PER_INSTRUMENT+2)
    )
    fundamentals=FundamentalSnapshot(
        us_instrument,
        {"peTTM":FundamentalValue(22.5,"multiple",None,now-timedelta(days=1),"finnhub")},
        now,now,"finnhub",QualityStatus.OK,
    )
    facts=ResearchContextBuilder().project_upstream_facts(
        news_snapshots=news,fundamental_snapshots=(fundamentals,),
    )
    titles=[item for item in facts if item.key.endswith(".title")]
    summaries=[item for item in facts if item.key.endswith(".summary")]
    financial=[item for item in facts if item.key.startswith("feature.fund.raw.")]
    assert len(titles)==MAX_NEWS_ITEMS_PER_INSTRUMENT
    assert all(item.source_payload_hash and item.source_refs for item in titles+financial)
    assert {item.value for item in summaries}=={"summary text"}
    assert len(financial)==1 and financial[0].value==22.5 and financial[0].unit=="multiple"
    builder=ResearchContextBuilder()
    manifest=builder.build_manifest(
        scope=ResearchScope.SINGLE_STOCK,market=Market.US,cutoff_at=now,
        instruments=(us_instrument,),facts=facts,generated_at=now,
    )
    context=builder.build_context(
        scope=ResearchScope.SINGLE_STOCK,market=Market.US,mode="eod",
        cutoff_at=now,manifest=manifest,
        instrument_roles=((us_instrument,"subject"),),generated_at=now,
    )
    prompt_facts=json.loads(build_prompt_chunks(context)[0][1])["facts"]
    projected={item["fact_id"]:item for item in prompt_facts}
    assert all(projected[item.fact_id]["source_refs"]==list(item.source_refs) for item in titles+financial)


def test_strategy_projection_keeps_plan_protection_ids(us_instrument):
    from strategy_helpers import strategy_input
    from strategies import StrategyEngine
    bundle=StrategyEngine().build(strategy_input(us_instrument))
    facts=ResearchContextBuilder().project_upstream_facts(strategy_bundles=(bundle,))
    keys={item.key for item in facts}
    plans=tuple(plan for branch in (bundle.entry_or_add,bundle.reduce_or_exit,bundle.hold,bundle.invalidation) for plan in branch.plans)
    assert all(f"strategy.{plan.plan_id}.invalidation_condition_id" in keys for plan in plans)
    actionable=tuple(plan for plan in plans if plan.stop is not None)
    assert actionable and all(f"strategy.{plan.plan_id}.stop_condition_id" in keys for plan in actionable)
