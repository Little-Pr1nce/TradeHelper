from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import exchange_calendars as xc
import pytest

from tradehelper_v2.application.analysis import RuntimeAnalysisPipeline
from tradehelper_v2.config.settings import V2Settings
from tradehelper_v2.contracts import (
    AccountSnapshot, AdjustmentMode, AnalysisTaskProgress, CanonicalBar, DecisionMode, Exchange,
    FreshnessStatus, InstrumentId, Market, PortfolioAnalysisCommand,
    PositionSnapshot, ProviderStatus, QuoteSnapshot, ReportKind,
    SingleStockAnalysisCommand, StockMetadata, TradingSession,
    WatchlistSnapshot, stable_hash,
)
from tradehelper_v2.contracts.providers import ProviderResult
from tradehelper_v2.runtime.container import build_runtime_container


def _as_of(market, mode):
    hours = {
        (Market.US, DecisionMode.PRE): 12,
        (Market.US, DecisionMode.INTRADAY): 15,
        (Market.US, DecisionMode.EOD): 22,
        (Market.A, DecisionMode.PRE): 1,
        (Market.A, DecisionMode.INTRADAY): 2,
        (Market.A, DecisionMode.EOD): 8,
    }
    return datetime(2026, 7, 16, hours[(market, mode)], tzinfo=timezone.utc)


def _instrument(market, second=False):
    if market is Market.US:
        return InstrumentId.from_code("AMD" if second else "AAPL", market, Exchange.XNAS)
    return InstrumentId.from_code("000001" if second else "600519", market)


def _bars(instrument, fetched_at):
    calendar = xc.get_calendar("XSHG" if instrument.market is Market.A else "XNYS")
    sessions = calendar.sessions_in_range("2025-12-01", "2026-07-15")
    result = []
    for index, session in enumerate(sessions):
        close = 80 + index * .1
        result.append(CanonicalBar(
            instrument, session.date(), close - .2, close + .5, close - .5,
            close, 1_000_000, AdjustmentMode.FRONT_ADJUSTED, "fixture", fetched_at,
        ))
    return tuple(result)


class _FrozenData:
    def __init__(self, instruments, now):
        self.now = now
        self.values = {item: _bars(item, now) for item in instruments}

    def refresh_metadata(self, instrument, *_):
        value = StockMetadata(instrument, f"{instrument.code} Company", "Technology", None, date(1980, 1, 1), "fixture", self.now)
        return ProviderResult.success(value, "fixture", self.now)

    def refresh_listing_date(self, *_):
        return ProviderResult.success(date(1980, 1, 1), "fixture", self.now)

    def refresh_daily_bars(self, instrument, *_, **__):
        source=next(value for key,value in self.values.items() if key.code==instrument.code)
        if source[0].instrument!=instrument:
            source=tuple(CanonicalBar(instrument,item.trading_date,item.open,item.high,item.low,item.close,item.volume,item.adjustment_mode,item.source,item.fetched_at,item.corporate_action_version) for item in source)
        return ProviderResult.success(source, "fixture", self.now)

    def refresh_quote(self, instrument, mode, as_of):
        if mode is DecisionMode.EOD or (mode is DecisionMode.PRE and instrument.market is Market.A):
            return ProviderResult.failure(ProviderStatus.UNAVAILABLE, as_of)
        session = TradingSession.PRE if mode is DecisionMode.PRE else TradingSession.REGULAR
        quote = QuoteSnapshot(instrument, session, 100, 99, 99.5, 100.5, 99, 10000, 99.9, 100.1, as_of, as_of, "fixture", FreshnessStatus.FRESH)
        return ProviderResult.success(quote, "fixture", as_of)

    def refresh_news(self, *args):
        return ProviderResult.failure(ProviderStatus.EMPTY, args[-1])

    def refresh_fundamentals(self, *args):
        return ProviderResult.failure(ProviderStatus.EMPTY, args[-1])


def _container(tmp_path, market, mode):
    now = _as_of(market, mode)
    instruments = (_instrument(market), _instrument(market, True))
    container = build_runtime_container(V2Settings.from_mapping({"work_dir": str(tmp_path)}))
    container.data_refresh = _FrozenData(instruments, now)
    account = AccountSnapshot(
        market, "CNY" if market is Market.A else "USD", Decimal("50000"),
        (PositionSnapshot(instruments[0], Decimal("100") if market is Market.A else Decimal("10"), Decimal("75"), now),), now,
    )
    container.repository.save_account_snapshot(account)
    watch = WatchlistSnapshot(stable_hash({"market": market, "instruments": (instruments[1],), "created": now}), market, (instruments[1],), now)
    container.repository.save_watchlist_snapshot(watch)
    return container, account, watch, now, instruments


@pytest.mark.parametrize("market", tuple(Market))
@pytest.mark.parametrize("mode", tuple(DecisionMode))
def test_single_stock_real_chain_all_markets_and_modes(tmp_path, market, mode):
    container, account, _, now, instruments = _container(tmp_path, market, mode)
    identity = {"instrument": instruments[0], "mode": mode, "history": "6m", "requested_at": now, "account": stable_hash(account), "force_refresh": False}
    command = SingleStockAnalysisCommand(stable_hash(identity), instruments[0], mode, "6m", now, stable_hash(account))
    try:
        document = RuntimeAnalysisPipeline(container).single_stock(command)
        assert document.report_kind is ReportKind.SINGLE_STOCK
        assert {section.section_id for section in document.sections} >= {"forecast", "plans", "risk", "history"}
    finally:
        container.close()


@pytest.mark.parametrize("market", tuple(Market))
@pytest.mark.parametrize("mode", tuple(DecisionMode))
def test_portfolio_real_chain_all_markets_and_modes(tmp_path, market, mode):
    container, account, watch, now, _ = _container(tmp_path, market, mode)
    identity = {"market": market, "mode": mode, "history": "6m", "requested_at": now, "account": stable_hash(account), "watchlist": watch.watchlist_id, "force_refresh": False}
    command = PortfolioAnalysisCommand(stable_hash(identity), market, mode, "6m", now, stable_hash(account), watch.watchlist_id)
    try:
        document = RuntimeAnalysisPipeline(container).portfolio(command)
        assert document.report_kind is ReportKind.PORTFOLIO
        assert "冻结账户权益" in document.summary
        assert {section.section_id for section in document.sections} >= {"priority_actions", "holdings", "forecast"}
    finally:
        container.close()


def test_production_application_runs_off_ui_thread_with_typed_progress(tmp_path):
    container, _, _, _, _ = _container(tmp_path, Market.US, DecisionMode.EOD)
    values=[]; completed=[]
    try:
        task_id=container.analysis.start_single(
            {"market":"US","symbol":"AAPL","mode":"eod","history_period":"6m"},
            on_progress=values.append,on_complete=completed.append,
        )
        document=container.analysis._futures[task_id].result(timeout=10)
        assert document is completed[0]
        assert values and all(isinstance(item,AnalysisTaskProgress) for item in values)
        assert values[-1].completed_units==values[-1].total_units
        assert container.repository.get_report_document(document.report_id) is not None
    finally:
        container.close()


def test_background_learning_records_four_horizons_idempotently(tmp_path):
    container, account, _, now, instruments = _container(tmp_path, Market.US, DecisionMode.EOD)
    identity={"instrument":instruments[0],"mode":DecisionMode.EOD,"history":"6m","requested_at":now,"account":stable_hash(account),"force_refresh":False}
    command=SingleStockAnalysisCommand(stable_hash(identity),instruments[0],DecisionMode.EOD,"6m",now,stable_hash(account))
    try:
        RuntimeAnalysisPipeline(container).single_stock(command)
        for future in tuple(container.background_learning._futures.values()): future.result(timeout=10)
        outcomes=container.repository.list_forecast_outcomes(instruments[0])
        assert {item.horizon for item in outcomes}=={1,3,5,10}
        task=container.background_learning.submit(instruments[0],container.data_refresh.values[instruments[0]],listing_date=date(1980,1,1))
        container.background_learning.result(task,timeout=10)
        assert len(container.repository.list_forecast_outcomes(instruments[0]))==4
    finally:
        container.close()
