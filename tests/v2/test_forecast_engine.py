from dataclasses import replace
from datetime import date, timedelta

import pytest

from contracts import (
    AdjustmentMode,
    CanonicalBar,
    DataCapabilities,
    DataQualityReport,
    ForecastAvailability,
    ForecastRequest,
    QualityAction,
    QualityStatus,
)
from data.calendar import StaticTradingCalendar
from data.repository import SQLiteRepository
from features.snapshot import FeatureBuilder
from forecast.engine import ForecastEngine
from forecast.registry import ForecastRegistry
from tests.v2.forecast_helpers import synthetic_samples
from feature_helpers import bars, calendar as feature_calendar, inputs


def _request(instrument, now, *, data_quality=None):
    current = synthetic_samples(instrument, count=1, start=date(2026, 7, 10))[0]
    bar = CanonicalBar(
        instrument, current.origin_session_date, 99.0, 102.0, 98.0, 100.0, 1000,
        AdjustmentMode.FRONT_ADJUSTED, "fixture", now,
    )
    return ForecastRequest(current.feature_snapshot, bar, now, (1,), data_quality), current


def test_fc02_calendar_failure_does_not_invent_target_date(us_instrument, now) -> None:
    request, _ = _request(us_instrument, now)
    calendar = StaticTradingCalendar((request.feature_snapshot.latest_bar_date,))
    result = ForecastEngine(calendar, ForecastRegistry()).forecast(request)[0]
    assert result.availability is ForecastAvailability.CALENDAR_UNAVAILABLE
    assert result.target_session_date is None


def test_fc12_baseline_uses_only_labels_matured_by_forecast_origin(us_instrument, now) -> None:
    request, _ = _request(us_instrument, now)
    historical = list(synthetic_samples(us_instrument, count=20, start=date(2025, 1, 1)))
    future_target = request.feature_snapshot.latest_bar_date + timedelta(days=10)
    historical.append(replace(historical[-1], target_session_date=future_target, matured_at=future_target))
    calendar = StaticTradingCalendar((request.feature_snapshot.latest_bar_date, date(2026, 7, 13)))
    result = ForecastEngine(calendar, ForecastRegistry()).forecast(request, samples=tuple(historical))[0]
    assert result.availability is ForecastAvailability.AVAILABLE
    assert result.sample_count == 20


def test_fc00_blocked_data_cannot_produce_probabilities(us_instrument, now) -> None:
    quality = DataQualityReport(
        QualityStatus.BLOCKED, QualityAction.BLOCK_NEW_ENTRIES, 0.0, 0.0, True, (),
        DataCapabilities(daily_price=False), now,
    )
    request, _ = _request(us_instrument, now, data_quality=quality)
    calendar = StaticTradingCalendar((request.feature_snapshot.latest_bar_date, date(2026, 7, 13)))
    result = ForecastEngine(calendar, ForecastRegistry()).forecast(request)[0]
    assert result.availability is ForecastAvailability.DATA_BLOCKED
    assert result.probabilities is None


@pytest.mark.parametrize("instrument_fixture", ("us_instrument", "a_instrument"))
def test_fc17_saved_v2_feature_snapshot_runs_without_network(request, instrument_fixture, now, tmp_path) -> None:
    instrument = request.getfixturevalue(instrument_fixture)
    history = bars(instrument, 130, fetched_at=now)
    snapshot = FeatureBuilder(feature_calendar(history)).build(inputs(instrument, now, history), generated_at=now)
    repository = SQLiteRepository(tmp_path / f"{instrument.market.value}.db")
    assert repository.upsert_feature_snapshot(snapshot).inserted == 1
    restored = repository.get_feature_snapshot(instrument, snapshot.mode, snapshot.cutoff_at)
    assert restored == snapshot
    reference = history[-1]
    future = tuple(reference.trading_date + timedelta(days=offset) for offset in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10))
    forecast_calendar = StaticTradingCalendar((reference.trading_date,) + future)
    result = ForecastEngine(forecast_calendar, ForecastRegistry()).forecast(ForecastRequest(restored, reference, now, (1,)))[0]
    assert result.availability is ForecastAvailability.INSUFFICIENT_SAMPLE
    assert result.instrument.market is instrument.market
    repository.close()
