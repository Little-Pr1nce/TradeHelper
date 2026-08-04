"""Opt-in real LLM smoke: connection, strict JSON, parser and secret redaction."""
from __future__ import annotations

import os
import json
from datetime import datetime,timezone

import pytest

from config.settings import V2Settings
from contracts import InstrumentId,InvocationStatus,Market,ResearchScope,stable_hash
from research.client import LLMResearchRequest,OpenAICompatibleResearchClient,capabilities_for_endpoint,output_token_budget
from research.context import ResearchContextBuilder
from research.parser import StrictHypothesisParser
from research.prompt import PROMPT_VERSION,build_prompt
from research_helpers import fact


if os.environ.get("TRADEHELPER_LLM_LIVE_TESTS")!="1":
    pytestmark=pytest.mark.skip(reason="set TRADEHELPER_LLM_LIVE_TESTS=1 to run the configured real LLM smoke")


def _settings():
    if os.environ.get("TRADEHELPER_LIVE_USE_V1_SETTINGS")=="1":
        path=V2Settings.default_path().with_name("config.json")
        legacy=json.loads(path.read_text(encoding="utf-8"))
        return V2Settings.from_mapping({name:legacy.get(name,"") for name in ("llm_base_url","llm_api_key","llm_model","llm_enable_thinking")})
    return V2Settings.load()


def test_RL78_live_llm_strict_json_and_secret_redaction():
    settings=_settings()
    if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
        pytest.fail("real LLM smoke was enabled but configured LLM credentials are incomplete")
    now=datetime.now(timezone.utc)
    instrument=InstrumentId.from_code("AAPL",Market.US,"XNAS")
    builder=ResearchContextBuilder()
    facts=(fact(instrument,now),fact(instrument,now,key="position.shares",value=12345))
    manifest=builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=Market.US,cutoff_at=now,instruments=(instrument,),facts=facts,generated_at=now)
    context=builder.build_context(scope=ResearchScope.SINGLE_STOCK,market=Market.US,mode="eod",cutoff_at=now,manifest=manifest,instrument_roles=((instrument,"subject"),),generated_at=now)
    prompt,prompt_hash=build_prompt(context)
    assert "12345" not in prompt
    capabilities=capabilities_for_endpoint(settings.llm_base_url)
    request=LLMResearchRequest.for_capabilities(capabilities=capabilities,request_id="live-research-smoke",context_id=context.context_id,prompt_version=PROMPT_VERSION,prompt_hash=prompt_hash,json_schema_version=1,provider_name="configured",model_name=settings.llm_model,requested_at=now,max_output_tokens=output_token_budget(settings.llm_enable_thinking),timeout_seconds=90,thinking_enabled=settings.llm_enable_thinking)
    client=OpenAICompatibleResearchClient(
        endpoint=settings.llm_base_url,api_key=settings.llm_api_key,prompts={request.request_id:prompt},
        capabilities=capabilities,
    )
    response=client.generate(request)
    assert response.invocation_status is InvocationStatus.SUCCEEDED
    assert settings.llm_api_key not in response.content
    hypotheses=StrictHypothesisParser().parse(content=response.content,context=context,response=response)
    assert len(hypotheses)<=5
    assert response.content_hash==stable_hash(response.content)
