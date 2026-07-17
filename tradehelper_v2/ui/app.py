"""V2-11 页面容器；composition root 由 V2-12 提供。"""
from __future__ import annotations
import flet as ft

def build_app(single_stock, history, portfolio, settings):
    labels = ("单股分析", "历史报告", "我的持仓", "设置")
    views = (single_stock.build(), history.build(), portfolio.build(), settings)
    return ft.Tabs(
        content=ft.Column(
            [
                ft.TabBar(tabs=[ft.Tab(label=label) for label in labels]),
                ft.TabBarView(controls=list(views), expand=True),
            ],
            expand=True,
        ),
        length=len(labels),
        selected_index=0,
        expand=True,
    )
