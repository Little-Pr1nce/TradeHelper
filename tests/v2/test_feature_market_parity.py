from __future__ import annotations

from tradehelper_v2.features import FeatureBuilder

from feature_helpers import bars, calendar, inputs


def test_f09_a_and_us_closed_feature_contracts_are_identical(a_instrument, us_instrument, now) -> None:
    a_bars, us_bars = bars(a_instrument, 120, fetched_at=now), bars(us_instrument, 120, fetched_at=now)
    a_snapshot = FeatureBuilder(calendar(a_bars)).build(inputs(a_instrument, now, a_bars))
    us_snapshot = FeatureBuilder(calendar(us_bars)).build(inputs(us_instrument, now, us_bars))
    a_values = {item.name: item.value for item in a_snapshot.values if item.name.startswith("closed.")}
    us_values = {item.name: item.value for item in us_snapshot.values if item.name.startswith("closed.")}
    assert a_values == us_values
