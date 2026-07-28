"""将已注册、确认的研究配置追加成 V2-9 CANDIDATE 请求。"""
from __future__ import annotations

from contracts import CandidateEligibility, CandidateKind, CandidateLifecycle, CandidateScope, EvidenceOrigin, HypothesisCandidateLink, HypothesisKind, LearningCandidateVersion, stable_hash
from .registry import ResearchMappingRegistry, default_research_registry


class CandidateBridge:
    def __init__(self, registry: ResearchMappingRegistry | None = None):
        self.registry = registry or default_research_registry()
        self.registry_version = self.registry.version

    def bridge(self, hypothesis, validation, *, market, scope_key, base_version, search_space_hash, created_at, existing_business_keys=(), existing_candidate_count=0):
        payload = dict(hypothesis.payload)
        if validation.candidate_eligibility is not CandidateEligibility.ELIGIBLE_FOR_OOF:
            reason={
                CandidateEligibility.OBSERVATION_ONLY:"RESEARCH_CURRENT_OBSERVATION_ONLY",
                CandidateEligibility.IMPLEMENTATION_REQUIRED:"RESEARCH_IMPLEMENTATION_REQUIRED",
                CandidateEligibility.REJECTED:"RESEARCH_PARAMETER_OUT_OF_BOUNDS",
            }.get(validation.candidate_eligibility,"RESEARCH_OOF_REQUIRED")
            return self._link(hypothesis, None, validation.candidate_eligibility, None, (reason,), created_at), None
        business_key = getattr(hypothesis, "business_key", None) or self._business_key(hypothesis, payload, market, scope_key)
        if business_key in set(existing_business_keys):
            return self._link(hypothesis, None, CandidateEligibility.REJECTED, None, ("RESEARCH_DUPLICATE_HYPOTHESIS",), created_at), None
        if existing_candidate_count >= 20:
            return self._link(hypothesis,None,CandidateEligibility.REJECTED,None,("RESEARCH_CANDIDATE_LIMIT_REACHED",),created_at),None
        if hypothesis.kind is HypothesisKind.MODEL_CONFIGURATION:
            # 注册与参数边界由 validator 在同一冻结 registry 中先行确认；桥接只
            # 接受该明确资格，避免第二次解释 LLM 原始文本。
            kind, mapping = CandidateKind.FORECAST_CONFIGURATION, self.registry.mapping_key(payload)
        elif hypothesis.kind is HypothesisKind.STRATEGY_CONFIGURATION:
            kind, mapping = CandidateKind.STRATEGY_PARAMETER_SET, self.registry.mapping_key(payload)
        elif hypothesis.kind is HypothesisKind.SYSTEM_CHALLENGE and payload.get("counterfactual_mapping") in self.registry.counterfactual_mappings:
            raw_mapping=self.registry.counterfactual_mappings[payload["counterfactual_mapping"]]
            prefix,mapping=(raw_mapping.split(":",1) if ":" in raw_mapping else ("forecast",raw_mapping))
            kind={"forecast":CandidateKind.FORECAST_CONFIGURATION,"strategy":CandidateKind.STRATEGY_PARAMETER_SET,"scenario":CandidateKind.SCENARIO_SOFT_POLICY,"risk":CandidateKind.RISK_SOFT_POLICY,"portfolio":CandidateKind.PORTFOLIO_SOFT_POLICY}.get(prefix)
            if kind is None:
                return self._link(hypothesis,None,CandidateEligibility.REJECTED,None,("RESEARCH_IMPLEMENTATION_REQUIRED",),created_at),None
        else:
            return self._link(hypothesis, None, CandidateEligibility.OBSERVATION_ONLY, None, ("RESEARCH_CURRENT_OBSERVATION_ONLY",), created_at), None
        scope_value=payload.get("scope","stock")
        scope={"stock":CandidateScope.STOCK,"industry":CandidateScope.INDUSTRY,"market":CandidateScope.MARKET}.get(scope_value,CandidateScope.STOCK)
        if hypothesis.kind is HypothesisKind.MODEL_CONFIGURATION:
            resolved_scope_key=payload.get("resolved_scope_key")
            if scope is CandidateScope.STOCK:
                resolved_scope_key=hypothesis.instrument.stable_key if getattr(hypothesis,"instrument",None) is not None else scope_key
            elif scope is CandidateScope.MARKET:
                resolved_scope_key=market.value
            if not resolved_scope_key:
                return self._link(hypothesis,None,CandidateEligibility.IMPLEMENTATION_REQUIRED,None,("RESEARCH_IMPLEMENTATION_REQUIRED",),created_at),None
        elif getattr(hypothesis,"instrument",None) is not None:
            resolved_scope_key=hypothesis.instrument.stable_key
            scope=CandidateScope.STOCK
        else:
            resolved_scope_key=scope_key
        profile=payload.get("profile_scope")
        parameter_hash = stable_hash({"configuration":{key:value for key,value in payload.items() if key!="research_rationale"},"mapping_version":self.registry_version})
        projection_key = f"{market.value}|{scope.value}|{resolved_scope_key}|{kind.value}|{mapping}|{profile or 'all'}"
        identity = {"kind": kind, "scope": scope, "scope_key": resolved_scope_key, "market": market, "profile": profile, "base_version": base_version, "parameter_hash": parameter_hash, "search_space_hash": search_space_hash, "origin": EvidenceOrigin.RECONSTRUCTED_OOF, "lifecycle": CandidateLifecycle.CANDIDATE, "projection_key": projection_key}
        candidate = LearningCandidateVersion(stable_hash(identity), kind, scope, resolved_scope_key, market, profile, base_version, parameter_hash, search_space_hash, CandidateLifecycle.CANDIDATE, EvidenceOrigin.RECONSTRUCTED_OOF, created_at, created_at, ("LEARNING_CANDIDATE_WITHIN_BOUNDS",), projection_key)
        return self._link(hypothesis, candidate.candidate_id, CandidateEligibility.ELIGIBLE_FOR_OOF, mapping, ("RESEARCH_OOF_REQUIRED", "RESEARCH_CANDIDATE_CREATED"), created_at), candidate

    @staticmethod
    def _business_key(hypothesis, payload, market, scope_key):
        instrument=getattr(hypothesis,"instrument",None)
        kind=getattr(hypothesis,"kind",None)
        normalized={key:value for key,value in payload.items() if key not in {"research_rationale"}}
        return stable_hash({
            "market":market,
            "scope_key":instrument.stable_key if instrument is not None else scope_key,
            "kind":kind.value if hasattr(kind,"value") else str(kind),
            "payload":normalized,
        })

    def _link(self, hypothesis, candidate_id, eligibility, mapping, reasons, created_at):
        identity = {"hypothesis": hypothesis.hypothesis_id, "candidate": candidate_id, "eligibility": eligibility, "mapping_version": self.registry_version, "mapping_key": mapping, "reasons": tuple(sorted(reasons))}
        return HypothesisCandidateLink(stable_hash(identity), hypothesis.hypothesis_id, candidate_id, eligibility, self.registry_version, mapping, tuple(reasons), created_at)
