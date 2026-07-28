"""Build strongly typed presentation inputs from frozen upstream contracts."""
from __future__ import annotations

from contracts import (
    PortfolioPresentationInput,
    SingleStockPresentationInput,
    presentation_source_refs,
    stable_hash,
)
from contracts.presentation import PRESENTATION_POLICY_REF


def single_stock_input(
    *,
    instrument,
    analysis_mode,
    as_of,
    history_period,
    metadata,
    quote_snapshot,
    data_quality,
    feature_snapshot,
    forecasts,
    scenario,
    strategy_bundle,
    risk_bundle,
    order_intent_bundle,
    learning_evidence=(),
    forecast_outcomes=(),
    strategy_outcomes=(),
    joint_outcomes=(),
    metric_snapshots=(),
    research_hypotheses=(),
    research_validations=(),
    research_outcomes=(),
    research_metric_snapshots=(),
    news_summary=(),
    fundamental_summary=None,
    built_at,
):
    forecasts = tuple(forecasts)
    learning_evidence = tuple(learning_evidence)
    forecast_outcomes = tuple(forecast_outcomes)
    strategy_outcomes = tuple(strategy_outcomes)
    joint_outcomes = tuple(joint_outcomes)
    metric_snapshots = tuple(metric_snapshots)
    research_hypotheses = tuple(research_hypotheses)
    research_validations = tuple(research_validations)
    research_outcomes = tuple(research_outcomes)
    research_metric_snapshots = tuple(research_metric_snapshots)
    news_summary = tuple(news_summary)
    refs = tuple(sorted((*presentation_source_refs(
        metadata, quote_snapshot, data_quality, feature_snapshot, forecasts, scenario,
        strategy_bundle, risk_bundle, order_intent_bundle, learning_evidence,
        forecast_outcomes, strategy_outcomes, joint_outcomes, metric_snapshots,
        research_hypotheses, research_validations, research_outcomes,
        research_metric_snapshots, news_summary, fundamental_summary,
    ), PRESENTATION_POLICY_REF)))
    identity = {
        "instrument": instrument, "mode": analysis_mode, "as_of": as_of,
        "history": history_period, "metadata": metadata, "quote": quote_snapshot,
        "quality": data_quality, "feature": feature_snapshot,
        "forecasts": tuple(sorted(forecasts, key=lambda item: item.horizon)),
        "scenario": scenario, "strategy": strategy_bundle, "risk": risk_bundle,
        "orders": order_intent_bundle, "learning_evidence": learning_evidence,
        "forecast_outcomes": forecast_outcomes, "strategy_outcomes": strategy_outcomes,
        "joint_outcomes": joint_outcomes, "metric_snapshots": metric_snapshots,
        "research_hypotheses": research_hypotheses,
        "research_validations": research_validations,
        "research_outcomes": research_outcomes,
        "research_metric_snapshots": research_metric_snapshots,
        "news": news_summary, "fundamental": fundamental_summary, "refs": refs,
    }
    return SingleStockPresentationInput(
        stable_hash(identity), instrument, analysis_mode, as_of, history_period,
        metadata, quote_snapshot, data_quality, feature_snapshot, forecasts, scenario,
        strategy_bundle, risk_bundle, order_intent_bundle, learning_evidence,
        forecast_outcomes, strategy_outcomes, joint_outcomes, metric_snapshots,
        research_hypotheses, research_validations, research_outcomes,
        research_metric_snapshots, news_summary, fundamental_summary, refs, built_at,
    )


def portfolio_input(
    *,
    market,
    analysis_mode,
    as_of,
    history_period,
    account_snapshot,
    frozen_account_valuation,
    portfolio_decision_bundle,
    instruments,
    watchlist_snapshot=None,
    portfolio_learning_evidence=(),
    portfolio_research_evidence=(),
    portfolio_research_hypotheses=(),
    portfolio_research_validations=(),
    research_status="pending",
    research_chunk_count=0,
    research_completed_chunk_count=0,
    research_failure_reasons=(),
    built_at,
):
    instruments = tuple(instruments)
    portfolio_learning_evidence = tuple(portfolio_learning_evidence)
    portfolio_research_evidence = tuple(portfolio_research_evidence)
    portfolio_research_hypotheses = tuple(portfolio_research_hypotheses)
    portfolio_research_validations = tuple(portfolio_research_validations)
    refs = set(presentation_source_refs(
        account_snapshot, frozen_account_valuation, portfolio_decision_bundle,
        watchlist_snapshot, portfolio_learning_evidence, portfolio_research_evidence,
        portfolio_research_hypotheses, portfolio_research_validations,
    ))
    refs.add(PRESENTATION_POLICY_REF)
    refs.update(ref for item in instruments for ref in item.source_artifact_refs)
    refs = tuple(sorted(refs))
    identity = {
        "market": market, "mode": analysis_mode, "as_of": as_of,
        "history": history_period, "account": account_snapshot,
        "valuation": frozen_account_valuation, "portfolio": portfolio_decision_bundle,
        "instruments": tuple(sorted(instruments, key=lambda item: item.instrument.stable_key)),
        "watchlist": watchlist_snapshot, "learning": portfolio_learning_evidence,
        "research": portfolio_research_evidence,
        "research_hypotheses": portfolio_research_hypotheses,
        "research_validations": portfolio_research_validations,
        "research_status": research_status,
        "research_chunk_count": research_chunk_count,
        "research_completed_chunk_count": research_completed_chunk_count,
        "research_failure_reasons": tuple(sorted(set(research_failure_reasons))),
        "refs": refs,
    }
    return PortfolioPresentationInput(
        stable_hash(identity), market, analysis_mode, as_of, history_period,
        account_snapshot, frozen_account_valuation, portfolio_decision_bundle,
        instruments, watchlist_snapshot, portfolio_learning_evidence,
        portfolio_research_evidence, portfolio_research_hypotheses,
        portfolio_research_validations, research_status, research_chunk_count,
        research_completed_chunk_count, tuple(sorted(set(research_failure_reasons))), refs, built_at,
    )
