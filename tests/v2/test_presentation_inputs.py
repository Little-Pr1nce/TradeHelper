"""UX01, UX06--UX08: source closure, watchlists, and display safety."""
from dataclasses import replace
from datetime import timedelta

import pytest

from contracts import ContractViolation, StockMetadata, WatchlistSnapshot, stable_hash
from data.repository import SQLiteRepository
from presentation_helpers import rebuild_single
from test_presentation_contracts import _input


def test_ux01_cross_market_or_timestamp_artifact_is_rejected(us_instrument, a_instrument, now):
    value = _input(us_instrument, now)
    foreign = StockMetadata(a_instrument, "贵州茅台", None, None, None, "fixture", now)
    with pytest.raises(ContractViolation):
        replace(value, metadata=foreign)
    with pytest.raises(ContractViolation):
        replace(value, as_of=value.as_of - timedelta(minutes=1))


def test_ux06_watchlist_is_immutable_and_latest_recovers(us_instrument, now, tmp_path):
    repo = SQLiteRepository(tmp_path / "report.sqlite")
    first = WatchlistSnapshot(
        stable_hash({"market": us_instrument.market, "instruments": (us_instrument,), "created": now}),
        us_instrument.market, (us_instrument,), now,
    )
    repo.save_watchlist_snapshot(first)
    assert repo.latest_watchlist_snapshot(us_instrument.market) == first
    assert repo.get_watchlist_snapshot(first.watchlist_id) == first
    repo.close()


def test_ux07_chart_hash_is_stable():
    from contracts import ChartKind, ChartSpec, stable_hash
    chart = ChartSpec("chart", ChartKind.CALIBRATION, "校准", "预测置信度", "实际发生频率", (("模型", (("0.5", .4),)),), (("0.5", .5),), 1, ("a", "b"), "解释")
    assert chart.content_hash == stable_hash({"kind": chart.chart_kind, "title": "校准", "x": "预测置信度", "y": "实际发生频率", "series": chart.series, "baseline": chart.baseline, "samples": 1, "range": ("a", "b"), "interpretation": "解释", "empty": None})


def test_ux08_secret_and_path_cannot_enter_presentation_contract(us_instrument, now):
    value = _input(us_instrument, now)
    for refs in (("secret:token",), ("/Users/private/report",)):
        with pytest.raises(ContractViolation):
            replace(value, source_artifact_refs=refs)


def test_future_visible_fact_is_rejected(us_instrument, now, quote_factory):
    value = _input(us_instrument, now)
    future_quote = quote_factory(
        us_instrument,
        observed_at=value.as_of + timedelta(minutes=1),
        fetched_at=now,
    )
    with pytest.raises(ContractViolation, match="unavailable at the analysis cutoff"):
        rebuild_single(value, quote_snapshot=future_quote)
