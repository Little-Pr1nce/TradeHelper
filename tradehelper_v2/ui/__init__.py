"""V2-11 Flet UI：只渲染 ViewModel/ReportDocument。"""
"""Flet 展示层入口。"""
from .app import build_app
from .pages.portfolio import PortfolioPage
from .pages.report_history import ReportHistoryPage
from .pages.single_stock import SingleStockPage

__all__=("build_app","PortfolioPage","ReportHistoryPage","SingleStockPage")
