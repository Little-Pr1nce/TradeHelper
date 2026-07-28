"""严格 JSON parser：把 LLM 输出限制成五种有限、可验证的研究假设。"""
from __future__ import annotations

import json
from math import isfinite

from contracts import (ConditionExpression, ConditionOperand, ConditionOperator, ContractViolation, EvidenceRequirement, HypothesisKind, HypothesisNovelty, OperandKind, ResearchHypothesis, stable_hash)
from .registry import ResearchMappingRegistry, default_research_registry

_TOP = frozenset(("schema_version", "context_id", "hypotheses"))
_ITEM = frozenset(("kind", "instrument_key", "title", "thesis", "evidence_refs", "payload"))
_FORBIDDEN = frozenset(("shares", "position", "cash", "account", "execution_level", "promote", "promotion", "champion", "target_price", "stop_price", "probability", "order", "code", "url", "sql", "regex"))
_DIRECTIONS = frozenset(("bullish", "neutral", "bearish"))
_HORIZONS = frozenset((1, 3, 5, 10))

class _UnknownPredicateOperator(ContractViolation):
    pass

def compile_condition(predicate):
    op = predicate["op"]
    if op in {"all", "any"}:
        return ConditionExpression("", ConditionOperator(op), children=tuple(compile_condition(item) for item in predicate["children"]), reason_code="PLAN_WAITING")
    if op == "not":
        return ConditionExpression("", ConditionOperator.NOT, children=(compile_condition(predicate["child"]),), reason_code="PLAN_WAITING")
    feature = ConditionOperand(OperandKind.FEATURE, predicate["fact_key"], None, predicate.get("unit"), (predicate["fact_key"],))
    evidence = EvidenceRequirement.EVENT_SEQUENCE if op in {"crosses_above", "crosses_below"} else EvidenceRequirement.SNAPSHOT
    if op == "between":
        return ConditionExpression("", ConditionOperator.BETWEEN, left=feature, lower=ConditionOperand(OperandKind.CONSTANT, "lower", predicate["lower"], predicate.get("unit")), upper=ConditionOperand(OperandKind.CONSTANT, "upper", predicate["upper"], predicate.get("unit")), evidence_requirement=evidence, reason_code="PLAN_WAITING")
    return ConditionExpression("", ConditionOperator(op), left=feature, right=ConditionOperand(OperandKind.CONSTANT, "constant", predicate["constant"], predicate.get("unit")), evidence_requirement=evidence, reason_code="PLAN_WAITING")


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ContractViolation(f"research {name} must be finite")
    return float(value)


def _plain_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\n" in value or "\x00" in value:
        raise ContractViolation(f"research {name} is invalid")
    return value


class StrictHypothesisParser:
    """拒绝猜测修复：一条无效响应整体不产生确定性 hypothesis。"""

    def __init__(self, registry: ResearchMappingRegistry | None = None):
        self.registry = registry or default_research_registry()

    def parse(self, *, content, context, response):
        if not isinstance(content, str) or not content or content.strip() != content or content.startswith("```"):
            raise ContractViolation("research response must be one strict JSON object")
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ContractViolation("research response is not valid JSON") from exc
        if not isinstance(value, dict) or frozenset(value) != _TOP or value.get("schema_version") != 1 or value.get("context_id") != context.context_id or not isinstance(value.get("hypotheses"), list):
            raise ContractViolation("research response schema is invalid")
        total_limit = 20 if context.scope.value == "portfolio" else 5
        if len(value["hypotheses"]) > total_limit:
            raise ContractViolation("research hypothesis limit exceeded")
        instruments = {item.stable_key: item for item in context.manifest.instruments}
        facts = {item.fact_id: item for item in context.manifest.facts}
        result: list[ResearchHypothesis] = []
        per_instrument: dict[str, int] = {}
        for raw in value["hypotheses"]:
            if not isinstance(raw, dict) or frozenset(raw) != _ITEM:
                raise ContractViolation("research hypothesis has unknown fields")
            try:
                kind = HypothesisKind(raw["kind"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ContractViolation("unknown research hypothesis kind") from exc
            instrument_key = raw["instrument_key"]
            instrument = instruments.get(instrument_key) if instrument_key is not None else None
            if instrument is None and not (kind is HypothesisKind.SYSTEM_CHALLENGE and context.scope.value == "portfolio" and context.portfolio_bundle_id):
                raise ContractViolation("research hypothesis instrument is unknown")
            refs = raw["evidence_refs"]
            if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)) or not all(isinstance(item, str) and item in facts for item in refs):
                raise ContractViolation("research hypothesis evidence ref is unknown")
            if instrument is not None and any(facts[item].instrument not in {None, instrument} for item in refs):
                raise ContractViolation("research hypothesis evidence belongs to another instrument")
            if instrument is not None:
                per_instrument[instrument.stable_key] = per_instrument.get(instrument.stable_key, 0) + 1
                if per_instrument[instrument.stable_key] > 5:
                    raise ContractViolation("research per-instrument limit exceeded")
            try:
                payload = self._payload(kind, raw["payload"], facts, context)
            except _UnknownPredicateOperator as exc:
                kind=HypothesisKind.IMPLEMENTATION_PROPOSAL
                payload={"proposal_type":"unregistered_dsl_operator","research_question":f"Register and test predicate operator {exc}","required_inputs":("ConditionOperator","point_in_time_evidence"),"expected_benefit":"Preserve the research idea without executing an unregistered expression.","engineering_acceptance_notes":"Implement the operator in the shared V2-5 DSL and add dual-market replay tests before registration."}
            predicate_refs=self._predicate_refs(payload.get("predicate"))
            if predicate_refs and not predicate_refs.issubset(refs):
                raise ContractViolation("research predicate facts must be declared as evidence")
            kind, payload = self._implementation_if_unregistered(kind, payload)
            if kind is HypothesisKind.MODEL_CONFIGURATION:
                kind,payload=self._bind_model_scope(kind,payload,instrument,refs,facts,context)
            title = _plain_text(raw["title"], "title", 80)
            thesis = _plain_text(raw["thesis"], "thesis", 500)
            business = stable_hash({"scope": context.scope.value, "instrument": instrument, "kind": kind.value, "payload": payload})
            identity = {"business": business, "response": response.response_id, "context": context.context_id, "instrument": instrument, "kind": kind, "evidence": tuple(sorted(refs)), "payload": tuple(sorted(payload.items())), "novelty": HypothesisNovelty.NOVEL}
            result.append(ResearchHypothesis(stable_hash(identity), business, response.response_id, context.context_id, instrument, kind, title, thesis, tuple(refs), tuple(payload.items()), HypothesisNovelty.NOVEL, response.received_at))
        return tuple(result)

    def _implementation_if_unregistered(self, kind, payload):
        """未知模型/策略不伪装成可执行候选，而是保留为工程待办。"""
        if kind is HypothesisKind.MODEL_CONFIGURATION and not self.registry.model_is_registered(payload["registered_model_family"], payload["registered_feature_set_id"]):
            question=f"Register model family {payload['registered_model_family']} with feature set {payload['registered_feature_set_id']}"
            return HypothesisKind.IMPLEMENTATION_PROPOSAL, {"proposal_type":"unregistered_model_or_feature","research_question":question,"required_inputs":(payload["registered_feature_set_id"],),"expected_benefit":"Evaluate only after point-in-time implementation and OOF tests.","engineering_acceptance_notes":"Add a frozen registry entry, bounded search space, and dual-market tests."}
        if kind is HypothesisKind.STRATEGY_CONFIGURATION and not self.registry.strategy_is_registered(payload["registered_strategy_id"]):
            return HypothesisKind.IMPLEMENTATION_PROPOSAL, {"proposal_type":"unregistered_strategy","research_question":f"Register strategy template {payload['registered_strategy_id']}","required_inputs":("StrategySpec",),"expected_benefit":"Evaluate only after stop, invalidation, validity and OOF coverage are implemented.","engineering_acceptance_notes":"Implement and register the template; never derive an order from this proposal."}
        return kind, payload

    @staticmethod
    def _bind_model_scope(kind,payload,instrument,refs,facts,context):
        scope=payload["scope"]
        if scope=="stock":
            payload={**payload,"resolved_scope_key":instrument.stable_key}
        elif scope=="market":
            payload={**payload,"resolved_scope_key":context.market.value}
        else:
            industry=[
                facts[ref] for ref in refs
                if facts[ref].instrument==instrument
                and facts[ref].key=="feature.context.industry"
                and facts[ref].status=="available"
                and isinstance(facts[ref].value,str)
                and facts[ref].value
            ]
            if len(industry)!=1:
                return HypothesisKind.IMPLEMENTATION_PROPOSAL,{
                    "proposal_type":"industry_scope_requires_frozen_fact",
                    "research_question":"Bind the industry candidate to one frozen feature.context.industry fact.",
                    "required_inputs":("feature.context.industry",),
                    "expected_benefit":"Prevent an industry candidate from being evaluated against an unrelated stock or market scope.",
                    "engineering_acceptance_notes":"Retry only after the industry fact is present in the hypothesis evidence.",
                }
            payload={**payload,"resolved_scope_key":str(industry[0].value)}
        return kind,payload

    def _payload(self, kind, payload, facts, context):
        if not isinstance(payload, dict) or any(key in _FORBIDDEN for key in payload) or any(not isinstance(key, str) for key in payload):
            raise ContractViolation("research hypothesis contains forbidden payload")
        if kind is HypothesisKind.FORECAST_PATTERN:
            self._exact(payload, {"predicate", "expected_direction", "horizons"}, optional={"regime_scope"})
            expected = payload["expected_direction"]
            horizons = payload["horizons"]
            if not isinstance(expected,str) or expected not in _DIRECTIONS or not isinstance(horizons, list) or not horizons or any(isinstance(item,bool) or not isinstance(item,int) for item in horizons) or not set(horizons).issubset(_HORIZONS):
                raise ContractViolation("forecast research payload is invalid")
            predicate = self._predicate(payload["predicate"], facts)
            # condition_id 由既有 V2-5 合同从冻结事实编译，LLM 从未接触其哈希或
            # reason code；validator 仍只读取同一份有限 AST。
            return {"predicate": predicate, "condition_expression": compile_condition(predicate), "expected_direction": expected, "horizons": tuple(sorted(set(horizons))), "regime_scope": self._optional_token(payload.get("regime_scope"))}
        if kind is HypothesisKind.MODEL_CONFIGURATION:
            self._exact(payload, {"registered_model_family", "registered_feature_set_id", "scope", "horizons", "registered_hyperparameter_overrides"}, optional={"regime_filter"})
            if payload["scope"] not in {"stock", "industry", "market"} or not isinstance(payload["registered_model_family"], str) or not isinstance(payload["registered_feature_set_id"], str) or not isinstance(payload["horizons"], list) or not payload["horizons"] or not set(payload["horizons"]).issubset(_HORIZONS):
                raise ContractViolation("model configuration payload is invalid")
            return {"registered_model_family": payload["registered_model_family"], "registered_feature_set_id": payload["registered_feature_set_id"], "scope": payload["scope"], "horizons": tuple(sorted(set(payload["horizons"]))), "registered_hyperparameter_overrides": self._scalar_map(payload["registered_hyperparameter_overrides"]), "regime_filter": self._optional_token(payload.get("regime_filter"))}
        if kind is HypothesisKind.STRATEGY_CONFIGURATION:
            self._exact(payload, {"registered_strategy_id", "parameter_overrides", "applicable_scenario_states", "profile_scope", "research_rationale"})
            from contracts import ScenarioState
            allowed_states={item.value for item in ScenarioState}
            if not isinstance(payload["registered_strategy_id"], str) or not isinstance(payload["applicable_scenario_states"], list) or not payload["applicable_scenario_states"] or not set(payload["applicable_scenario_states"]).issubset(allowed_states) or payload["profile_scope"] not in {None,"conservative","aggressive"}:
                raise ContractViolation("strategy configuration payload is invalid")
            return {"registered_strategy_id": payload["registered_strategy_id"], "parameter_overrides": self._scalar_map(payload["parameter_overrides"]), "applicable_scenario_states": tuple(sorted(set(payload["applicable_scenario_states"]))), "profile_scope": self._optional_token(payload["profile_scope"]), "research_rationale": _plain_text(payload["research_rationale"], "research rationale", 500)}
        if kind is HypothesisKind.SYSTEM_CHALLENGE:
            self._exact(payload, {"challenged_artifact_type", "challenged_artifact_id", "challenge_kind"}, optional={"counterfactual_mapping"})
            artifact_type=payload["challenged_artifact_type"]
            artifact_id=payload["challenged_artifact_id"]
            if not all(isinstance(payload[item], str) and payload[item] for item in ("challenged_artifact_type", "challenged_artifact_id")) or artifact_id not in self._artifact_ids(context) or artifact_id not in self._artifact_ids_by_type(context).get(artifact_type,set()) or payload["challenge_kind"] not in {"fact_disagreement", "forecast_disagreement", "missing_opportunity", "strategy_too_restrictive", "risk_too_restrictive", "data_quality_concern"}:
                raise ContractViolation("system challenge payload is invalid")
            return {"challenged_artifact_type": payload["challenged_artifact_type"], "challenged_artifact_id": payload["challenged_artifact_id"], "challenge_kind": payload["challenge_kind"], "counterfactual_mapping": self._optional_token(payload.get("counterfactual_mapping"))}
        self._exact(payload, {"proposal_type", "research_question", "required_inputs", "expected_benefit", "engineering_acceptance_notes"})
        if not isinstance(payload["proposal_type"], str) or not isinstance(payload["required_inputs"], list) or not all(isinstance(item, str) and item for item in payload["required_inputs"]):
            raise ContractViolation("implementation proposal payload is invalid")
        return {"proposal_type": payload["proposal_type"], "research_question": _plain_text(payload["research_question"], "research question", 500), "required_inputs": tuple(sorted(set(payload["required_inputs"]))), "expected_benefit": _plain_text(payload["expected_benefit"], "expected benefit", 500), "engineering_acceptance_notes": _plain_text(payload["engineering_acceptance_notes"], "engineering notes", 500)}

    @staticmethod
    def _exact(value, required, optional=frozenset()):
        if frozenset(value) != frozenset(required) | frozenset(optional) and not (frozenset(required).issubset(value) and frozenset(value).issubset(frozenset(required) | frozenset(optional))):
            raise ContractViolation("research payload has unknown fields")

    @staticmethod
    def _optional_token(value):
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 80 or "\n" in value:
            raise ContractViolation("research payload token is invalid")
        return value

    @staticmethod
    def _scalar_map(value):
        if not isinstance(value, dict) or any(not isinstance(key, str) or not key or isinstance(item, (dict, list)) or not isinstance(item, (str, int, float, bool)) or (isinstance(item, float) and not isfinite(item)) for key, item in value.items()):
            raise ContractViolation("research parameter overrides are invalid")
        return tuple(sorted(value.items()))

    def _predicate(self, value, facts):
        if not isinstance(value, dict) or not isinstance(value.get("op"), str):
            raise ContractViolation("research predicate is invalid")
        op = value["op"]
        if op in {"all", "any"}:
            if frozenset(value) != {"op", "children"} or not isinstance(value["children"], list) or not value["children"]:
                raise ContractViolation("research logical predicate is invalid")
            return {"op": op, "children": tuple(self._predicate(item, facts) for item in value["children"])}
        if op == "not":
            if frozenset(value) != {"op", "child"}:
                raise ContractViolation("research not predicate is invalid")
            return {"op": op, "child": self._predicate(value["child"], facts)}
        if op in {"gte", "crosses_above", "crosses_below"}:
            if frozenset(value) != {"op", "fact_ref", "constant"} or value["fact_ref"] not in facts:
                raise ContractViolation("research predicate fact is invalid")
            fact = facts[value["fact_ref"]]
            constant = _finite_number(value["constant"], "predicate constant")
            if fact.unit not in {None, "price", "ratio", "index"}:
                raise ContractViolation("research predicate unit is unsupported")
            return {"op": op, "fact_ref": value["fact_ref"], "fact_key": fact.key, "unit": fact.unit, "constant": constant}
        if op == "between":
            if frozenset(value) != {"op", "fact_ref", "lower", "upper"} or value["fact_ref"] not in facts:
                raise ContractViolation("research predicate fact is invalid")
            lower, upper = _finite_number(value["lower"], "predicate lower"), _finite_number(value["upper"], "predicate upper")
            if lower > upper:
                raise ContractViolation("research predicate range is inverted")
            fact = facts[value["fact_ref"]]
            return {"op": op, "fact_ref": value["fact_ref"], "fact_key": fact.key, "unit": fact.unit, "lower": lower, "upper": upper}
        raise _UnknownPredicateOperator(op)

    @staticmethod
    def _predicate_refs(predicate):
        if not isinstance(predicate,dict):
            return set()
        refs={predicate["fact_ref"]} if isinstance(predicate.get("fact_ref"),str) else set()
        for child in predicate.get("children",()):
            refs.update(StrictHypothesisParser._predicate_refs(child))
        refs.update(StrictHypothesisParser._predicate_refs(predicate.get("child")))
        return refs

    @staticmethod
    def _artifact_ids(context):
        return frozenset(context.manifest.artifact_refs) | frozenset(context.forecast_event_keys) | frozenset(context.scenario_ids) | frozenset(context.strategy_bundle_ids) | frozenset(context.risk_bundle_ids) | ({context.portfolio_bundle_id} if context.portfolio_bundle_id else set())

    @staticmethod
    def _artifact_ids_by_type(context):
        return {
            "forecast":set(context.forecast_event_keys),
            "scenario":set(context.scenario_ids),
            "strategy":set(context.strategy_bundle_ids),
            "risk":set(context.risk_bundle_ids),
            "portfolio":{context.portfolio_bundle_id} if context.portfolio_bundle_id else set(),
            "artifact":set(context.manifest.artifact_refs),
            "learning":set(context.learning_snapshot_ids),
        }
