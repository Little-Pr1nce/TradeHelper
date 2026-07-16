"""六层配对反事实归因。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradehelper_v2.contracts import ContractViolation


@dataclass(frozen=True, slots=True)
class CounterfactualObservation:
    value: Decimal | None
    event_keys: tuple[str, ...]
    price_path_hash: str
    fee_version: str
    hard_constraint_hash: str
    policy_version: str

    def __post_init__(self):
        if (
            len(self.price_path_hash) != 64
            or len(self.hard_constraint_hash) != 64
            or not self.fee_version
            or not self.policy_version
            or tuple(sorted(set(self.event_keys))) != self.event_keys
        ):
            raise ContractViolation("counterfactual observation is not auditable")
        if self.value is not None:
            object.__setattr__(self, "value", Decimal(str(self.value)))


def paired_contribution(*, factual, counterfactual):
    if not isinstance(factual, CounterfactualObservation) or not isinstance(counterfactual, CounterfactualObservation):
        raise ContractViolation("paired attribution requires frozen counterfactual observations")
    if (
        factual.event_keys != counterfactual.event_keys
        or factual.price_path_hash != counterfactual.price_path_hash
        or factual.fee_version != counterfactual.fee_version
        or factual.hard_constraint_hash != counterfactual.hard_constraint_hash
    ):
        raise ContractViolation("counterfactual paths are not paired")
    if factual.value is None or counterfactual.value is None:
        return {"status": "unavailable", "value": None, "reason": "LEARNING_COUNTERFACTUAL_UNAVAILABLE"}
    return {
        "status": "paired",
        "value": factual.value - counterfactual.value,
        "reason": "LEARNING_COUNTERFACTUAL_PAIRED",
    }


def risk_contribution(strategy_path, risk_path):
    return paired_contribution(factual=risk_path, counterfactual=strategy_path)


def execution_contribution(mid_path, filled_path):
    return paired_contribution(factual=filled_path, counterfactual=mid_path)


def portfolio_contribution(single_plan_path, portfolio_path):
    return paired_contribution(factual=portfolio_path, counterfactual=single_plan_path)


def forecast_contribution(candidate_path, baseline_path):
    return paired_contribution(factual=baseline_path, counterfactual=candidate_path)


def scenario_contribution(policy_path, baseline_path):
    return paired_contribution(factual=policy_path, counterfactual=baseline_path)


def strategy_contribution(strategy_path, baseline_path):
    return paired_contribution(factual=strategy_path, counterfactual=baseline_path)
