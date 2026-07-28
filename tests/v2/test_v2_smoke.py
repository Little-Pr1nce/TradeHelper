from datetime import date

from contracts import InstrumentId, Market, canonical_json, stable_hash


def test_g01_contract_determinism() -> None:
    value_a = {"at": "2026-07-10T16:00:00Z", "amount": "1.20", "code": "AAPL"}
    value_b = {"code": "AAPL", "amount": "1.20", "at": "2026-07-10T16:00:00Z"}
    assert canonical_json(value_a) == canonical_json(value_b)
    assert stable_hash(value_a) == stable_hash(value_b)


def test_g02_dual_market_fixtures_construct_without_v1_models() -> None:
    instruments = (
        InstrumentId.from_code("600519", Market.A), InstrumentId.from_code("000001", Market.A),
        InstrumentId.from_code("430047", Market.A), InstrumentId.from_code("AAPL", Market.US, "XNAS"),
        InstrumentId.from_code("BRK.B", Market.US),
    )
    assert [item.stable_key for item in instruments] == [
        "A:XSHG:600519", "A:XSHE:000001", "A:XBSE:430047", "US:XNAS:AAPL", "US:UNKNOWN:BRK.B",
    ]
