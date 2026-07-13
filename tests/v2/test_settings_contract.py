from tradehelper_v2.config.settings import V2Settings
from tradehelper_v2.data import DataProviders, DataRefreshService
from tradehelper_v2.data.cache import DataCache


def test_g63_data_layer_does_not_require_llm_settings(tmp_path, us_instrument, now, calendar) -> None:
    settings = V2Settings.from_mapping({"work_dir": str(tmp_path)})
    assert settings.database_path == tmp_path / "tradehelper_v2.db"
    service = DataRefreshService(DataProviders(), calendar, DataCache())
    assert service.refresh_fundamentals(us_instrument, now).value is None
