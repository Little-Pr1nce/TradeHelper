from datetime import date

from tradehelper_v2.contracts import ForecastDirection
from tradehelper_v2.forecast.labels import direction_label, flat_band, target_session_date


def test_fc01_target_sessions_use_calendar(calendar, us_instrument, a_instrument) -> None:
    origin = date(2026, 7, 2)
    assert target_session_date(calendar, us_instrument, origin, 1) == date(2026, 7, 6)
    assert target_session_date(calendar, a_instrument, origin, 3) == date(2026, 7, 8)


def test_fc03_volatility_scaled_band() -> None:
    low = flat_band(.1, 5); high = flat_band(.5, 5)
    assert .005 <= low < high <= .04
    assert direction_label(high + 1e-6, high) is ForecastDirection.BULLISH
    assert direction_label(-high - 1e-6, high) is ForecastDirection.BEARISH
    assert direction_label(0.0, high) is ForecastDirection.NEUTRAL
