from datetime import date, datetime, timezone
import json
from pathlib import Path

from tradehelper_v2.contracts import InstrumentId, Market
from tradehelper_v2.data.providers import (
    parse_baostock_listing_date, parse_finnhub_fundamentals, parse_finnhub_metadata,
    parse_finnhub_news, parse_nasdaq_bars, parse_nasdaq_quote, parse_tickflow_bars, parse_tickflow_metadata, parse_tickflow_quote,
    parse_yfinance_bars, parse_yfinance_quote,
)
from tradehelper_v2.data.providers.adapters import TickFlowAdapter
from tradehelper_v2.data.providers.base import RetryingClient
from tradehelper_v2.contracts.enums import ProviderStatus


def test_g26_fixture_shapes_parse_offline_for_both_markets(a_instrument, us_instrument, now) -> None:
    a_bars = parse_tickflow_bars([{"trade_date": "2026-07-09", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 2}], a_instrument, now)
    us_bars = parse_yfinance_bars([{"Date": "2026-07-09", "Open": 100, "High": 110, "Low": 95, "Close": 108, "Volume": 12345}], us_instrument, now)
    nasdaq_bars = parse_nasdaq_bars({"data": {"tradesTable": {"rows": [{"date": "07/09/2026", "open": "$100", "high": "$110", "low": "$95", "close": "$108", "volume": "12,345"}]}}}, us_instrument, now)
    quote = parse_tickflow_quote({"last_price": 108, "timestamp": 1783699200}, us_instrument, "regular", now)
    y_quote = parse_yfinance_quote({"preMarketPrice": 109, "preMarketTime": 1783699200}, us_instrument, "pre", now)
    metadata = parse_tickflow_metadata({"name": "贵州茅台"}, a_instrument, now)
    finnhub_metadata = parse_finnhub_metadata({"name": "Apple Inc", "ipo": "1980-12-12"}, us_instrument, now)
    fundamentals = parse_finnhub_fundamentals({"metric": {"peNormalizedAnnual": 31.2}}, us_instrument, now)
    news = parse_finnhub_news([{"headline": "Apple update", "source": "Reuters", "datetime": 1783699200}], us_instrument, now)
    assert a_bars[0].volume == 200 and us_bars[0].source == "yfinance" and nasdaq_bars[0].close == 108
    assert quote.price == 108 and y_quote.price == 109
    assert metadata.name == "贵州茅台" and finnhub_metadata.listing_date == date(1980, 12, 12)
    assert fundamentals.fields["peNormalizedAnnual"].source == "finnhub" and news[0].title == "Apple update"
    assert parse_baostock_listing_date({"ipoDate": "1999-11-10"}, a_instrument) == date(1999, 11, 10)


def test_nasdaq_real_nested_quote_shape_and_et_timestamp(us_instrument, now) -> None:
    payload = json.loads((Path(__file__).parent / "fixtures" / "providers" / "nasdaq_price_only.json").read_text(encoding="utf-8"))
    from tradehelper_v2.data.providers import parse_nasdaq_quote

    quote = parse_nasdaq_quote(payload, us_instrument, "pre", now)
    assert quote.price == 217.5 and quote.prev_close == 210.0
    assert quote.bid == 217.4 and quote.ask == 217.6 and quote.volume == 1234
    assert quote.observed_at == datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    assert quote.freshness_status.value == "not_required"


def test_news_available_at_is_first_ingestion_time(us_instrument, now) -> None:
    published = now.replace(hour=10)
    fetched = now.replace(hour=12)
    news = parse_finnhub_news([{"headline": "Known later", "source": "Reuters", "datetime": int(published.timestamp())}], us_instrument, fetched)
    assert news[0].published_at == published and news[0].available_at == fetched


def test_structured_fundamental_fields_preserve_period_and_source(a_instrument, now) -> None:
    snapshot = parse_finnhub_fundamentals(
        {"fields": {"roe": {"value": 0.2, "unit": "ratio", "period_end": "2025-12-31", "source": "baostock"}}},
        a_instrument, now, provider="baostock",
    )
    assert snapshot.fields["roe"].period_end == date(2025, 12, 31)
    assert snapshot.fields["roe"].source == "baostock" and snapshot.fields["roe"].unit == "ratio"


def test_g26_deidentified_payload_fixtures_have_no_credentials() -> None:
    root = Path(__file__).parent / "fixtures" / "providers"
    payloads = [path.read_text(encoding="utf-8") for path in root.glob("*.json")]
    assert len(payloads) >= 12
    assert all(token not in "\n".join(payloads).lower() for token in ("api_key", "token", "cookie", "/users/"))
    assert all(json.loads(payload) is not None for payload in payloads)


def test_every_provider_fixture_is_parsed_by_its_production_parser(a_instrument, us_instrument, now) -> None:
    root = Path(__file__).parent / "fixtures" / "providers"
    load = lambda name: json.loads((root / name).read_text(encoding="utf-8"))
    assert parse_tickflow_bars(load("tickflow_a_daily.json"), a_instrument, now)
    assert parse_tickflow_bars(load("tickflow_us_daily.json"), us_instrument, now)
    assert parse_tickflow_quote(load("tickflow_quote_batch.json")[0], us_instrument, "regular", now).price == 217
    assert parse_nasdaq_bars(load("nasdaq_historical.json"), us_instrument, now)
    assert parse_nasdaq_quote(load("nasdaq_price_only.json"), us_instrument, "pre", now).price == 217.5
    assert parse_yfinance_bars(load("yfinance_daily.json"), us_instrument, now)
    assert parse_yfinance_quote(load("yfinance_extended_quote.json"), us_instrument, "pre", now).price == 217.5
    assert parse_finnhub_metadata(load("finnhub_profile2.json"), us_instrument, now).name == "Apple Inc"
    assert parse_finnhub_fundamentals(load("finnhub_fundamentals.json"), us_instrument, now).fields
    assert parse_finnhub_fundamentals(load("baostock_fundamentals.json"), a_instrument, now, provider="baostock").fields["roe"].period_end == date(2025, 12, 31)
    assert parse_baostock_listing_date(load("baostock_listing.json"), a_instrument) == date(1999, 11, 10)
    assert parse_finnhub_news(load("finnhub_news.json"), us_instrument, now)
    assert parse_finnhub_news(load("akshare_news.json"), a_instrument, now)


def test_tickflow_prefers_full_trade_date_over_display_day_column(a_instrument, now) -> None:
    bars = parse_tickflow_bars(
        [{"date": "29", "trade_date": "2026-07-09", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 12}],
        a_instrument,
        now,
    )
    assert bars[0].trading_date.isoformat() == "2026-07-09"


def test_tickflow_adapter_enforces_batch_and_round_limits(us_instrument, now) -> None:
    calls: list[list[str]] = []
    def quotes(symbols):
        calls.append(symbols)
        return [{"last_price": 100 + index, "timestamp": now.isoformat()} for index, _ in enumerate(symbols)]
    adapter = TickFlowAdapter(lambda *_: [], quotes, lambda *_: {"name": "fixture"}, lambda: now)
    instruments = tuple(InstrumentId.from_code(f"T{index}", Market.US, "XNAS") for index in range(51))
    batch = adapter.quotes(instruments, "regular")
    assert len(calls) == 10 and all(len(item) <= 5 for item in calls)
    assert batch.failures[instruments[-1]].value == "rate_limited"


def test_provider_retry_uses_injected_backoff_without_sleep(now) -> None:
    calls, delays = 0, []
    def invoke():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("timeout")
        return "ok"
    result = RetryingClient("fixture", invoke, lambda: now, lambda _: ProviderStatus.TIMEOUT, delays.append).fetch()
    assert result.status is ProviderStatus.OK and calls == 3 and delays == [1.0, 2.0]
