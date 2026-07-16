"""LE10-LE19：目标交易日到期与不可验证语义。"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from tradehelper_v2.contracts import AdjustmentMode, CanonicalBar, ForecastDirection
from tradehelper_v2.learning import MaturityResolver
from test_learning_smoke import _forecast

def test_le11_target_session_not_finished_stays_pending(us_instrument,now):
    forecast=_forecast(us_instrument,now)
    pending=MaturityResolver().resolve(forecast,(),evaluated_at=now-timedelta(days=1))
    assert pending.status.value=='pending'

def test_le12_missing_target_bar_is_not_replaced_by_later_bar(us_instrument,now):
    forecast=_forecast(us_instrument,now)
    assert MaturityResolver().resolve(forecast,(),evaluated_at=now).status.value=='unverifiable'


def test_frozen_volatility_scaled_band_controls_realized_direction(us_instrument, now):
    forecast=_forecast(us_instrument,now)
    bar=CanonicalBar(
        us_instrument,
        forecast.target_session_date,
        100.,
        101.,
        99.,
        100.3,
        100,
        AdjustmentMode.FRONT_ADJUSTED,
        "fixture",
        now,
    )
    evidence=MaturityResolver().resolve(forecast,(bar,),evaluated_at=now)
    assert evidence.flat_band == Decimal(".005")
    assert evidence.actual_direction is ForecastDirection.NEUTRAL


def test_legacy_forecast_without_frozen_label_policy_is_unverifiable(us_instrument, now):
    current=_forecast(us_instrument,now)
    forecast=SimpleNamespace(
        instrument=current.instrument,
        origin_session_date=current.origin_session_date,
        target_session_date=current.target_session_date,
        reference_price=current.reference_price,
        label_flat_band=None,
    )
    evidence=MaturityResolver().resolve(forecast,(),evaluated_at=now)
    assert evidence.status.value == "unverifiable"
    assert evidence.reason_codes == ("LEARNING_LABEL_POLICY_UNAVAILABLE",)
