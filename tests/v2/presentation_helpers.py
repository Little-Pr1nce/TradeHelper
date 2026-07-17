"""V2-11 frozen presentation fixtures built from the real V2-3--V2-8 chain."""
from __future__ import annotations

from dataclasses import replace

from portfolio_helpers import portfolio_batch_many
from risk_helpers import quality
from strategy_helpers import NOW as PIPELINE_NOW, strategy_input
from test_scenario_planner import _forecast, _request
from datetime import timedelta
from decimal import Decimal

from tradehelper_v2.contracts import (
    AccountSnapshot, DirectionProbabilities, EvidenceOrigin, ExecutionPolicy, ForecastDirection,
    ForecastOutcome, ForecastScope, LearningEvidenceGrade, OutcomeStatus,
    RiskPolicy, RiskRequest, StockMetadata, ValuationPrice, ValuationPriceKind,
    WatchlistSnapshot, FreshnessStatus, stable_hash,
)
from tradehelper_v2.execution import OrderIntentFactory
from tradehelper_v2.data.calendar import StaticTradingCalendar
from tradehelper_v2.portfolio import PortfolioDecisionEngine
from tradehelper_v2.presentation.inputs import portfolio_input, single_stock_input
from tradehelper_v2.presentation.report_builder import SingleStockReportBuilder
from tradehelper_v2.risk import RiskOfficer, freeze_account_valuation
from tradehelper_v2.risk.market_rules import default_market_rules
from tradehelper_v2.research.parser import StrictHypothesisParser
from tradehelper_v2.research.validator import DeterministicHypothesisValidator
from tradehelper_v2.strategies import StrategyEngine

from research_helpers import context_response, fact, forecast_item, response_json


def _forecasts(instrument, directions=None, *, confirmed=True, reference_price=100.0):
    directions = directions or {horizon: "bullish" for horizon in (1, 3, 5, 10)}
    base = tuple(_forecast(instrument, horizon, directions[horizon], confirmed=confirmed) for horizon in (1, 3, 5, 10))
    return tuple(replace(item, reference_price=reference_price) for item in _request(instrument, base).forecasts)


def _plans(strategy_bundle):
    return {
        plan.plan_id: plan
        for branch in (
            strategy_bundle.entry_or_add,
            strategy_bundle.reduce_or_exit,
            strategy_bundle.hold,
            strategy_bundle.invalidation,
        )
        for plan in branch.plans
    }


def forecast_outcome(instrument, *, now, status=OutcomeStatus.MATURED, correct=True, regime="risk_on"):
    origin = now.date()
    target = origin + timedelta(days=1)
    maturity_id = "fixture-maturity"
    actual_return = Decimal("0.02") if status is OutcomeStatus.MATURED else None
    reasons = ("LEARNING_MATURED",) if status is OutcomeStatus.MATURED else ("LEARNING_PENDING_TARGET_SESSION",)
    identity = {
        "forecast_event_key": f"fixture:{instrument.stable_key}:{origin}",
        "origin": EvidenceOrigin.ISSUED_ONLINE,
        "maturity": maturity_id if status is OutcomeStatus.MATURED else None,
        "status": status,
        "actual_return": actual_return,
        "revision": maturity_id if status is OutcomeStatus.MATURED else None,
        "reasons": reasons,
    }
    matured = status is OutcomeStatus.MATURED
    return ForecastOutcome(
        stable_hash(identity), identity["forecast_event_key"], instrument, origin, target, 1,
        ForecastScope.STOCK, instrument.stable_key, "analog", "fixture-v1", "tech",
        "a" * 64, "b" * 64, EvidenceOrigin.ISSUED_ONLINE,
        maturity_id if matured else None, ForecastDirection.BULLISH,
        DirectionProbabilities(.7, .2, .1), -.03, .01, .05,
        ForecastDirection.BULLISH if matured and correct else ForecastDirection.BEARISH if matured else None,
        actual_return, Decimal("102") if matured else None, correct if matured else None,
        .1 if matured else None, .3 if matured else None, True if matured else None,
        .01 if matured else None, regime, status,
        LearningEvidenceGrade.HIGH if matured else LearningEvidenceGrade.INSUFFICIENT,
        reasons, now, now,
    )


def single_presentation(
    instrument, *, now, calendar, history_period="3m", position=None,
    directions=None, feature_overrides=None, confirmed=True, availability=None,
    reference_price=100.0, research_hypotheses=(), research_validations=(),
):
    calendar = calendar or StaticTradingCalendar((now.date(),))
    input_value = strategy_input(
        instrument, position=position, directions=directions, feature_overrides=feature_overrides,
        confirmed=confirmed, as_of=now, quality_report=quality(), reference_price=reference_price,
    )
    strategy_bundle = StrategyEngine().build(input_value, generated_at=PIPELINE_NOW)
    account = AccountSnapshot(
        instrument.market, "CNY" if instrument.market.value == "A" else "USD", 10000,
        (position,) if position else (), PIPELINE_NOW,
    )
    prices = {} if position is None else {
        instrument: ValuationPrice(
            instrument, reference_price, input_value.trading_scenario.as_of, "fixture",
            ValuationPriceKind.REFERENCE_CLOSE, FreshnessStatus.NOT_REQUIRED,
        )
    }
    valuation = freeze_account_valuation(account, prices, input_value.trading_scenario.as_of, generated_at=PIPELINE_NOW)
    request = RiskRequest(
        instrument, strategy_bundle, input_value.trading_scenario, quality(), account, valuation,
        availability, (), default_market_rules(instrument.market, instrument.exchange, PIPELINE_NOW),
        None, RiskPolicy(), input_value.trading_scenario.as_of,
    )
    risk_bundle = RiskOfficer().assess(request, generated_at=now)
    order_bundle = OrderIntentFactory(calendar).build_bundle(
        risk_bundle,
        _plans(strategy_bundle),
        {},
        now,
        ExecutionPolicy(),
    )
    scenario = replace(input_value.trading_scenario, generated_at=PIPELINE_NOW)
    forecasts = _forecasts(instrument, directions, confirmed=confirmed, reference_price=reference_price)
    assert {item.forecast_event_key for item in input_value.trading_scenario.horizon_assessments} == {
        item.event_key for item in forecasts
    }
    metadata = StockMetadata(instrument, f"{instrument.code} 公司", "测试行业", None, None, "fixture", now)
    return single_stock_input(
        instrument=instrument,
        analysis_mode=input_value.trading_scenario.mode,
        as_of=input_value.trading_scenario.as_of,
        history_period=history_period,
        metadata=metadata,
        quote_snapshot=None,
        data_quality=request.data_quality,
        feature_snapshot=input_value.feature_snapshot,
        forecasts=forecasts,
        scenario=scenario,
        strategy_bundle=strategy_bundle,
        risk_bundle=risk_bundle,
        order_intent_bundle=order_bundle,
        research_hypotheses=research_hypotheses,
        research_validations=research_validations,
        built_at=now,
    )


def research_validation_states(instrument, now):
    """Build all four research states through the real V2-10 parser/validator."""
    confirmed_fact = fact(instrument, now, key="feature.closed.rsi_14", value=60)
    refuted_fact = fact(instrument, now, key="feature.closed.ma_distance_120", value=0.01)
    pending_fact = fact(instrument, now, key="feature.closed.close", value=100)
    missing_fact = fact(instrument, now, key="feature.closed.volume_ratio", status="missing")
    facts = (confirmed_fact, refuted_fact, pending_fact, missing_fact)
    context, response, _ = context_response(instrument, now, facts=facts)
    items = (
        forecast_item(instrument, confirmed_fact.fact_id, predicate={"op": "gte", "fact_ref": confirmed_fact.fact_id, "constant": 50}),
        forecast_item(instrument, refuted_fact.fact_id, predicate={"op": "gte", "fact_ref": refuted_fact.fact_id, "constant": 0.05}),
        forecast_item(instrument, pending_fact.fact_id, predicate={"op": "crosses_above", "fact_ref": pending_fact.fact_id, "constant": 105}),
        forecast_item(instrument, missing_fact.fact_id, predicate={"op": "gte", "fact_ref": missing_fact.fact_id, "constant": 1.2}),
    )
    hypotheses = StrictHypothesisParser().parse(
        content=response_json(context, items), context=context, response=response,
    )
    validations = tuple(
        DeterministicHypothesisValidator().validate(item, context, evaluated_at=now)
        for item in hypotheses
    )
    return hypotheses, validations


def single_document(instrument, *, now, calendar, history_period="3m", **scenario_options):
    return SingleStockReportBuilder().build(
        single_presentation(
            instrument, now=now, calendar=calendar, history_period=history_period,
            **scenario_options,
        )
    )


def rebuild_single(value, **changes):
    payload = {
        name: getattr(value, name)
        for name in value.__dataclass_fields__
        if name not in {"presentation_id", "source_artifact_refs"}
    }
    payload.update(changes)
    return single_stock_input(**payload)


def portfolio_presentation(instruments, *, now, calendar, history_period="3m"):
    calendar = calendar or StaticTradingCalendar((now.date(),))
    instruments = tuple(instruments)
    batch = portfolio_batch_many(instruments)
    decision = PortfolioDecisionEngine().decide(batch, generated_at=now)
    members = []
    for instrument in instruments:
        input_value = strategy_input(instrument, as_of=now)
        scenario = replace(input_value.trading_scenario, generated_at=PIPELINE_NOW)
        strategy_bundle = StrategyEngine().build(input_value, generated_at=PIPELINE_NOW)
        risk_bundle = next(item for item in batch.risk_bundles if item.instrument == instrument)
        order_bundle = OrderIntentFactory(calendar).build_bundle(
            risk_bundle,
            _plans(strategy_bundle),
            {},
            now,
            ExecutionPolicy(),
        )
        metadata = StockMetadata(instrument, f"{instrument.code} 公司", "测试行业", None, None, "fixture", now)
        members.append(single_stock_input(
            instrument=instrument,
            analysis_mode=input_value.trading_scenario.mode,
            as_of=input_value.trading_scenario.as_of,
            history_period=history_period,
            metadata=metadata,
            quote_snapshot=None,
            data_quality=quality(),
            feature_snapshot=input_value.feature_snapshot,
            forecasts=_forecasts(instrument),
            scenario=scenario,
            strategy_bundle=strategy_bundle,
            risk_bundle=risk_bundle,
            order_intent_bundle=order_bundle,
            built_at=now,
        ))
    watchlist = WatchlistSnapshot(
        stable_hash({"market": batch.market, "instruments": batch.watchlist, "created": now}),
        batch.market,
        batch.watchlist,
        now,
    )
    return portfolio_input(
        market=batch.market,
        analysis_mode=batch.mode,
        as_of=members[0].as_of,
        history_period=history_period,
        account_snapshot=batch.account_snapshot,
        frozen_account_valuation=batch.valuation,
        portfolio_decision_bundle=decision,
        instruments=tuple(members),
        watchlist_snapshot=watchlist,
        built_at=now,
    )
