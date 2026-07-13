from datetime import timedelta

from tradehelper_v2.contracts import ProviderResult, ProviderStatus
from tradehelper_v2.contracts.enums import DecisionMode
from tradehelper_v2.data import DataProviders, DataRefreshService
from tradehelper_v2.data.cache import DataCache


def test_g31_empty_news_is_negative_cached_then_refreshed(us_instrument, now, calendar) -> None:
    calls = 0
    def news(_):
        nonlocal calls
        calls += 1
        return ProviderResult.failure(ProviderStatus.EMPTY, now)
    service = DataRefreshService(DataProviders(finnhub_news=news), calendar, DataCache())
    assert service.refresh_news(us_instrument, DecisionMode.INTRADAY, now).status is ProviderStatus.EMPTY
    assert service.refresh_news(us_instrument, DecisionMode.INTRADAY, now + timedelta(minutes=4)).status is ProviderStatus.EMPTY
    assert calls == 1
    service.refresh_news(us_instrument, DecisionMode.INTRADAY, now + timedelta(minutes=5))
    assert calls == 2


def test_g32_tab1_tab3_share_cache_but_each_refreshes(us_instrument, now, calendar) -> None:
    calls = 0
    def news(_):
        nonlocal calls
        calls += 1
        return ProviderResult.success((), "finnhub", now)
    service = DataRefreshService(DataProviders(finnhub_news=news), calendar, DataCache())
    first = service.refresh_news(us_instrument, DecisionMode.PRE, now)
    second = service.refresh_news(us_instrument, DecisionMode.PRE, now + timedelta(minutes=1))
    assert first.status is second.status is ProviderStatus.OK
    assert calls == 1
