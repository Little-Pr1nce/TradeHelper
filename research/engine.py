"""V2-10 编排：失败不阻断预测、策略、风控或组合主链。"""
from __future__ import annotations

from datetime import datetime,timezone
import logging
from time import perf_counter

from contracts import InvocationStatus, ResearchRunStatus, stable_hash
from .client import unavailable_response
from .prompt import MAX_PROMPT_INSTRUMENTS

logger = logging.getLogger(__name__)


class ResearchEngine:
    def __init__(self, parser, validator, bridge):
        self.parser = parser
        self.validator = validator
        self.bridge = bridge
        versions={item.registry.version for item in (parser,validator,bridge)}
        registry_hashes={stable_hash(item.registry) for item in (parser,validator,bridge)}
        if len(versions)!=1 or len(registry_hashes)!=1:
            raise ValueError("research parser, validator and bridge must share one frozen registry")

    def run(self, *, context, request, client, market, scope_key, base_version, search_space_hash, cancelled=lambda: False, existing_business_keys=(), existing_candidate_count=0, allowed_instrument_keys=()):
        empty = {"hypotheses": (), "validations": (), "links": (), "candidates": ()}
        if market is not context.market:
            raise ValueError("research engine market must match frozen context")
        allowed=set(allowed_instrument_keys or request.instrument_keys)
        context_keys={item.stable_key for item in context.manifest.instruments}
        if not allowed.issubset(context_keys):
            raise ValueError("research request instruments must belong to frozen context")
        if len(context_keys)>MAX_PROMPT_INSTRUMENTS and not allowed:
            raise ValueError("large portfolio research must use stable prompt chunks")
        if cancelled():
            return {"status": ResearchRunStatus.PARTIAL, "reason": "cancelled", "response": None, **empty}
        try:
            response = client.generate(request)
        except TimeoutError:
            response = unavailable_response(request=request, status=InvocationStatus.TIMED_OUT, received_at=datetime.now(timezone.utc),revision=request.revision)
            return {"status": ResearchRunStatus.UNAVAILABLE, "reason": "RESEARCH_LLM_TIMEOUT", "response": response, **empty}
        except Exception:
            response = unavailable_response(request=request, status=InvocationStatus.TRANSPORT_FAILED, received_at=datetime.now(timezone.utc),revision=request.revision)
            return {"status": ResearchRunStatus.UNAVAILABLE, "reason": "RESEARCH_LLM_TRANSPORT_FAILED", "response": response, **empty}
        if response.context_id != context.context_id or response.request_id != request.request_id:
            return {"status": ResearchRunStatus.FAILED, "reason": "RESEARCH_SCHEMA_INVALID", "response": response, **empty}
        if response.invocation_status is not InvocationStatus.SUCCEEDED:
            return {"status": ResearchRunStatus.PARTIAL, "reason": response.invocation_status.value, "response": response, **empty}
        if response.finish_reason not in {None, "stop", "end_turn", "completed"}:
            return {"status": ResearchRunStatus.PARTIAL, "reason": "RESEARCH_RESPONSE_TRUNCATED", "response": response, **empty}
        if cancelled():
            return {"status":ResearchRunStatus.PARTIAL,"reason":"cancelled","response":response,**empty}
        try:
            hypotheses = self.parser.parse(content=response.content, context=context, response=response)
        except Exception:
            return {"status": ResearchRunStatus.PARTIAL, "reason": "RESEARCH_SCHEMA_INVALID", "response": response, **empty}
        if allowed and any(item.instrument is not None and item.instrument.stable_key not in allowed for item in hypotheses):
            return {"status":ResearchRunStatus.PARTIAL,"reason":"RESEARCH_INSTRUMENT_UNKNOWN","response":response,**empty}
        try:
            validations = tuple(self.validator.validate(item, context, evaluated_at=response.received_at) for item in hypotheses)
        except Exception:
            logger.exception(
                "Research validation rejected malformed hypothesis request_id=%s",
                request.request_id,
            )
            return {
                "status":ResearchRunStatus.PARTIAL,
                "reason":"RESEARCH_VALIDATION_FAILED",
                "response":response,
                **empty,
            }
        pairs=[]
        candidate_count=existing_candidate_count
        business_keys=set(existing_business_keys)
        for item,validation in zip(hypotheses,validations):
            pair=self.bridge.bridge(item,validation,market=market,scope_key=scope_key,base_version=base_version,search_space_hash=search_space_hash,created_at=response.received_at,existing_business_keys=business_keys,existing_candidate_count=candidate_count)
            pairs.append(pair)
            business_keys.add(item.business_key)
            if pair[1] is not None:
                candidate_count+=1
        pairs=tuple(pairs)
        return {"status": ResearchRunStatus.COMPLETED, "response": response, "hypotheses": hypotheses, "validations": validations, "links": tuple(item[0] for item in pairs), "candidates": tuple(item[1] for item in pairs if item[1] is not None)}

    def run_chunks(self, *, context, invocations, market, scope_key, base_version, search_space_hash, cancelled=lambda:False, existing_business_keys=(), existing_candidate_count=0):
        """稳定分片依次执行；失败分片不丢弃其他分片的有效观察。"""
        if market is not context.market:
            raise ValueError("research engine market must match frozen context")
        collected={"responses":[],"hypotheses":[],"validations":[],"links":[],"candidates":[]}
        attempted_chunks=0
        completed_chunks=0
        failure_reasons=[]
        keys=set(existing_business_keys)
        status=ResearchRunStatus.COMPLETED
        reason=None
        for invocation in invocations:
            if cancelled():
                status=ResearchRunStatus.PARTIAL; reason="cancelled"; break
            if len(collected["hypotheses"])>=20:
                break
            attempted_chunks+=1
            if len(invocation)!=3:
                raise ValueError("chunk invocation must contain instrument keys, request and client")
            instrument_keys,request,client=invocation
            if tuple(sorted(set(instrument_keys))) != request.instrument_keys:
                raise ValueError("chunk request instruments must match the prompt chunk")
            started_at=perf_counter()
            logger.info(
                "Research chunk started request_id=%s model=%s instruments=%s max_output_tokens=%s",
                request.request_id,request.model_name,",".join(instrument_keys),request.max_output_tokens,
            )
            result=self.run(context=context,request=request,client=client,market=market,scope_key=scope_key,base_version=base_version,search_space_hash=search_space_hash,cancelled=cancelled,existing_business_keys=keys,existing_candidate_count=existing_candidate_count+len(collected["candidates"]),allowed_instrument_keys=instrument_keys)
            response=result["response"]
            if response is not None:
                collected["responses"].append(response)
            logger.info(
                "Research chunk finished request_id=%s status=%s reason=%s finish_reason=%s "
                "response_chars=%s token_usage=%s hypotheses=%s duration_seconds=%.3f",
                request.request_id,_value(result["status"]),result.get("reason") or "none",
                getattr(response,"finish_reason",None) or "none",
                len(getattr(response,"content","") or ""),
                getattr(response,"token_usage",None) or "unknown",
                len(result["hypotheses"]),perf_counter()-started_at,
            )
            if result["status"] is not ResearchRunStatus.COMPLETED:
                status=ResearchRunStatus.PARTIAL; reason=result.get("reason")
                failure_reasons.append(reason or "RESEARCH_UNKNOWN_FAILURE")
                continue
            completed_chunks+=1
            remaining=20-len(collected["hypotheses"])
            for name in ("hypotheses","validations","links"):
                collected[name].extend(result[name][:remaining])
            accepted_ids={item.hypothesis_id for item in result["hypotheses"][:remaining]}
            collected["candidates"].extend(item for item in result["candidates"] if any(link.candidate_id==item.candidate_id and link.hypothesis_id in accepted_ids for link in result["links"]))
            keys.update(item.business_key for item in result["hypotheses"][:remaining])
            if remaining<=len(result["hypotheses"]): break
        return {
            "status":status,"reason":reason,"responses":tuple(collected["responses"]),
            "hypotheses":tuple(collected["hypotheses"]),"validations":tuple(collected["validations"]),
            "links":tuple(collected["links"]),"candidates":tuple(collected["candidates"]),
            "attempted_chunks":attempted_chunks,"completed_chunks":completed_chunks,
            "failure_reasons":tuple(failure_reasons),
        }


def _value(value):
    return getattr(value,"value",value)
