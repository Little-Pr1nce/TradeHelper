"""TradeHelper desktop visual tokens and small layout helpers."""
from __future__ import annotations

import flet as ft


BACKGROUND = "#F4F6F8"
SURFACE = "#FFFFFF"
SURFACE_MUTED = "#F8FAFC"
BORDER = "#D9E0E6"
PRIMARY = "#146C94"
PRIMARY_DARK = "#0B4F6C"
PRIMARY_SOFT = "#E7F2F6"
NAV = "#17242D"
NAV_SELECTED = "#263B47"
TEXT = "#17232C"
TEXT_MUTED = "#667680"
SUCCESS = "#287A55"
WARNING = "#A96612"
DANGER = "#B33A3A"


def panel(content: ft.Control, *, padding: int = 20, expand: bool | int | None = None) -> ft.Container:
    return ft.Container(
        content=content,
        bgcolor=SURFACE,
        border=ft.Border.all(1, BORDER),
        border_radius=8,
        padding=padding,
        expand=expand,
        shadow=ft.BoxShadow(
            blur_radius=8,
            color=ft.Colors.with_opacity(0.035, ft.Colors.BLACK),
            offset=ft.Offset(0, 2),
        ),
    )


def page_heading(title: str, subtitle: str, actions: list[ft.Control] | None = None) -> ft.Row:
    return ft.Row(
        controls=[
            ft.Column(
                controls=[
                    ft.Text(title, size=22, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(subtitle, size=12, color=TEXT_MUTED),
                ],
                spacing=2,
                tight=True,
            ),
            ft.Row(controls=actions or [], spacing=8, wrap=True),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        wrap=True,
    )


def primary_button(label: str, icon, on_click, *, disabled: bool = False) -> ft.Button:
    return ft.Button(
        label,
        icon=icon,
        on_click=on_click,
        disabled=disabled,
        color={
            ft.ControlState.DEFAULT: ft.Colors.WHITE,
            ft.ControlState.DISABLED: "#8DA0AA",
        },
        bgcolor={
            ft.ControlState.DEFAULT: PRIMARY,
            ft.ControlState.HOVERED: PRIMARY_DARK,
            ft.ControlState.PRESSED: "#083C52",
            ft.ControlState.DISABLED: "#DCE4E8",
        },
        elevation={ft.ControlState.DEFAULT: 0, ft.ControlState.HOVERED: 2},
        height=42,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=8),
            padding=ft.Padding(18, 10, 18, 10),
        ),
    )


def secondary_button(label: str, icon, on_click, *, disabled: bool = False) -> ft.Button:
    return ft.Button(
        label,
        icon=icon,
        on_click=on_click,
        disabled=disabled,
        color={ft.ControlState.DEFAULT: TEXT, ft.ControlState.DISABLED: "#9AA7AE"},
        bgcolor={
            ft.ControlState.DEFAULT: SURFACE,
            ft.ControlState.HOVERED: "#EDF3F5",
            ft.ControlState.PRESSED: "#E3ECEF",
            ft.ControlState.DISABLED: "#F3F5F6",
        },
        elevation={ft.ControlState.DEFAULT: 0, ft.ControlState.HOVERED: 1},
        height=42,
        style=ft.ButtonStyle(
            side=ft.BorderSide(1, BORDER),
            shape=ft.RoundedRectangleBorder(radius=8),
            padding=ft.Padding(14, 10, 14, 10),
        ),
    )


def configure_field(control: ft.Control) -> ft.Control:
    if hasattr(control, "border_radius"):
        control.border_radius = 8
    if hasattr(control, "border_color"):
        control.border_color = BORDER
    if hasattr(control, "focused_border_color"):
        control.focused_border_color = PRIMARY
    if hasattr(control, "content_padding"):
        control.content_padding = ft.Padding(12, 10, 12, 10)
    return control


def feedback_banner(message: str | None, *, error: bool = False) -> ft.Control | None:
    if not message:
        return None
    color = DANGER if error else SUCCESS
    background = "#FFF0F0" if error else "#EAF6EF"
    icon = ft.Icons.ERROR_OUTLINE if error else ft.Icons.CHECK_CIRCLE_OUTLINE
    return ft.Container(
        content=ft.Row([
            ft.Icon(icon, size=18, color=color),
            ft.Text(message, color=color, expand=True, selectable=True),
        ], spacing=9),
        bgcolor=background,
        border=ft.Border(left=ft.BorderSide(4, color)),
        border_radius=6,
        padding=ft.Padding(12, 10, 12, 10),
    )
