"""TradeHelper 2.0 production entry point.

主程序只依赖 ``tradehelper_v2``。V1 源码由 Git 标签保留，旧用户数据仅通过
冻结的只读迁移 reader 导入；PyInstaller、macOS 和 Windows 使用同一入口。
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
import flet as ft

from tradehelper_v2.runtime import start_runtime
from tradehelper_v2.ui.app import build_app
from tradehelper_v2.ui.pages.single_stock import SingleStockPage
from tradehelper_v2.ui.pages.portfolio import PortfolioPage
from tradehelper_v2.ui.pages.report_history import ReportHistoryPage
from tradehelper_v2.ui.pages.settings import settings_page
from tradehelper_v2.ui.pages.migration import MigrationPage
from tradehelper_v2.application.exports import export_report
from tradehelper_v2.application.evaluation import RepositoryHistoricalEvaluationService
from tradehelper_v2.contracts import ExportFormat, Market
from tradehelper_v2.contracts import InstrumentId
from tradehelper_v2.data.composition import build_data_refresh_service

def _main(page: ft.Page) -> None:
    lifecycle = start_runtime()
    container = lifecycle.container
    page.title = "TradeHelper 2.0"
    page.padding = 16
    page.on_disconnect = lambda _event: lifecycle.close()
    single = SingleStockPage(lookup_port=container.lookup, analysis_port=container.analysis,
                             export_port=lambda document, *, format: export_report(container.repository, document, directory=container.settings.work_dir/"reports", format=format))
    portfolio = PortfolioPage(editor=container.portfolio_editor, lookup_port=container.lookup,
                              analysis_port=container.analysis, export_port=lambda document, *, format: export_report(container.repository, document, directory=container.settings.work_dir/"reports", format=format),
                              evaluation_port=RepositoryHistoricalEvaluationService(container.repository),
                              account_loader=container.repository.get_latest_account_snapshot,
                              watchlist_loader=container.repository.latest_watchlist_snapshot)
    initial_account=container.repository.get_latest_account_snapshot(Market.US) or container.repository.get_latest_account_snapshot(Market.A)
    if initial_account is not None:
        portfolio.account=initial_account
        portfolio.watchlist_snapshot=container.repository.latest_watchlist_snapshot(initial_account.market)
    history = ReportHistoryPage(container.history, clock=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc), lookup_port=container.lookup)
    def live_test(value, *, market, mode):
        instrument=InstrumentId.from_code("600519" if market is Market.A else "AAPL",market)
        now=datetime.now(timezone.utc)
        service=build_data_refresh_service(value,container.repository)
        metadata=service.refresh_metadata(instrument,now)
        listing=service.refresh_listing_date(instrument,now)
        end=container.calendar.latest_completed_session(market,now)
        bars=service.refresh_daily_bars(instrument,end-timedelta(days=14),end,listing.value if listing.status.value=="ok" else None,now)
        return f"{instrument.code} 元数据 {metadata.status.value}；日K {bars.status.value}"
    settings = settings_page(container.settings, save_port=lambda value: value.save(),live_test_port=live_test)
    migration = MigrationPage(container.repository, lifecycle.migration_source) if lifecycle.migration_source else None
    page.add(build_app(single, history, portfolio, settings, migration))
    if os.environ.get("TRADEHELPER_SMOKE_TEST") == "1":
        page.add(ft.Text("V2 smoke mode: runtime initialized", size=12))

if __name__ == "__main__":
    if os.environ.get("TRADEHELPER_SMOKE_TEST") == "1":
        from tradehelper_v2.release.smoke import run_smoke
        raise SystemExit(0 if run_smoke().get("ok") else 1)
    ft.app(target=_main)
