from __future__ import annotations

from dataclasses import replace

import pytest

from features.technical import closed_features

from feature_helpers import bars


def test_f01_returns_and_moving_averages_have_fixed_answers(us_instrument, now) -> None:
    values = bars(us_instrument, 120, fetched_at=now)
    features = {item.name: item for item in closed_features(values, now)}
    assert features["closed.return_5"].value == pytest.approx(5 / 214, abs=1e-12)
    assert features["closed.ma_20"].value == pytest.approx(209.5, abs=1e-12)
    assert features["closed.ma_distance_20"].value == pytest.approx(219 / 209.5 - 1, abs=1e-12)


def test_f02_indicators_have_fixed_ohlcv_answers(us_instrument, now) -> None:
    values = bars(us_instrument, 40, fetched_at=now)
    features = {item.name: item for item in closed_features(values, now)}
    assert features["closed.rsi_14"].value == pytest.approx(100.0, abs=1e-10)
    assert features["closed.atr_pct_14"].value == pytest.approx(0.02877697841726619, abs=1e-10)
    assert features["closed.macd_dif_pct"].value == pytest.approx(0.04594767854381173, abs=1e-10)
    assert features["closed.macd_signal_pct"].value == pytest.approx(0.04420062631548991, abs=1e-10)
    assert features["closed.macd_hist_pct"].value == pytest.approx(0.0017470522283218183, abs=1e-10)
    assert features["closed.bb_pct_20"].value == pytest.approx(0.911877235523957, abs=1e-10)
    assert features["closed.realized_vol_20"].value == pytest.approx(0.005668495793299555, abs=1e-10)


def test_f02_flat_prices_produce_neutral_rsi(us_instrument, now) -> None:
    values = tuple(
        replace(item, open=100.0, high=101.0, low=99.0, close=100.0)
        for item in bars(us_instrument, 20, fetched_at=now)
    )
    features = {item.name: item for item in closed_features(values, now)}
    assert features["closed.rsi_14"].value == 50.0


def test_f03_insufficient_history_never_substitutes_zero(us_instrument, now) -> None:
    short = {item.name: item for item in closed_features(bars(us_instrument, 19, fetched_at=now), now)}
    enough = {item.name: item for item in closed_features(bars(us_instrument, 20, fetched_at=now), now)}
    assert short["closed.ma_20"].value is None and short["closed.ma_20"].status.value == "insufficient_history"
    assert short["closed.bb_pct_20"].value is None and short["closed.bb_pct_20"].status.value == "insufficient_history"
    assert enough["closed.ma_20"].value is not None
    assert enough["closed.bb_pct_20"].value is not None
    assert enough["closed.ma_120"].value is None and enough["closed.ma_120"].status.value == "insufficient_history"
