from __future__ import annotations

import asyncio
import inspect

import flet as ft

from tradehelper_v2.contracts import ExportFormat
from ..components.progress_panel import progress_panel
from ..components.report_view import report_view


class SingleStockPage:
    """Stateful Tab1 view driven only by injected application ports."""

    def __init__(self, lookup_port=None, analysis_port=None, export_port=None):
        self.lookup_port = lookup_port
        self.analysis_port = analysis_port
        self.export_port = export_port
        self.last_input = {"market": "US", "symbol": "", "mode": "eod", "history_period": "3m"}
        self.document = None
        self.error = None
        self.progress = None
        self.running_task_id = None
        self.suggestions = ()
        self._lookup_revision = 0
        self._root = None

    @property
    def is_full_width_result(self):
        return self.document is not None

    def _update(self):
        if self._root is not None and self._root.page is not None:
            self._root.content = self._content()
            self._root.update()

    def set_document(self, document):
        self.document = document
        self.error = None
        self.progress = None
        self.running_task_id = None
        self._update()

    def reanalyze_input(self):
        return dict(self.last_input)

    def suggest(self, query):
        return () if not query or self.lookup_port is None else tuple(self.lookup_port(self.last_input["market"], query))

    def validate(self):
        if not self.last_input.get("symbol", "").strip():
            self.error = "请输入股票代码或公司名"
            return False
        self.error = None
        return True

    def _set_value(self, key, value):
        self.last_input[key] = value

    async def _symbol_changed(self, event):
        value = event.control.value.strip()
        self._set_value("symbol", value)
        self._lookup_revision += 1
        revision = self._lookup_revision
        await asyncio.sleep(.3)
        if revision == self._lookup_revision:
            self.suggestions = self.suggest(value)
            self._update()

    def _on_progress(self, value):
        self.progress = value
        self.running_task_id = getattr(value, "task_id", self.running_task_id)
        self._update()

    def _on_complete(self, document):
        self.set_document(document)

    def _on_error(self, error):
        self.error = str(error) or "分析失败"
        self.running_task_id = None
        self._update()

    async def _start(self, _event=None):
        if not self.validate():
            self._update(); return
        if self.analysis_port is None:
            self.error = "分析服务尚未配置"
            self._update(); return
        self.error = None
        command = dict(self.last_input)
        method = getattr(self.analysis_port, "start_single", self.analysis_port)
        try:
            result = method(command, on_progress=self._on_progress, on_complete=self._on_complete, on_error=self._on_error)
            if inspect.isawaitable(result):
                result = await result
            if hasattr(result, "sections"):
                self.set_document(result)
            elif isinstance(result, str):
                self.running_task_id = result
        except Exception as exc:
            self._on_error(exc)

    def _cancel(self, _event=None):
        if self.running_task_id and self.analysis_port is not None:
            cancel = getattr(self.analysis_port, "cancel", None)
            if cancel:
                cancel(self.running_task_id)
        self.running_task_id = None
        self._update()

    def _back(self, _event=None):
        self.document = None
        self._update()

    async def _reanalyze(self, event=None):
        self.document = None
        await self._start(event)

    def _export(self, format):
        def handler(_event=None):
            if self.export_port is None or self.document is None:
                self.error = "导出服务尚未配置"
            else:
                self.export_port(self.document, format=format)
            self._update()
        return handler

    def _input_content(self):
        market = ft.SegmentedButton(
            selected=[self.last_input["market"]],
            segments=[ft.Segment(value="US", label=ft.Text("美股")), ft.Segment(value="A", label=ft.Text("A股"))],
            on_change=lambda e: self._set_value("market", next(iter(e.control.selected))),
        )
        symbol = ft.TextField(label="代码或公司名", value=self.last_input["symbol"], on_change=self._symbol_changed, on_submit=self._start, col={"xs": 12, "md": 5})
        mode = ft.Dropdown(label="分析模式", value=self.last_input["mode"], options=[ft.dropdown.Option("pre", "盘前"), ft.dropdown.Option("intraday", "盘中"), ft.dropdown.Option("eod", "盘后")], on_select=lambda e: self._set_value("mode", e.control.value), col={"xs": 6, "md": 3})
        period = ft.Dropdown(label="回看周期", value=self.last_input["history_period"], options=[ft.dropdown.Option("1m", "1个月"), ft.dropdown.Option("3m", "3个月"), ft.dropdown.Option("6m", "6个月"), ft.dropdown.Option("1y", "1年")], on_select=lambda e: self._set_value("history_period", e.control.value), col={"xs": 6, "md": 2})
        controls = [ft.Text("单股分析", theme_style=ft.TextThemeStyle.HEADLINE_SMALL), market, ft.ResponsiveRow([symbol, mode, period]), ft.Row([ft.Button("开始分析", icon=ft.Icons.PLAY_ARROW, on_click=self._start), ft.Button("取消", icon=ft.Icons.CANCEL_OUTLINED, on_click=self._cancel, disabled=self.running_task_id is None)])]
        if self.suggestions:
            controls.append(ft.Wrap([
                ft.Button(self._suggestion_label(item), on_click=lambda _e, value=item: self._choose_suggestion(value))
                for item in self.suggestions
            ]))
        if self.error:
            controls.insert(1, ft.Text(self.error, color=ft.Colors.RED_700))
        if self.progress:
            controls.append(progress_panel(self.progress))
        return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=14)

    def _choose_suggestion(self, value):
        instrument = getattr(value, "instrument", value)
        self.last_input["symbol"] = getattr(instrument, "code", str(instrument))
        self.suggestions = ()
        self._update()

    @staticmethod
    def _suggestion_label(value):
        instrument = getattr(value, "instrument", value)
        code = getattr(instrument, "code", str(instrument))
        name = getattr(value, "name", None)
        return f"{name} ({code})" if name else code

    def _result_content(self):
        toolbar = ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="修改分析条件", on_click=self._back),
            ft.IconButton(ft.Icons.REFRESH, tooltip="重新分析", on_click=self._reanalyze),
            ft.IconButton(ft.Icons.DESCRIPTION_OUTLINED, tooltip="导出 Markdown", on_click=self._export(ExportFormat.MARKDOWN)),
            ft.IconButton(ft.Icons.HTML, tooltip="导出 HTML", on_click=self._export(ExportFormat.HTML)),
            ft.IconButton(ft.Icons.PICTURE_AS_PDF, tooltip="导出 PDF", on_click=self._export(ExportFormat.PDF)),
        ], spacing=4)
        return ft.Column([toolbar, report_view(self.document)], expand=True)

    def _content(self):
        return self._result_content() if self.document else self._input_content()

    def build(self):
        self._root = ft.Container(content=self._content(), expand=True, padding=16)
        return self._root
