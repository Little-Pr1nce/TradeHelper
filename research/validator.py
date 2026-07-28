"""只读取冻结事实与注册表的确定性四状态验证器。"""
from __future__ import annotations

from contracts import CandidateEligibility, ConditionResult, FeatureStatus, HypothesisKind, HypothesisValidation, HypothesisValidationStatus, stable_hash
from strategies.conditions import evaluate
from .registry import ResearchMappingRegistry, default_research_registry
from .parser import compile_condition


class DeterministicHypothesisValidator:
    validator_version = "research_validator_v2"

    def __init__(self, registry: ResearchMappingRegistry | None = None):
        self.registry = registry or default_research_registry()

    def validate(self, hypothesis, context, *, evaluated_at):
        facts = {item.fact_id: item for item in context.manifest.facts}
        payload = dict(hypothesis.payload)
        predicate_refs=self._predicate_refs(payload.get("predicate"))
        all_refs=tuple(sorted(set(hypothesis.evidence_refs)|predicate_refs))
        selected = [facts.get(item) for item in all_refs]
        missing, conflicting = self._data_problems(selected, all_refs, context)
        linked_artifacts = self._artifacts(payload, context)
        condition_evaluation=None
        if missing or conflicting:
            status = HypothesisValidationStatus.INVALID_DATA
            observed = ()
            reason = "RESEARCH_FACT_CONFLICTING" if conflicting else self._missing_reason(selected)
            eligibility = CandidateEligibility.OBSERVATION_ONLY
        elif hypothesis.kind is HypothesisKind.IMPLEMENTATION_PROPOSAL:
            status, observed, reason, eligibility = HypothesisValidationStatus.PENDING, tuple(hypothesis.evidence_refs), "RESEARCH_IMPLEMENTATION_REQUIRED", CandidateEligibility.IMPLEMENTATION_REQUIRED
        elif hypothesis.kind is HypothesisKind.SYSTEM_CHALLENGE:
            status = HypothesisValidationStatus.PENDING
            observed = tuple(hypothesis.evidence_refs)
            reason = "RESEARCH_FACT_PENDING_EVENT"
            eligibility = CandidateEligibility.OBSERVATION_ONLY
        elif hypothesis.kind is HypothesisKind.MODEL_CONFIGURATION:
            status, observed, reason, eligibility = self._registered_model(payload, hypothesis.evidence_refs)
        elif hypothesis.kind is HypothesisKind.STRATEGY_CONFIGURATION:
            status, observed, reason, eligibility = self._registered_strategy(payload, hypothesis.evidence_refs)
        else:
            status, observed, reason, eligibility, condition_evaluation = self._predicate(payload, facts, all_refs, evaluated_at)
        identity = {"hypothesis": hypothesis.hypothesis_id, "context": context.context_id, "status": status, "condition_evaluation":condition_evaluation,"observed": observed, "missing": missing, "conflicting": conflicting, "artifacts": linked_artifacts, "eligibility": eligibility, "validator": self.validator_version, "reasons": (reason,), "evaluated": evaluated_at}
        return HypothesisValidation(stable_hash(identity), hypothesis.hypothesis_id, context.context_id, status, observed, missing, conflicting, linked_artifacts, eligibility, self.validator_version, (reason,), evaluated_at, evaluated_at,condition_evaluation)

    @staticmethod
    def _data_problems(selected, refs, context):
        missing, conflicting = [], []
        for ref, fact in zip(refs, selected):
            if fact is None or fact.available_at > context.cutoff_at or fact.status in {"missing", "stale", "blocked", "invalid"}:
                missing.append(ref)
            elif fact.status == "conflicting":
                conflicting.append(ref)
            elif fact.key.startswith("feature.fund.") and fact.value is not None and not fact.source_payload_hash:
                missing.append(ref)
        return tuple(sorted(set(missing))), tuple(sorted(set(conflicting)))

    @staticmethod
    def _missing_reason(selected):
        statuses = {item.status for item in selected if item is not None}
        if "stale" in statuses:
            return "RESEARCH_FACT_STALE"
        if "blocked" in statuses:
            return "RESEARCH_FACT_BLOCKED"
        return "RESEARCH_FACT_MISSING"

    def _registered_model(self, payload, refs):
        parameters = dict(payload.get("registered_hyperparameter_overrides", ()))
        if not self.registry.model_is_registered(payload.get("registered_model_family"), payload.get("registered_feature_set_id")):
            return HypothesisValidationStatus.PENDING, tuple(refs), "RESEARCH_IMPLEMENTATION_REQUIRED", CandidateEligibility.IMPLEMENTATION_REQUIRED
        if not self.registry.model_parameters_valid(payload["registered_model_family"], parameters):
            return HypothesisValidationStatus.REFUTED, tuple(refs), "RESEARCH_PARAMETER_OUT_OF_BOUNDS", CandidateEligibility.REJECTED
        return HypothesisValidationStatus.CONFIRMED, tuple(refs), "RESEARCH_REGISTERED_MODEL_MAPPING", CandidateEligibility.ELIGIBLE_FOR_OOF

    def _registered_strategy(self, payload, refs):
        strategy_id = payload.get("registered_strategy_id")
        parameters = dict(payload.get("parameter_overrides", ()))
        if not self.registry.strategy_is_registered(strategy_id):
            return HypothesisValidationStatus.PENDING, tuple(refs), "RESEARCH_IMPLEMENTATION_REQUIRED", CandidateEligibility.IMPLEMENTATION_REQUIRED
        spec=self.registry.strategies[strategy_id]
        if not set(payload.get("applicable_scenario_states",())).issubset({item.value for item in spec.allowed_states}):
            return HypothesisValidationStatus.REFUTED, tuple(refs), "RESEARCH_PARAMETER_OUT_OF_BOUNDS", CandidateEligibility.REJECTED
        if not self.registry.strategy_parameters_valid(strategy_id, parameters):
            return HypothesisValidationStatus.REFUTED, tuple(refs), "RESEARCH_PARAMETER_OUT_OF_BOUNDS", CandidateEligibility.REJECTED
        # StrategySpec 自身已承载止损/失效/有效期能力的冻结校验；研究层不能取消它。
        return HypothesisValidationStatus.CONFIRMED, tuple(refs), "RESEARCH_REGISTERED_STRATEGY_MAPPING", CandidateEligibility.ELIGIBLE_FOR_OOF

    def _predicate(self, payload, facts, refs, evaluated_at):
        predicate=payload.get("predicate")
        if not isinstance(predicate, dict):
            return HypothesisValidationStatus.PENDING, tuple(refs), "RESEARCH_IMPLEMENTATION_REQUIRED", CandidateEligibility.IMPLEMENTATION_REQUIRED,None
        if payload.get("condition_expression") is None:
            predicate=self._enrich_predicate(predicate,facts)
        expression=payload.get("condition_expression") or compile_condition(predicate)
        values={fact.key:(fact.value,FeatureStatus(fact.status) if fact.status in {item.value for item in FeatureStatus} else FeatureStatus.MISSING,fact.available_at) for fact in facts.values()}
        result=evaluate(expression,values,evaluated_at)
        mapping={
            ConditionResult.TRUE:(HypothesisValidationStatus.CONFIRMED,"RESEARCH_FACT_CONFIRMED"),
            ConditionResult.FALSE:(HypothesisValidationStatus.REFUTED,"RESEARCH_FACT_REFUTED"),
            ConditionResult.PENDING_EVENT:(HypothesisValidationStatus.PENDING,"RESEARCH_FACT_PENDING_EVENT"),
            ConditionResult.UNKNOWN:(HypothesisValidationStatus.INVALID_DATA,"RESEARCH_FACT_MISSING"),
            ConditionResult.NOT_APPLICABLE:(HypothesisValidationStatus.INVALID_DATA,"RESEARCH_FACT_MISSING"),
        }
        status,reason=mapping[result.result]
        observed=tuple(sorted(fact.fact_id for fact in facts.values() if any(item.key==fact.key for item in result.observed_values)))
        return status,observed,reason,CandidateEligibility.OBSERVATION_ONLY,result

    @staticmethod
    def _requires_event(predicate):
        if not isinstance(predicate, dict):
            return False
        if predicate.get("op") in {"crosses_above", "crosses_below"}:
            return True
        return any(DeterministicHypothesisValidator._requires_event(item) for item in predicate.get("children", ())) or DeterministicHypothesisValidator._requires_event(predicate.get("child"))

    @staticmethod
    def _predicate_refs(predicate):
        if not isinstance(predicate,dict): return set()
        refs={predicate["fact_ref"]} if isinstance(predicate.get("fact_ref"),str) else set()
        for child in predicate.get("children",()): refs.update(DeterministicHypothesisValidator._predicate_refs(child))
        refs.update(DeterministicHypothesisValidator._predicate_refs(predicate.get("child")))
        return refs

    @staticmethod
    def _enrich_predicate(predicate,facts):
        value=dict(predicate)
        if "fact_ref" in value:
            fact=facts[value["fact_ref"]]
            value["fact_key"]=fact.key
            value["unit"]=fact.unit
        if "children" in value:
            value["children"]=tuple(DeterministicHypothesisValidator._enrich_predicate(item,facts) for item in value["children"])
        if "child" in value:
            value["child"]=DeterministicHypothesisValidator._enrich_predicate(value["child"],facts)
        return value

    @staticmethod
    def _artifacts(payload, context):
        artifact = payload.get("challenged_artifact_id")
        return (artifact,) if artifact else ()
