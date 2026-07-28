from datetime import date

import pytest

from contracts import ContractViolation, InstrumentId, Market
from data.providers import parse_nasdaq_quote, parse_tickflow_bars


@pytest.mark.parametrize(("code", "market", "exchange", "expected"), [
    ("600519", Market.A, None, "A:XSHG:600519"), ("000001", Market.A, None, "A:XSHE:000001"),
    ("430047", Market.A, None, "A:XBSE:430047"), ("aapl", Market.US, "XNAS", "US:XNAS:AAPL"),
    ("brk.b", Market.US, None, "US:UNKNOWN:BRK.B"),
])
def test_g10_code_normalization(code, market, exchange, expected) -> None:
    assert InstrumentId.from_code(code, market, exchange).stable_key == expected


def test_g10_rejects_cross_market_identifiers() -> None:
    with pytest.raises(ContractViolation):
        InstrumentId.from_code("AAPL", Market.A)
    with pytest.raises(ContractViolation):
        InstrumentId.from_code("600519", Market.US)


def test_g11_normal_bar_and_a_lot_conversion(a_instrument, now) -> None:
    bars = parse_tickflow_bars([{"date": "2026-07-09", "open": 100, "high": 110, "low": 95, "close": 108, "volume": 123}], a_instrument, now)
    assert bars[0].volume == 12_300


def test_g12_ohlc_hard_failure(us_instrument, bar_factory) -> None:
    with pytest.raises(ContractViolation, match="INVALID_OHLC"):
        bar_factory(us_instrument, date(2026, 7, 9), 108).__class__(
            instrument=us_instrument, trading_date=date(2026, 7, 9), open=100, high=105, low=95, close=108,
            volume=1, adjustment_mode="front_adjusted", source="fixture", fetched_at=__import__("conftest").NOW,
        )


def test_g14_nasdaq_price_only_preserves_missing_fields(us_instrument, now) -> None:
    quote = parse_nasdaq_quote({"data": {"lastSalePrice": "$217.50", "previousClose": "210"}}, us_instrument, "pre", now)
    assert quote.price == 217.5
    assert quote.open is quote.high is quote.low is quote.volume is quote.bid is quote.ask is None
    assert quote.available_fields == frozenset({"price", "prev_close"})
