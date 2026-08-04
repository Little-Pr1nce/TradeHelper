"""Compact calendar-backed date fields shared by desktop filter pages."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import flet as ft

from ..theme import configure_field


class CalendarDateRangeField:
    """One compact field backed by Flet's native date-range calendar."""

    def __init__(
        self, label: str = "日期范围", start_value: str = "", end_value: str = "",
        *, width: int = 200,
    ):
        self.label = label
        self.start_value = start_value
        self.end_value = end_value
        self.control = configure_field(ft.TextField(
            label=label,
            value=self._display_value(),
            hint_text="全部日期",
            read_only=True,
            width=width,
            text_size=12,
        ))
        self.control.on_click = self._open_picker
        self._sync_suffix()

    @staticmethod
    def _parse(value: str):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def _display_value(self):
        if self.start_value and self.end_value:
            start = self.start_value.replace("-", "/")
            end = self.end_value.replace("-", "/")
            if self.start_value[:4] == self.end_value[:4]:
                end = end[5:]
            return f"{start} - {end}"
        if self.start_value:
            return f"{self.start_value.replace('-', '/')} 起"
        if self.end_value:
            return f"截至 {self.end_value.replace('-', '/')}"
        return ""

    def _sync_suffix(self):
        has_value = bool(self.start_value or self.end_value)
        self.control.suffix_icon = ft.IconButton(
            ft.Icons.CLOSE if has_value else ft.Icons.CALENDAR_MONTH_OUTLINED,
            tooltip="清除日期范围" if has_value else "选择日期范围",
            on_click=self._clear if has_value else self._open_picker,
        )

    def _clear(self, _event=None):
        self.start_value = ""
        self.end_value = ""
        self.control.value = ""
        self._sync_suffix()
        try:
            self.control.update()
        except RuntimeError:
            pass

    def _open_picker(self, event):
        def selected(change_event):
            start = change_event.control.start_value
            end = change_event.control.end_value
            self.start_value = "" if start is None else (start.date() if isinstance(start, datetime) else start).isoformat()
            self.end_value = "" if end is None else (end.date() if isinstance(end, datetime) else end).isoformat()
            self.control.value = self._display_value()
            self._sync_suffix()
            self.control.update()

        picker = ft.DateRangePicker(
            start_value=self._parse(self.start_value),
            end_value=self._parse(self.end_value),
            first_date=date(2000, 1, 1),
            last_date=date.today() + timedelta(days=3660),
            current_date=self._parse(self.start_value) or date.today(),
            entry_mode=ft.DatePickerEntryMode.CALENDAR,
            help_text=f"选择{self.label}",
            cancel_text="取消",
            confirm_text="确定",
            save_text="确定",
            on_change=selected,
        )
        event.control.page.show_dialog(picker)


def calendar_date_range_field(
    label: str = "日期范围", start_value: str = "", end_value: str = "", *, width: int = 200,
) -> CalendarDateRangeField:
    return CalendarDateRangeField(label, start_value, end_value, width=width)


def calendar_date_field(label: str, value: str = "", *, width: int = 170) -> ft.TextField:
    """Return a read-only field that opens Flet's native calendar picker."""
    field = configure_field(ft.TextField(
        label=label,
        value=value,
        hint_text="选择日期",
        read_only=True,
        width=width,
    ))

    def open_picker(event):
        current = None
        if field.value:
            try:
                current = date.fromisoformat(field.value)
            except ValueError:
                current = None

        def selected(change_event):
            selected_value = change_event.control.value
            if selected_value is None:
                return
            selected_date = (
                selected_value.date()
                if isinstance(selected_value, datetime)
                else selected_value
            )
            field.value = selected_date.isoformat()
            field.update()

        picker = ft.DatePicker(
            value=current,
            first_date=date(2000, 1, 1),
            last_date=date.today() + timedelta(days=3660),
            current_date=current or date.today(),
            entry_mode=ft.DatePickerEntryMode.CALENDAR,
            help_text=f"选择{label}",
            cancel_text="取消",
            confirm_text="确定",
            on_change=selected,
        )
        event.control.page.show_dialog(picker)

    field.on_click = open_picker
    field.suffix_icon = ft.IconButton(
        ft.Icons.CALENDAR_MONTH_OUTLINED,
        tooltip=f"选择{label}",
        on_click=open_picker,
    )
    return field


def clear_date_fields(*fields: ft.TextField):
    """Create a compact command that clears an optional date range."""
    def clear(_event=None):
        for field in fields:
            field.value = ""
            try:
                field.update()
            except RuntimeError:
                pass

    return ft.IconButton(
        ft.Icons.EVENT_BUSY_OUTLINED,
        tooltip="清除日期范围",
        on_click=clear,
    )
