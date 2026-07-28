"""Purged walk-forward 全链回放编排。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from contracts import (
    ContractViolation,
    EvidenceOrigin,
    Market,
    OutcomeStatus,
    stable_hash,
)


@dataclass(frozen=True, slots=True)
class FoldDefinition:
    fold_id: str
    market: Market
    scope: str
    scope_key: str
    train_start: date
    train_end: date
    embargo_start: date
    embargo_end: date
    test_start: date
    test_end: date
    data_cutoff_at: date
    training_event_hash: str
    selected_forecast_versions: tuple[str, ...] = ()
    selected_strategy_parameter_hashes: tuple[str, ...] = ()
    risk_policy_version: str = ""
    execution_policy_version: str = ""
    portfolio_policy_version: str = ""
    embargo_sessions: int = 10

    def __post_init__(self):
        if not (self.train_start <= self.train_end < self.embargo_start <= self.embargo_end < self.test_start <= self.test_end):
            raise ContractViolation("OOF fold windows overlap or leak")
        if len(self.training_event_hash) != 64 or self.data_cutoff_at != self.train_end or self.embargo_sessions < 10:
            raise ContractViolation("OOF fold needs a frozen mature prefix and ten-session embargo")
        expected = stable_hash(
            {
                "market": self.market,
                "scope": self.scope,
                "scope_key": self.scope_key,
                "train": (self.train_start, self.train_end),
                "embargo": (self.embargo_start, self.embargo_end),
                "test": (self.test_start, self.test_end),
                "cutoff": self.data_cutoff_at,
                "training": self.training_event_hash,
            }
        )
        if self.fold_id != expected:
            raise ContractViolation("OOF fold identity mismatch")
        object.__setattr__(self, "selected_forecast_versions", tuple(sorted(set(self.selected_forecast_versions))))
        object.__setattr__(self, "selected_strategy_parameter_hashes", tuple(sorted(set(self.selected_strategy_parameter_hashes))))


@dataclass(frozen=True, slots=True)
class ReplayAccountPolicy:
    """OOF 账户路径必须显式给出，绝不混入当前账户建议。"""

    policy_version: str
    mode: str
    initial_cash: Decimal
    currency: str
    external_cash_flows: tuple[tuple[date, Decimal], ...]

    def __post_init__(self):
        if (
            self.policy_version != "replay_account_policy_v1"
            or self.mode not in {"user_frozen_snapshot", "standardized_research_notional"}
            or self.initial_cash <= 0
            or self.currency not in {"CNY", "USD"}
        ):
            raise ContractViolation("replay account policy must be explicit and auditable")
        flows = tuple(sorted((item[0], Decimal(item[1])) for item in self.external_cash_flows))
        object.__setattr__(self, "external_cash_flows", flows)


@dataclass(frozen=True, slots=True)
class FoldReplayResult:
    fold_id: str
    training_event_hash: str
    evidence_origin: EvidenceOrigin
    records: tuple[object, ...]
    selected_forecast_versions: tuple[str, ...]
    selected_strategy_parameter_hashes: tuple[str, ...]
    risk_policy_version: str
    execution_policy_version: str
    portfolio_policy_version: str
    generated_at: datetime


class FullChainFoldRunner:
    """固定调用 V2-3 -> V2-8，再由学习投影器生成 outcome。"""

    def __init__(
        self,
        *,
        forecast_stage,
        scenario_stage,
        strategy_stage,
        risk_stage,
        portfolio_stage,
        execution_stage,
        outcome_stage,
    ):
        self.forecast_stage = forecast_stage
        self.scenario_stage = scenario_stage
        self.strategy_stage = strategy_stage
        self.risk_stage = risk_stage
        self.portfolio_stage = portfolio_stage
        self.execution_stage = execution_stage
        self.outcome_stage = outcome_stage

    @staticmethod
    def _instrument_key(value):
        instrument = getattr(value, "instrument", None)
        return None if instrument is None else instrument.stable_key

    @classmethod
    def _validate_forecasts(cls, fold, event, forecasts):
        forecasts = tuple(forecasts)
        forecast_instrument_keys = {cls._instrument_key(item) for item in forecasts}
        expected_instrument_key = (
            fold.scope_key
            if fold.scope == "stock"
            else cls._instrument_key(event) or next(iter(forecast_instrument_keys), None)
        )
        if (
            len(forecasts) != 4
            or {item.horizon for item in forecasts} != {1, 3, 5, 10}
            or forecast_instrument_keys != {expected_instrument_key}
            or any(item.origin_session_date != event.origin_session_date for item in forecasts)
            or len({item.event_key for item in forecasts}) != 4
        ):
            raise ContractViolation("OOF forecast stage did not emit one frozen four-horizon bundle")
        if fold.selected_forecast_versions and any(
            item.model_version not in fold.selected_forecast_versions for item in forecasts
        ):
            raise ContractViolation("OOF forecast stage used an unselected model version")
        return forecasts

    @classmethod
    def _validate_scenario(cls, fold, forecasts, scenario):
        forecast_keys = {item.event_key for item in forecasts}
        instrument_key = cls._instrument_key(forecasts[0])
        assessment_keys = {
            item.forecast_event_key for item in getattr(scenario, "horizon_assessments", ())
        }
        if (
            scenario is None
            or cls._instrument_key(scenario) != instrument_key
            or scenario.origin_session_date != forecasts[0].origin_session_date
            or assessment_keys != forecast_keys
        ):
            raise ContractViolation("OOF scenario stage is not linked to the frozen forecasts")
        return scenario

    @classmethod
    def _validate_strategy(cls, fold, scenario, strategies):
        if (
            strategies is None
            or cls._instrument_key(strategies) != cls._instrument_key(scenario)
            or getattr(strategies, "scenario_id", None) != scenario.scenario_id
            or not getattr(strategies, "bundle_id", None)
        ):
            raise ContractViolation("OOF strategy stage is not linked to the frozen scenario")
        return strategies

    @classmethod
    def _validate_risks(cls, fold, scenario, strategies, risks):
        risks = tuple(risks)
        if (
            not risks
            or any(cls._instrument_key(item) != cls._instrument_key(strategies) for item in risks)
            or any(item.scenario_id != scenario.scenario_id for item in risks)
            or any(item.strategy_bundle_id != strategies.bundle_id for item in risks)
        ):
            raise ContractViolation("OOF risk stage is not linked to the frozen strategy bundle")
        return risks

    @staticmethod
    def _allocations(portfolio):
        return tuple(portfolio.conservative.allocations) + tuple(portfolio.aggressive.allocations)

    @classmethod
    def _validate_portfolio(cls, fold, risks, portfolio):
        risk_decisions = {
            decision.decision_id
            for bundle in risks
            for decision in bundle.decisions
        }
        allocations = cls._allocations(portfolio) if portfolio is not None else ()
        allocation_decisions = {item.decision_id for item in allocations}
        if (
            portfolio is None
            or portfolio.market is not fold.market
            or not portfolio.portfolio_bundle_id
            or allocation_decisions != risk_decisions
        ):
            raise ContractViolation("OOF portfolio stage is not linked to every frozen risk decision")
        return portfolio

    @classmethod
    def _validate_executions(cls, portfolio, executions):
        executions = tuple(executions)
        allocations = cls._allocations(portfolio)
        allowed = {
            (item.decision_id, item.plan_id, item.instrument.stable_key)
            for item in allocations
        }
        for item in executions:
            intents = tuple(getattr(item, "intents", ()))
            fills = tuple(getattr(item, "fills", ()))
            if not intents and not fills and not getattr(item, "records", None):
                raise ContractViolation("OOF execution stage emitted an unauditable object")
            for fact in (*intents, *fills):
                identity = (
                    fact.decision_id,
                    fact.plan_id,
                    fact.instrument.stable_key,
                )
                if identity not in allowed:
                    raise ContractViolation("OOF execution fact is not linked to a portfolio allocation")
        return executions

    @staticmethod
    def _validate_outcomes(forecasts, scenario, strategies, portfolio, emitted):
        emitted = tuple(emitted)
        if not emitted:
            raise ContractViolation("OOF outcome stage must emit an auditable result")
        forecast_keys = {item.event_key for item in forecasts}
        plan_ids = {
            plan.plan_id
            for branch in (
                strategies.entry_or_add,
                strategies.reduce_or_exit,
                strategies.hold,
                strategies.invalidation,
            )
            for plan in branch.plans
        }
        decision_ids = {
            item.decision_id
            for item in FullChainFoldRunner._allocations(portfolio)
        }
        for item in emitted:
            linked = False
            if getattr(item, "forecast_event_key", None) in forecast_keys:
                linked = True
            if getattr(item, "scenario_id", None) == scenario.scenario_id:
                linked = True
            if (
                getattr(item, "plan_id", None) in plan_ids
                and getattr(item, "decision_id", None) in decision_ids
            ):
                linked = True
            if getattr(item, "portfolio_bundle_id", None) == portfolio.portfolio_bundle_id:
                linked = True
            if not linked:
                raise ContractViolation("OOF outcome is not linked to the frozen full-chain artifacts")
        return emitted

    def run_fold(self, fold, training, testing, account_policy):
        records = []
        generated_at = None
        for event in sorted(testing, key=lambda item: (item.origin_session_date, item.event_key)):
            forecasts = self._validate_forecasts(
                fold, event, self.forecast_stage(fold, event, training)
            )
            scenario = self._validate_scenario(
                fold, forecasts, self.scenario_stage(fold, event, forecasts)
            )
            strategies = self._validate_strategy(
                fold, scenario, self.strategy_stage(fold, event, scenario)
            )
            risks = self._validate_risks(
                fold,
                scenario,
                strategies,
                self.risk_stage(fold, event, strategies, account_policy),
            )
            portfolio = self._validate_portfolio(
                fold,
                risks,
                self.portfolio_stage(fold, event, risks, account_policy),
            )
            executions = self._validate_executions(
                portfolio,
                self.execution_stage(fold, event, portfolio, account_policy),
            )
            emitted = tuple(
                self.outcome_stage(
                    fold,
                    event,
                    forecasts,
                    scenario,
                    strategies,
                    risks,
                    portfolio,
                    executions,
                )
            )
            emitted = self._validate_outcomes(
                forecasts, scenario, strategies, portfolio, emitted
            )
            if any(getattr(item, "evidence_origin", None) is not EvidenceOrigin.RECONSTRUCTED_OOF for item in emitted):
                raise ContractViolation("full-chain OOF outcomes must be marked reconstructed")
            records.extend(emitted)
            if emitted:
                generated_at = max(item.generated_at for item in emitted)
        return FoldReplayResult(
            fold.fold_id,
            fold.training_event_hash,
            EvidenceOrigin.RECONSTRUCTED_OOF,
            tuple(records),
            fold.selected_forecast_versions,
            fold.selected_strategy_parameter_hashes,
            fold.risk_policy_version,
            fold.execution_policy_version,
            fold.portfolio_policy_version,
            generated_at or datetime.min.replace(tzinfo=timezone.utc),
        )


def validate_folds(folds):
    ordered = tuple(sorted(folds, key=lambda item: item.test_start))
    if len(ordered) < 3 or any(left.test_end >= right.test_start for left, right in zip(ordered, ordered[1:])):
        raise ContractViolation("OOF requires three non-overlapping sequential folds")
    if len({(item.market, item.scope, item.scope_key) for item in ordered}) != 1:
        raise ContractViolation("OOF folds cannot mix market or scope")
    if any(left.train_end >= right.test_start for left, right in zip(ordered, ordered[1:])):
        raise ContractViolation("OOF folds cannot train on a later test period")
    return ordered


def _event_available_at(item):
    value = getattr(item, "available_at", None) or getattr(item, "evaluated_at", None)
    if value is None:
        raise ContractViolation("OOF events require an auditable availability timestamp")
    return value


class WalkForwardReplayer:
    def run(self, folds, events, runner, *, account_policy: ReplayAccountPolicy, cancelled=lambda: False):
        if not isinstance(account_policy, ReplayAccountPolicy):
            raise ContractViolation("OOF replay requires explicit account policy")
        if not isinstance(runner, FullChainFoldRunner):
            raise ContractViolation("OOF replay requires the fixed full-chain runner")
        results = []
        for fold in validate_folds(folds):
            if cancelled():
                break
            training = tuple(
                item
                for item in events
                if fold.train_start <= item.origin_session_date <= fold.train_end
                and item.target_session_date <= fold.train_end
                and getattr(item, "status", None) is OutcomeStatus.MATURED
                and _event_available_at(item).date() <= fold.data_cutoff_at
            )
            training = tuple(sorted(training, key=lambda item: (item.origin_session_date, item.target_session_date, item.event_key)))
            actual_training_hash = stable_hash(tuple(item.event_key for item in training))
            if actual_training_hash != fold.training_event_hash:
                raise ContractViolation("OOF training event hash does not match frozen fold")
            testing = tuple(
                sorted(
                    (item for item in events if fold.test_start <= item.origin_session_date <= fold.test_end),
                    key=lambda item: (item.origin_session_date, item.event_key),
                )
            )
            result = runner.run_fold(fold, training, testing, account_policy)
            if (
                result.fold_id != fold.fold_id
                or result.training_event_hash != fold.training_event_hash
                or result.evidence_origin is not EvidenceOrigin.RECONSTRUCTED_OOF
                or result.selected_forecast_versions != fold.selected_forecast_versions
                or result.selected_strategy_parameter_hashes != fold.selected_strategy_parameter_hashes
                or result.risk_policy_version != fold.risk_policy_version
                or result.execution_policy_version != fold.execution_policy_version
                or result.portfolio_policy_version != fold.portfolio_policy_version
            ):
                raise ContractViolation("OOF fold result does not match frozen versions and policies")
            for record in result.records:
                model_version = getattr(record, "model_version", None)
                if model_version is not None and fold.selected_forecast_versions and model_version not in fold.selected_forecast_versions:
                    raise ContractViolation("OOF outcome used an unselected forecast version")
                parameter_hash = getattr(record, "parameter_hash", None)
                if parameter_hash is not None and fold.selected_strategy_parameter_hashes and parameter_hash not in fold.selected_strategy_parameter_hashes:
                    raise ContractViolation("OOF outcome used an unselected strategy parameter set")
            results.extend(result.records)
        return tuple(results)
