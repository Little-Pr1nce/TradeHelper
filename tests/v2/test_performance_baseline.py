from datetime import date, timedelta
from time import perf_counter

from conftest import make_bar
from tradehelper_v2.contracts import FundamentalSnapshot, FundamentalValue, NewsSnapshot, ProviderStatus
from tradehelper_v2.features import FeatureBuilder

from feature_helpers import bars as feature_bars, calendar as feature_calendar, inputs as feature_inputs


def test_g04_validate_ten_thousand_bars_within_local_baseline(us_instrument) -> None:
    started = perf_counter()
    first = date(2000, 1, 1)
    bars = tuple(make_bar(us_instrument, first + timedelta(days=index), 100 + index * 0.01) for index in range(10_000))
    elapsed = perf_counter() - started
    assert len(bars) == 10_000
    assert elapsed < 2.0, f"validated 10,000 bars in {elapsed:.3f}s"


def test_f13_feature_snapshot_performance(us_instrument, now) -> None:
    values = feature_bars(us_instrument, 500, fetched_at=now)
    builder = FeatureBuilder(feature_calendar(values))
    news = tuple(NewsSnapshot(us_instrument, f"news-{index}", "fixture", now - timedelta(hours=index), now - timedelta(hours=index), now,
                              None, False, "positive", 0.8, 1.0) for index in range(100))
    fundamentals = FundamentalSnapshot(us_instrument, {"pe_ttm": FundamentalValue(20.0, "multiple", None, None, "finnhub")}, now, now, "finnhub", "ok")
    feature_input = feature_inputs(us_instrument, now, values, news=news, news_status=ProviderStatus.OK,
                                   fundamentals=fundamentals, fundamentals_status=ProviderStatus.OK)
    started = perf_counter()
    for _ in range(1000):
        builder.build(feature_input)
    median = (perf_counter() - started) / 1000
    assert median < 0.010, f"median FeatureSnapshot build took {median:.6f}s"
