"""V2 desktop shell; composition root remains in ``main.py``."""
from __future__ import annotations
import flet as ft

from .theme import BACKGROUND, BORDER, NAV, PRIMARY, PRIMARY_DARK, PRIMARY_SOFT, SURFACE, TEXT_MUTED

def build_app(single_stock, history, portfolio, settings, migration=None, evaluation=None):
    items = [
        ("单股分析", ft.Icons.QUERY_STATS_OUTLINED, ft.Icons.QUERY_STATS, single_stock.build()),
        ("我的持仓", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, ft.Icons.ACCOUNT_BALANCE_WALLET, portfolio.build()),
    ]
    if evaluation is not None:
        items.append(("能力评估", ft.Icons.INSIGHTS_OUTLINED, ft.Icons.INSIGHTS, evaluation.build()))
    items.extend([
        ("报告记录", ft.Icons.HISTORY_OUTLINED, ft.Icons.HISTORY, history.build()),
        ("设置", ft.Icons.SETTINGS_OUTLINED, ft.Icons.SETTINGS, settings),
    ])
    if migration is not None:
        items.append(("数据迁移", ft.Icons.SYNC_OUTLINED, ft.Icons.SYNC, migration.build()))

    views = [
        ft.Container(content=item[3], visible=index == 0, expand=True)
        for index, item in enumerate(items)
    ]
    current = ft.Text(items[0][0], size=12, weight=ft.FontWeight.W_600, color=TEXT_MUTED)

    def switch_page(event):
        selected = event.control.selected_index
        for index, view in enumerate(views):
            view.visible = index == selected
        current.value = items[selected][0]
        if current.page:
            current.update()
            for view in views:
                view.update()
        if evaluation is not None and items[selected][0] == "能力评估":
            on_show = getattr(evaluation, "on_show", None)
            if on_show is not None:
                on_show()

    header = ft.Container(
        bgcolor=SURFACE,
        border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
        padding=ft.Padding(22, 10, 22, 10),
        content=ft.Row(
            controls=[
                ft.Row(
                    controls=[
                        ft.Column(
                            controls=[
                                ft.Text("决策工作区", size=14, weight=ft.FontWeight.BOLD),
                                ft.Text("所有结论基于冻结事实与真实账户", size=10, color=TEXT_MUTED),
                            ],
                            spacing=0,
                            tight=True,
                        ),
                    ],
                    spacing=10,
                ),
                ft.Row(
                    controls=[
                        current,
                        ft.Container(width=1, height=20, bgcolor=BORDER),
                        ft.Container(
                            bgcolor=PRIMARY_SOFT,
                            border_radius=6,
                            padding=ft.Padding(8, 4, 8, 4),
                            content=ft.Text("V2.0", size=11, color=PRIMARY, weight=ft.FontWeight.BOLD),
                        ),
                    ],
                    spacing=10,
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        ),
    )
    navigation = ft.NavigationRail(
        destinations=[
            ft.NavigationRailDestination(
                icon=ft.Icon(icon, color="#A9BBC5"),
                selected_icon=ft.Icon(selected_icon, color=PRIMARY_DARK),
                label=label,
            )
            for label, icon, selected_icon, _view in items
        ],
        selected_index=0,
        on_change=switch_page,
        bgcolor=NAV,
        indicator_color=PRIMARY_SOFT,
        label_type=ft.NavigationRailLabelType.ALL,
        min_width=84,
        min_extended_width=190,
        extended=True,
        group_alignment=-0.85,
        selected_label_text_style=ft.TextStyle(color=ft.Colors.WHITE, size=12, weight=ft.FontWeight.W_600),
        unselected_label_text_style=ft.TextStyle(color="#C7D2D8", size=12),
        leading=ft.Container(
            padding=ft.Padding(12, 14, 12, 18),
            content=ft.Row([
                ft.Container(
                    width=36,
                    height=36,
                    border_radius=7,
                    bgcolor=PRIMARY,
                    alignment=ft.Alignment.CENTER,
                    content=ft.Icon(ft.Icons.CANDLESTICK_CHART, color=ft.Colors.WHITE, size=21),
                ),
                ft.Column([
                    ft.Text("TradeHelper", color=ft.Colors.WHITE, size=15, weight=ft.FontWeight.BOLD),
                    ft.Text("可信交易助手", color="#A9BBC5", size=10),
                ], spacing=0, tight=True),
            ], spacing=9),
        ),
    )
    content = ft.Column(
        controls=[
            header,
            ft.Container(content=ft.Stack(controls=views, expand=True), bgcolor=BACKGROUND, expand=True),
        ],
        spacing=0,
        expand=True,
    )
    return ft.Row(
        controls=[
            ft.Container(content=navigation, width=204, bgcolor=NAV),
            ft.Container(content=content, expand=True),
        ],
        spacing=0,
        expand=True,
    )
