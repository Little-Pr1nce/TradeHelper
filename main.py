"""TradeHelper 2.0 production entry point.

主程序只依赖根目录下按职责拆分的正式模块。V1 源码由 Git 标签保留，
旧用户数据仅通过冻结的只读迁移 reader 导入；各平台使用同一入口。
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone
import flet as ft

from runtime import start_runtime
from runtime.logging_config import configure_runtime_logging, shutdown_runtime_logging
from config.settings import V2Settings
from ui.app import build_app
from ui.pages.single_stock import SingleStockPage
from ui.pages.portfolio import PortfolioPage
from ui.pages.evaluation import AbilityEvaluationPage
from ui.pages.report_history import ReportHistoryPage
from ui.pages.settings import settings_page
from ui.pages.migration import MigrationPage
from application.exports import export_report_and_reveal
from application.evaluation import RepositoryHistoricalEvaluationService
from contracts import ExportFormat, Market
from contracts import InstrumentId
from data.composition import build_data_refresh_service
from ui.theme import BACKGROUND, PRIMARY


logger = logging.getLogger(__name__)

def _main(page: ft.Page) -> None:
    lifecycle = None
    try:
        logger.info("Flet UI session initializing")
        lifecycle = start_runtime()
        container = lifecycle.container
        page.title = "TradeHelper - 股票分析助手"
        page.theme_mode = ft.ThemeMode.LIGHT
        page.bgcolor = BACKGROUND
        page.padding = 0
        page.window.width = 1320
        page.window.height = 860
        page.window.min_width = 920
        page.window.min_height = 640
        page.theme = ft.Theme(color_scheme_seed=PRIMARY)
        def close_session(_event=None):
            logger.info("Flet UI session closing permanently")
            lifecycle.close()
        # A transient disconnect can reconnect to the same Page. Closing the
        # repository there leaves every button bound to a dead runtime.
        page.on_close = close_session
        single = SingleStockPage(lookup_port=container.lookup, analysis_port=container.analysis,
                             export_port=lambda document, *, format: export_report_and_reveal(container.repository, document, directory=container.settings.work_dir/"reports", format=format))
        portfolio = PortfolioPage(editor=container.portfolio_editor, lookup_port=container.lookup,
                              analysis_port=container.analysis, export_port=lambda document, *, format: export_report_and_reveal(container.repository, document, directory=container.settings.work_dir/"reports", format=format),
                              account_loader=container.repository.get_latest_account_snapshot,
                              watchlist_loader=container.repository.latest_watchlist_snapshot)
        evaluation = AbilityEvaluationPage(
            evaluation_port=RepositoryHistoricalEvaluationService(container.repository),
            lookup_port=container.lookup,
        )
        initial_account=container.repository.get_latest_account_snapshot(Market.US) or container.repository.get_latest_account_snapshot(Market.A)
        if initial_account is not None:
            portfolio.set_account(initial_account)
            portfolio.set_watchlist(container.repository.latest_watchlist_snapshot(initial_account.market))
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
        page.add(build_app(single, history, portfolio, settings, migration, evaluation=evaluation))
        if os.environ.get("TRADEHELPER_SMOKE_TEST") == "1":
            page.add(ft.Text("V2 smoke mode: runtime initialized", size=12))
        logger.info("Flet UI session ready migration_status=%s", container.migration_status)
    except Exception:
        logger.exception("Flet UI session initialization failed")
        if lifecycle is not None:
            lifecycle.close()
        raise

if __name__ == "__main__":
    runtime_settings = V2Settings.load()
    log_path = configure_runtime_logging(runtime_settings.work_dir)
    logger.info("TradeHelper 2.0 starting work_dir=%s log_file=%s", runtime_settings.work_dir, log_path)
    try:
        if os.environ.get("TRADEHELPER_SMOKE_TEST") == "1":
            from release.smoke import run_smoke
            raise SystemExit(0 if run_smoke().get("ok") else 1)
        ft.run(_main)
    except SystemExit:
        raise
    except Exception:
        logger.exception("TradeHelper process terminated by an unhandled error")
        raise
    finally:
        logger.info("TradeHelper 2.0 stopped")
        shutdown_runtime_logging()
