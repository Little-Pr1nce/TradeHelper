from __future__ import annotations
import flet as ft
from .report_view import chart_control

def evaluation_charts(view):
    return ft.Column([chart_control(chart) for chart in view.charts],scroll=ft.ScrollMode.AUTO,spacing=16)
