"""Opt-in, credential-safe smoke coverage for the real V2-1 composition root."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradehelper_v2.config.settings import V2Settings
from tradehelper_v2.contracts import InstrumentId, Market, ProviderStatus
from tradehelper_v2.contracts.enums import DecisionMode
from tradehelper_v2.data.composition import build_data_refresh_service
from tradehelper_v2.data.repository import SQLiteRepository


if os.environ.get("TRADEHELPER_LIVE_TESTS") != "1":
    pytestmark = pytest.mark.skip(reason="set TRADEHELPER_LIVE_TESTS=1 to run real provider smoke tests")


def _settings() -> V2Settings:
    configured = os.environ.get("TRADEHELPER_LIVE_SETTINGS_PATH")
    if configured:
        path = Path(configured).expanduser()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("live settings must contain a JSON object")
        return V2Settings.from_mapping(payload)
    return V2Settings.load()


def test_g27_real_v2_provider_composition(tmp_path) -> None:
    settings = _settings()
    missing = [name for name in ("stock_token_us", "stock_token_a", "news_token_us") if not getattr(settings, name)]
    if missing:
        pytest.skip(f"V2 live settings missing required provider credentials: {', '.join(missing)}")
    repo = SQLiteRepository(tmp_path / "live_v2.db")
    service = build_data_refresh_service(settings, repo)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=45)).date()
    end = now.date()
    us = InstrumentId.from_code("AAPL", Market.US, "XNAS")
    a_share = InstrumentId.from_code("600519", Market.A)
    try:
        us_metadata = service.refresh_metadata(us, now)
        us_listing = service.refresh_listing_date(us, now)
        us_bars = service.refresh_daily_bars(us, start, end, us_listing.value, now)
        us_fundamentals = service.refresh_fundamentals(us, now)
        us_news = service.refresh_news(us, DecisionMode.EOD, now)
        a_metadata = service.refresh_metadata(a_share, now)
        a_listing = service.refresh_listing_date(a_share, now)
        a_bars = service.refresh_daily_bars(a_share, start, end, a_listing.value, now)
        a_fundamentals = service.refresh_fundamentals(a_share, now)
        a_news = service.refresh_news(a_share, DecisionMode.EOD, now)

        assert us_metadata.status is ProviderStatus.OK and us_metadata.value is not None
        assert us_metadata.value.name and us_metadata.value.name != us.code
        assert us_listing.status is ProviderStatus.OK and us_listing.value is not None
        assert us_bars.status is ProviderStatus.OK and us_bars.selected_source == "nasdaq" and us_bars.value
        assert all(bar.source == "nasdaq" and bar.fetched_at.tzinfo is not None for bar in us_bars.value)
        assert us_listing.value <= us_bars.value[0].trading_date
        assert us_fundamentals.status is ProviderStatus.OK and us_fundamentals.value is not None and us_fundamentals.value.fields
        assert us_news.status is ProviderStatus.OK and us_news.value

        assert a_metadata.status is ProviderStatus.OK and a_metadata.value is not None
        assert a_metadata.value.name and a_metadata.value.name != a_share.code
        assert a_listing.status is ProviderStatus.OK and a_listing.value is not None
        assert a_bars.status is ProviderStatus.OK and a_bars.selected_source == "tickflow" and a_bars.value
        assert all(bar.source == "tickflow" and bar.fetched_at.tzinfo is not None for bar in a_bars.value)
        assert a_listing.value <= a_bars.value[0].trading_date
        assert a_fundamentals.status is ProviderStatus.OK and a_fundamentals.value is not None and a_fundamentals.value.fields
        assert {"pe_ttm", "pb_mrq", "roe", "gross_margin", "debt_ratio", "net_profit_yoy", "revenue_yoy"}.issubset(a_fundamentals.value.fields)
        assert all(a_fundamentals.value.fields[name].period_end is not None for name in ("roe", "gross_margin", "debt_ratio", "net_profit_yoy", "revenue_yoy"))
        assert a_news.status is ProviderStatus.OK and a_news.value
    finally:
        repo.close()


def test_real_yfinance_fundamental_fallback(tmp_path) -> None:
    settings = _settings()
    repo = SQLiteRepository(tmp_path / "live_yfinance_v2.db")
    service = build_data_refresh_service(replace(settings, news_token_us=""), repo)
    try:
        result = service.refresh_fundamentals(
            InstrumentId.from_code("AAPL", Market.US, "XNAS"), datetime.now(timezone.utc)
        )
        assert result.status is ProviderStatus.OK and result.selected_source == "yfinance" and result.value is not None
    finally:
        repo.close()
