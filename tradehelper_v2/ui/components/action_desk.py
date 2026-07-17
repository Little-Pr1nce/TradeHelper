from __future__ import annotations
import flet as ft
def action_desk(summary): return ft.Container(content=ft.Text(str(summary),selectable=True),padding=12,expand=True)
