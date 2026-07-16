"""Linked audit fixtures for the fixed V2-3 -> V2-8 OOF replay chain."""
from types import SimpleNamespace

from tradehelper_v2.contracts import EvidenceOrigin
from tradehelper_v2.learning.replay import FullChainFoldRunner


def linked_full_chain_runner(seen=None, *, corrupt_scenario=False):
    seen = seen if seen is not None else []

    def forecast_stage(fold, event, training):
        seen.append((fold, training, event))
        instrument = getattr(event, "instrument", None) or SimpleNamespace(
            stable_key=fold.scope_key
        )
        return tuple(
            SimpleNamespace(
                instrument=instrument,
                horizon=horizon,
                origin_session_date=event.origin_session_date,
                event_key=f"{event.event_key}:forecast:{horizon}",
                model_version="fixture_forecast_v1",
            )
            for horizon in (1, 3, 5, 10)
        )

    def scenario_stage(fold, event, forecasts):
        keys = tuple(item.event_key for item in forecasts)
        if corrupt_scenario:
            keys = keys[:-1] + ("unlinked-forecast",)
        return SimpleNamespace(
            instrument=forecasts[0].instrument,
            origin_session_date=event.origin_session_date,
            scenario_id=f"{event.event_key}:scenario",
            horizon_assessments=tuple(
                SimpleNamespace(forecast_event_key=key) for key in keys
            ),
        )

    def strategy_stage(fold, event, scenario):
        plan = SimpleNamespace(plan_id=f"{event.event_key}:plan")
        empty_branch = SimpleNamespace(plans=())
        return SimpleNamespace(
            instrument=scenario.instrument,
            scenario_id=scenario.scenario_id,
            bundle_id=f"{event.event_key}:strategy",
            entry_or_add=SimpleNamespace(plans=(plan,)),
            reduce_or_exit=empty_branch,
            hold=empty_branch,
            invalidation=empty_branch,
        )

    def risk_stage(fold, event, strategies, account):
        decision = SimpleNamespace(
            decision_id=f"{event.event_key}:decision",
            plan_id=f"{event.event_key}:plan",
        )
        return (
            SimpleNamespace(
                instrument=strategies.instrument,
                scenario_id=strategies.scenario_id,
                strategy_bundle_id=strategies.bundle_id,
                decisions=(decision,),
            ),
        )

    def portfolio_stage(fold, event, risks, account):
        decision = risks[0].decisions[0]
        allocation = SimpleNamespace(
            decision_id=decision.decision_id,
            plan_id=decision.plan_id,
            instrument=risks[0].instrument,
        )
        return SimpleNamespace(
            market=fold.market,
            portfolio_bundle_id=f"{event.event_key}:portfolio",
            conservative=SimpleNamespace(allocations=(allocation,)),
            aggressive=SimpleNamespace(allocations=(allocation,)),
        )

    def execution_stage(fold, event, portfolio, account):
        return (SimpleNamespace(intents=(), fills=(), records=("audited",)),)

    def outcome_stage(
        fold,
        event,
        forecasts,
        scenario,
        strategies,
        risks,
        portfolio,
        executions,
    ):
        return (
            SimpleNamespace(
                evidence_origin=EvidenceOrigin.RECONSTRUCTED_OOF,
                generated_at=event.available_at,
                portfolio_bundle_id=portfolio.portfolio_bundle_id,
            ),
        )

    return FullChainFoldRunner(
        forecast_stage=forecast_stage,
        scenario_stage=scenario_stage,
        strategy_stage=strategy_stage,
        risk_stage=risk_stage,
        portfolio_stage=portfolio_stage,
        execution_stage=execution_stage,
        outcome_stage=outcome_stage,
    )
