from datetime import date, timedelta
from time import perf_counter

from conftest import make_bar


def test_g04_validate_ten_thousand_bars_within_local_baseline(us_instrument) -> None:
    started = perf_counter()
    first = date(2000, 1, 1)
    bars = tuple(make_bar(us_instrument, first + timedelta(days=index), 100 + index * 0.01) for index in range(10_000))
    elapsed = perf_counter() - started
    assert len(bars) == 10_000
    assert elapsed < 2.0, f"validated 10,000 bars in {elapsed:.3f}s"
