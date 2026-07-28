"""情景账：用版本化多周期映射评价预测到情景的翻译。"""
from __future__ import annotations

from dataclasses import dataclass

from contracts import (
    ForecastDirection,
    LearningEvidenceGrade,
    OutcomeStatus,
    ScenarioOutcome,
    stable_hash,
)


@dataclass(frozen=True, slots=True)
class ScenarioOutcomePolicy:
    policy_version: str = "scenario_outcome_policy_v1"

    @staticmethod
    def _band(directions):
        values = set(directions)
        if ForecastDirection.BULLISH in values and ForecastDirection.BEARISH in values:
            return "conflict"
        if ForecastDirection.BULLISH in values:
            return "bullish"
        if ForecastDirection.BEARISH in values:
            return "bearish"
        return "range"

    def realized_bias(self, outcomes):
        by_horizon = {item.horizon: item.actual_direction for item in outcomes}
        tactical = self._band((by_horizon[1], by_horizon[3]))
        swing = self._band((by_horizon[5], by_horizon[10]))
        if "conflict" in {tactical, swing}:
            return "uncertain"
        if tactical == swing:
            return "range" if tactical == "range" else tactical
        if swing in {"bullish", "bearish"}:
            return swing
        if tactical in {"bullish", "bearish"}:
            return tactical
        return "range"


def scenario_outcome(*, scenario, forecast_outcomes, evidence_origin, generated_at, policy=None):
    policy = policy or ScenarioOutcomePolicy()
    outcomes = tuple(sorted(forecast_outcomes, key=lambda item: item.horizon))
    horizons = {item.horizon for item in outcomes}
    complete = (
        len(outcomes) == 4
        and horizons == {1, 3, 5, 10}
        and all(item.status is OutcomeStatus.MATURED and item.actual_direction is not None for item in outcomes)
    )
    if not complete:
        status = OutcomeStatus.PENDING
        realized = None
        grade = LearningEvidenceGrade.INSUFFICIENT
        reasons = ("LEARNING_PENDING_TARGET_SESSION",)
    else:
        realized = policy.realized_bias(outcomes)
        status = OutcomeStatus.MATURED
        grade = LearningEvidenceGrade.HIGH
        reasons = ("LEARNING_SCENARIO_ATTRIBUTED",)
    ids = tuple(sorted({item.forecast_outcome_id for item in outcomes}))
    identity = {
        "scenario": scenario.scenario_id,
        "forecast_outcomes": ids,
        "expected": scenario.bias.value,
        "realized": realized,
        "policy": policy.policy_version,
        "origin": evidence_origin,
        "status": status,
        "reasons": reasons,
    }
    return ScenarioOutcome(
        stable_hash(identity),
        scenario.scenario_id,
        scenario.instrument,
        ids,
        scenario.bias.value,
        realized,
        policy.policy_version,
        evidence_origin,
        status,
        grade,
        reasons,
        generated_at,
        generated_at,
    )
