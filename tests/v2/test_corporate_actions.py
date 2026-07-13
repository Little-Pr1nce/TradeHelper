from datetime import date

from tradehelper_v2.contracts import CanonicalBar
from tradehelper_v2.data.repository import SQLiteRepository


def test_daily_bar_records_adjustment_version_and_never_silently_replaces_it(tmp_path, us_instrument, now) -> None:
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    original = CanonicalBar(us_instrument, date(2026, 7, 9), 100, 102, 99, 101, 1000, "front_adjusted", "tickflow", now, "ca-v1")
    revision = CanonicalBar(us_instrument, date(2026, 7, 9), 50, 51, 49, 50.5, 2000, "front_adjusted", "tickflow", now, "ca-v2")
    assert repo.upsert_daily_bars((original,)).inserted == 1
    assert repo.upsert_daily_bars((revision,)).conflicts == 1
    assert repo.list_daily_bars(us_instrument, date(2026, 7, 9), date(2026, 7, 9))[0].corporate_action_version == "ca-v1"
    repo.close()
