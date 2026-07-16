"""PO24-PO29、PO43：点时相关性与当前组合风险证据。"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from conftest import make_bar
from portfolio_helpers import correlation_for, portfolio_batch, portfolio_batch_many, rebuild_batch
from strategy_helpers import position
from tradehelper_v2.contracts import CorrelationStatus, Market, RiskProfile
from tradehelper_v2.portfolio import (
    PortfolioDecisionEngine, build_correlation_snapshot, build_portfolio_risk_snapshot,
)


def _bars(instrument, count, now, *, slope=1):
    start = date(2026, 5, 1)
    return tuple(replace(make_bar(instrument, start + timedelta(days=index), 100 + slope * index),
                         fetched_at=now) for index in range(count))


def test_po24_correlation_uses_only_visible_consistent_daily_bars(us_instrument, now):
    bars = list(_bars(us_instrument, 25, now))
    bars.append(replace(make_bar(us_instrument, date(2026, 7, 9), 999),
                        fetched_at=now + timedelta(seconds=1)))
    snapshot = build_correlation_snapshot(
        market=Market.US, universe=(us_instrument,), bars_by_instrument={us_instrument: bars},
        policy=portfolio_batch(us_instrument).portfolio_policy, cutoff_at=now,
        source_batch_hash="visible-bars", generated_at=now,
    )
    assert snapshot.instrument_risks[0].sample_count == 24
    conflicting = list(_bars(us_instrument, 25, now))
    conflicting[-1] = replace(conflicting[-1], corporate_action_version="different")
    degraded = build_correlation_snapshot(
        market=Market.US, universe=(us_instrument,), bars_by_instrument={us_instrument: conflicting},
        policy=portfolio_batch(us_instrument).portfolio_policy, cutoff_at=now,
        source_batch_hash="conflicting-bars", generated_at=now,
    )
    assert degraded.status is CorrelationStatus.UNAVAILABLE


def test_po25_under_twenty_overlapping_returns_stays_missing(us_instrument, now):
    other = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    snapshot = build_correlation_snapshot(
        market=Market.US, universe=(us_instrument, other),
        bars_by_instrument={us_instrument: _bars(us_instrument, 12, now), other: _bars(other, 12, now)},
        policy=portfolio_batch(us_instrument).portfolio_policy, cutoff_at=now,
        source_batch_hash="short-bars", generated_at=now,
    )
    assert snapshot.pairs[0].overlapping_samples == 11
    assert snapshot.pairs[0].coefficient is None


def test_po26_high_correlation_neighborhood_stays_below_thirty_five_percent(us_instrument, now):
    other = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    batch = portfolio_batch_many((us_instrument, other))
    batch = rebuild_batch(batch, correlation_snapshot=correlation_for((us_instrument, other), coefficient=Decimal("0.90")))
    result = PortfolioDecisionEngine().decide(batch, now).aggressive
    assert result.reservation_snapshot.reserved_entry_notional <= result.current_risk_snapshot.equity * Decimal("0.35")


def test_po27_missing_correlation_counts_as_unknown_and_applies_half_multiplier(us_instrument, now):
    other = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    result = PortfolioDecisionEngine().decide(portfolio_batch_many((us_instrument, other)), now).aggressive
    selected = [item for item in result.allocations if item.final_requested_shares > 0]
    assert selected
    assert any("PORTFOLIO_CORRELATION_MULTIPLIER_APPLIED" in item.reason_codes for item in selected)
    assert result.reservation_snapshot.reserved_entry_notional <= result.current_risk_snapshot.equity * Decimal("0.35")


def test_po28_low_or_negative_correlation_does_not_apply_group_cap(us_instrument, now):
    other = type(us_instrument).from_code("MSFT", Market.US, "XNAS")
    batch = portfolio_batch_many((us_instrument, other))
    batch = rebuild_batch(batch, correlation_snapshot=correlation_for((us_instrument, other), coefficient=Decimal("-0.20")))
    result = PortfolioDecisionEngine().decide(batch, now).aggressive
    assert all("PORTFOLIO_HIGH_CORRELATION_LIMITED" not in item.reason_codes for item in result.allocations)
    assert result.reservation_snapshot.reserved_entry_notional > result.current_risk_snapshot.equity * Decimal("0.35")


def test_po29_hhi_warning_is_evidence_not_an_invented_exit(us_instrument, now):
    batch = portfolio_batch(us_instrument, position=position(us_instrument), cash=Decimal("0"))
    result = PortfolioDecisionEngine().decide(batch, now).conservative
    assert result.current_risk_snapshot.hhi == Decimal("1")
    assert "PORTFOLIO_HHI_WARNING" in result.current_risk_snapshot.reason_codes
    upstream = {item.execution_decision.decision_id for item in batch.candidates}
    assert all(item.decision_id in upstream for item in result.allocations)


def test_po43_equity_weights_and_hhi_recompute_from_one_frozen_valuation(us_instrument, now):
    batch = portfolio_batch(us_instrument, position=position(us_instrument), cash=Decimal("1000"))
    snapshot = build_portfolio_risk_snapshot(
        valuation=batch.valuation, holding_risks=batch.holding_risks,
        correlation_snapshot=batch.correlation_snapshot, policy=batch.portfolio_policy,
        calculated_at=now,
    )
    assert snapshot.equity == Decimal("2000")
    assert snapshot.invested_pct == Decimal("0.5")
    assert snapshot.weights_by_instrument == ((us_instrument, Decimal("0.5")),)
    assert snapshot.hhi == Decimal("0.25")
