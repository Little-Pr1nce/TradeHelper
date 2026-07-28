"""LLM 独立复盘账：只消费 V2-9 已冻结的 maturity/forecast/candidate 事实。"""
from __future__ import annotations

from collections import Counter

from contracts import (HypothesisKind, HypothesisOutcome, HypothesisOutcomeStatus, HypothesisValidationStatus, LearningCandidateVersion, PromotionDecision, PromotionEvent, ResearchMetricSnapshot, stable_hash)


def forecast_outcome(*, hypothesis, validation, observation_event_key, maturity, forecast, evaluated_at, seen_business_events=()):
    """仅 confirmed 的 forecast pattern 在成熟后计入方向样本。"""
    if hypothesis.kind is not HypothesisKind.FORECAST_PATTERN:
        return _not_applicable(hypothesis, validation, observation_event_key, evaluated_at)
    payload = dict(hypothesis.payload)
    expected = payload.get("expected_direction")
    if not hasattr(forecast,"forecast_outcome_id"):
        raise ValueError("research scoring requires a frozen V2-9 ForecastOutcome")
    if forecast.instrument!=hypothesis.instrument or maturity.instrument!=hypothesis.instrument or forecast.origin_session_date!=maturity.origin_session_date or forecast.target_session_date!=maturity.target_session_date or forecast.horizon not in payload.get("horizons",()):
        raise ValueError("research forecast outcome references do not match hypothesis")
    if forecast.maturity_evidence_id!=maturity.evidence_id:
        raise ValueError("research forecast outcome must link the supplied maturity evidence")
    uniqueness = (getattr(hypothesis, "business_key", hypothesis.hypothesis_id), hypothesis.instrument.stable_key, forecast.origin_session_date, forecast.horizon)
    if uniqueness in set(seen_business_events):
        return _superseded(hypothesis, validation, observation_event_key, forecast, evaluated_at)
    matured = validation.status is HypothesisValidationStatus.CONFIRMED and maturity.status.value == "matured"
    status = HypothesisOutcomeStatus.MATURED if matured else HypothesisOutcomeStatus.PENDING
    actual = maturity.actual_direction.value if matured else None
    correct = actual == expected if matured and expected is not None else None
    forecast_id=forecast.forecast_outcome_id if matured else None
    actual_return = float(maturity.actual_return) if matured and maturity.actual_return is not None else None
    grade="high" if matured else "pending"
    identity = {"hypothesis": hypothesis.hypothesis_id, "event": observation_event_key, "instrument": hypothesis.instrument, "origin": forecast.origin_session_date, "target": forecast.target_session_date, "horizon": forecast.horizon, "trigger": validation.status, "expected":expected,"actual": actual,"actual_return":actual_return,"direction_correct":correct, "maturity": maturity.evidence_id if matured else None, "forecast": forecast_id, "candidate": None,"promotions":(), "status": status,"evidence_grade":grade}
    return HypothesisOutcome(stable_hash(identity), hypothesis.hypothesis_id, observation_event_key, hypothesis.instrument, forecast.origin_session_date, forecast.target_session_date, forecast.horizon, validation.status, expected, actual, actual_return, correct, maturity.evidence_id if matured else None, forecast_id, None, (), status, grade, evaluated_at, evaluated_at)


def candidate_outcome(*, hypothesis, validation, observation_event_key, candidate, promotion_events=(), evaluated_at):
    """模型/策略效果仅由 linked candidate 的 OOF/PromotionEvent 给出。"""
    if hypothesis.kind not in {HypothesisKind.MODEL_CONFIGURATION,HypothesisKind.STRATEGY_CONFIGURATION,HypothesisKind.SYSTEM_CHALLENGE}:
        raise ValueError("candidate outcome requires a model, strategy or mapped system challenge hypothesis")
    if hypothesis.instrument is None:
        raise ValueError("candidate research outcome requires an instrument-bound hypothesis")
    if not isinstance(candidate,LearningCandidateVersion) or any(not isinstance(item,PromotionEvent) or item.candidate_id!=candidate.candidate_id for item in promotion_events):
        raise ValueError("candidate outcome requires frozen linked V2-9 candidate and promotion events")
    decisions={item.decision for item in promotion_events}
    positive=(PromotionDecision.PROMOTE_TO_CHALLENGER,PromotionDecision.PROMOTE_TO_SHADOW,PromotionDecision.PROMOTE_TO_CHAMPION)
    negative={PromotionDecision.REJECT,PromotionDecision.ROLLBACK,PromotionDecision.SUSPEND_NEW_RISK}
    terminal=next((item for item in reversed(positive) if item in decisions),None)
    if terminal is not None:
        status=HypothesisOutcomeStatus.MATURED; grade=f"candidate_{terminal.value}"
    elif decisions & negative:
        status=HypothesisOutcomeStatus.MATURED
        grade="candidate_rolled_back" if PromotionDecision.ROLLBACK in decisions else "candidate_not_improved"
    else:
        status=HypothesisOutcomeStatus.PENDING; grade="pending"
    candidate_id=candidate.candidate_id
    promotions=tuple(sorted(item.promotion_id for item in promotion_events))
    identity = {"hypothesis": hypothesis.hypothesis_id, "event": observation_event_key, "instrument": hypothesis.instrument, "origin": evaluated_at.date(), "target": None, "horizon": None, "trigger": validation.status, "expected":None,"actual": None,"actual_return":None,"direction_correct":None, "maturity": None, "forecast": None, "candidate": candidate_id,"promotions":promotions, "status": status,"evidence_grade":grade}
    return HypothesisOutcome(stable_hash(identity), hypothesis.hypothesis_id, observation_event_key, hypothesis.instrument, evaluated_at.date(), None, None, validation.status, None, None, None, None, None, None, candidate_id, promotions, status, grade, evaluated_at, evaluated_at)


def metric_snapshot(*, market, scope_key, cutoff_at, hypotheses, validations, outcomes, generated_at, dimensions=(), dimension_memberships=()):
    """按输入切片生成 LLM 专属的最小指标；未成熟项不污染准确率。"""
    dimensions=tuple(sorted((str(key),str(value)) for key,value in dimensions))
    memberships={str(hypothesis_id):dict(values) for hypothesis_id,values in dimension_memberships}
    hypotheses=tuple(item for item in hypotheses if _matches_dimensions(item,dimensions,memberships))
    selected_ids={item.hypothesis_id for item in hypotheses}
    selected_memberships={
        item.hypothesis_id:_dimension_values(item,memberships)
        for item in hypotheses
    }
    validations=tuple(item for item in validations if item.hypothesis_id in selected_ids)
    outcomes=tuple(
        item for item in outcomes
        if item.hypothesis_id in selected_ids
        and _matches_dimensions(item,dimensions,selected_memberships)
    )
    statuses = Counter(item.status.value for item in validations)
    matured = [item for item in outcomes if item.status is HypothesisOutcomeStatus.MATURED and item.direction_correct is not None]
    candidate_latest={}
    for item in outcomes:
        if not item.linked_candidate_id:
            continue
        previous=candidate_latest.get(item.linked_candidate_id)
        rank=(item.status is HypothesisOutcomeStatus.MATURED,item.evaluated_at)
        previous_rank=(previous.status is HypothesisOutcomeStatus.MATURED,previous.evaluated_at) if previous else None
        if previous is None or rank>previous_rank:
            candidate_latest[item.linked_candidate_id]=item
    candidates=list(candidate_latest.values())
    metrics = {
        "issued_count": float(len(hypotheses)), "confirmed_count": float(statuses["confirmed"]), "refuted_count": float(statuses["refuted"]), "pending_count": float(statuses["pending"]), "invalid_data_count": float(statuses["invalid_data"]), "matured_direction_count": float(len(matured)), "direction_accuracy": (sum(item.direction_correct for item in matured) / len(matured)) if matured else None, "coverage": (statuses["confirmed"] / len(hypotheses)) if hypotheses else None, "candidate_created_count": float(len(candidates)), "candidate_oof_improved_count": float(sum(item.evidence_grade in {"candidate_promote_to_challenger","candidate_promote_to_shadow","candidate_promote_to_champion"} for item in candidates)), "challenger_count": float(sum(item.evidence_grade=="candidate_promote_to_challenger" for item in candidates)), "shadow_count": float(sum(item.evidence_grade=="candidate_promote_to_shadow" for item in candidates)), "champion_count": float(sum(item.evidence_grade=="candidate_promote_to_champion" for item in candidates)), "rollback_count": float(sum(item.evidence_grade=="candidate_rolled_back" for item in candidates)),
    }
    identity = {"market": market, "scope": scope_key, "cutoff": cutoff_at, "metrics": tuple(sorted(metrics.items())), "dimensions": dimensions}
    return ResearchMetricSnapshot(stable_hash(identity), market, scope_key, cutoff_at, tuple(metrics.items()), generated_at, dimensions)


def _not_applicable(hypothesis, validation, event, at):
    if hypothesis.instrument is None:
        raise ValueError("research outcome requires an instrument-bound hypothesis")
    identity={"hypothesis":hypothesis.hypothesis_id,"event":event,"instrument":hypothesis.instrument,"origin":at.date(),"target":None,"horizon":None,"trigger":validation.status,"expected":None,"actual":None,"actual_return":None,"direction_correct":None,"maturity":None,"forecast":None,"candidate":None,"promotions":(),"status":HypothesisOutcomeStatus.NOT_APPLICABLE,"evidence_grade":"not_applicable"}
    return HypothesisOutcome(stable_hash(identity),hypothesis.hypothesis_id,event,hypothesis.instrument,at.date(),None,None,validation.status,None,None,None,None,None,None,None,(),HypothesisOutcomeStatus.NOT_APPLICABLE,"not_applicable",at,at)


def _superseded(hypothesis, validation, event, forecast, at):
    if hypothesis.instrument is None:
        raise ValueError("research outcome requires an instrument-bound hypothesis")
    identity={"hypothesis":hypothesis.hypothesis_id,"event":event,"instrument":hypothesis.instrument,"origin":forecast.origin_session_date,"target":forecast.target_session_date,"horizon":forecast.horizon,"trigger":validation.status,"expected":None,"actual":None,"actual_return":None,"direction_correct":None,"maturity":None,"forecast":None,"candidate":None,"promotions":(),"status":HypothesisOutcomeStatus.SUPERSEDED,"evidence_grade":"deduplicated"}
    return HypothesisOutcome(stable_hash(identity),hypothesis.hypothesis_id,event,hypothesis.instrument,forecast.origin_session_date,forecast.target_session_date,forecast.horizon,validation.status,None,None,None,None,None,None,None,(),HypothesisOutcomeStatus.SUPERSEDED,"deduplicated",at,at)


def _dimension_values(item,memberships):
    hypothesis_id=getattr(item,"hypothesis_id",None)
    values=dict(memberships.get(str(hypothesis_id),{}))
    instrument=getattr(item,"instrument",None)
    if instrument is not None:
        values["instrument"]=instrument.stable_key
        values["market"]=instrument.market.value
    kind=getattr(item,"kind",None)
    if kind is not None:
        values["hypothesis_kind"]=kind.value
    horizon=getattr(item,"horizon",None)
    if horizon is not None:
        values["horizon"]=str(horizon)
    payload=dict(getattr(item,"payload",()) or ())
    if "registered_model_family" in payload:
        values["model"]=str(payload["registered_model_family"])
    if "registered_strategy_id" in payload:
        values["strategy"]=str(payload["registered_strategy_id"])
    return values


def _matches_dimensions(item,dimensions,memberships):
    if not dimensions:
        return True
    values=_dimension_values(item,memberships)
    payload=dict(getattr(item,"payload",()) or ())
    horizon=getattr(item,"horizon",None)
    for key,expected in dimensions:
        if key=="horizon" and horizon is None and payload.get("horizons"):
            if expected not in {str(value) for value in payload["horizons"]}:
                return False
            continue
        if key not in values:
            raise ValueError(f"research metric dimension {key} requires explicit membership")
        if str(values[key])!=expected:
            return False
    return True
