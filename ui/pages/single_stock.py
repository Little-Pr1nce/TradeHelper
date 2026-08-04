from __future__ import annotations

import asyncio
import inspect

import flet as ft

from contracts import ExportFormat
from ..components.progress_panel import progress_panel
from ..components.report_view import report_view
from ..theme import PRIMARY, TEXT_MUTED, configure_field, feedback_banner, page_heading, panel, primary_button, secondary_button
from ..update_dispatch import rebuild_on_page


class SingleStockPage:
    """Stateful Tab1 view driven only by injected application ports."""

    def __init__(self, lookup_port=None, analysis_port=None, export_port=None):
        self.lookup_port = lookup_port
        self.analysis_port = analysis_port
        self.export_port = export_port
        self.last_input = {"market": "US", "symbol": "", "mode": "eod", "history_period": "1y"}
        self.document = None
        self.error = None
        self.progress = None
        self.running_task_id = None
        self.busy = False
        self.notice = None
        self.suggestions = ()
        self._lookup_revision = 0
        self._root = None

    @property
    def is_full_width_result(self):
        return self.document is not None

    def _update(self):
        rebuild_on_page(self._root, self._content)

    def set_document(self, document):
        self.document = document
        self.error = None
        self.notice = "分析完成，报告已冻结并保存。"
        self.progress = None
        self.running_task_id = None
        self.busy = False
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
            try:
                self.suggestions = self.suggest(value)
                self.error = None
            except Exception as exc:
                self.suggestions = ()
                self.error = f"股票检索失败：{exc}"
            self._update()

    def _on_progress(self, value):
        self.progress = value
        self.running_task_id = getattr(value, "task_id", self.running_task_id)
        self._update()

    def _on_complete(self, document):
        self.set_document(document)

    def _on_error(self, error):
        message = str(error) or "分析失败"
        if "ANALYSIS_CANCELLED" in message:
            self.error = None
            self.notice = "分析已取消。"
        else:
            self.error = message
            self.notice = None
        self.progress = None
        self.running_task_id = None
        self.busy = False
        self._update()

    async def _start(self, _event=None):
        if not self.validate():
            self._update(); return
        if self.analysis_port is None:
            self.error = "分析服务尚未配置"
            self._update(); return
        self.error = None
        self.notice = "正在创建分析任务，请稍候。"
        self.busy = True
        self._update()
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
                self.notice = "分析任务已启动，进度会持续更新。"
                self._update()
        except Exception as exc:
            self._on_error(exc)

    def _cancel(self, _event=None):
        if self.running_task_id and self.analysis_port is not None:
            cancel = getattr(self.analysis_port, "cancel", None)
            if cancel:
                cancel(self.running_task_id)
        self.running_task_id = None
        self.busy = False
        self.progress = None
        self.notice = "已请求取消分析。"
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
                try:
                    self.error = None
                    self.notice = "正在导出报告，请稍候..."
                    self._update()
                    artifact = self.export_port(self.document, format=format)
                    status = getattr(getattr(artifact, "status", None), "value", None)
                    if status == "failed":
                        raise RuntimeError(getattr(artifact, "error_code", None) or "报告导出失败")
                    self.error = None
                    self.notice = f"报告已导出，并已在文件管理器中定位：{getattr(artifact, 'path', format.value)}"
                except Exception as exc:
                    self.error = f"报告导出失败：{exc}"
                    self.notice = None
            self._update()
        return handler

    def _input_content(self):
        market = ft.SegmentedButton(
            width=220,
            selected=[self.last_input["market"]],
            segments=[
                ft.Segment(value="US", label=ft.Text("美股", no_wrap=True), icon=ft.Icon(ft.Icons.PUBLIC, size=16)),
                ft.Segment(value="A", label=ft.Text("A股", no_wrap=True), icon=ft.Icon(ft.Icons.SHOW_CHART, size=16)),
            ],
            on_change=lambda e: self._set_value("market", next(iter(e.control.selected))),
        )
        symbol = configure_field(ft.TextField(
            label="股票代码或公司名",
            hint_text="例如 AAPL、600519、苹果、茅台",
            value=self.last_input["symbol"],
            prefix_icon=ft.Icons.SEARCH,
            on_change=self._symbol_changed,
            on_submit=self._start,
            col={"xs": 12, "md": 6},
        ))
        mode = configure_field(ft.Dropdown(
            label="分析时段",
            value=self.last_input["mode"],
            options=[ft.dropdown.Option("pre", "盘前"), ft.dropdown.Option("intraday", "盘中"), ft.dropdown.Option("eod", "盘后")],
            on_select=lambda e: self._set_value("mode", e.control.value),
            col={"xs": 6, "md": 3},
        ))
        period = configure_field(ft.Dropdown(
            label="历史窗口",
            value=self.last_input["history_period"],
            options=[ft.dropdown.Option("1m", "1个月"), ft.dropdown.Option("3m", "3个月"), ft.dropdown.Option("6m", "6个月"), ft.dropdown.Option("1y", "1年")],
            on_select=lambda e: self._set_value("history_period", e.control.value),
            col={"xs": 6, "md": 3},
        ))
        actions = [
            primary_button("分析运行中" if self.busy else "开始分析", ft.Icons.HOURGLASS_TOP if self.busy else ft.Icons.PLAY_ARROW, self._start, disabled=self.busy),
            secondary_button("取消", ft.Icons.STOP_CIRCLE_OUTLINED, self._cancel, disabled=self.running_task_id is None),
        ]
        controls = [
            page_heading("单股分析", "从同一份事实快照生成预测、条件计划、风险分级与历史证据", actions),
            panel(ft.Column([
                ft.Row([
                    ft.Column([
                        ft.Text("分析条件", size=15, weight=ft.FontWeight.BOLD),
                        ft.Text("市场决定数据源与交易规则，分析时段决定当前价格口径", size=11, color=TEXT_MUTED),
                    ], spacing=1),
                    market,
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=True),
                ft.Divider(height=1),
                ft.ResponsiveRow([symbol, mode, period], spacing=12, run_spacing=10),
                ft.Text("默认使用 1 年历史数据完成样本外验证；较短窗口仅适合快速观察，预测模型可能因样本不足而降级。", size=11, color=TEXT_MUTED),
            ], spacing=14)),
        ]
        if self.suggestions:
            controls.append(panel(ft.Column([
                ft.Text("匹配结果", size=12, color=TEXT_MUTED),
                ft.Row([
                    ft.Button(self._suggestion_label(item), icon=ft.Icons.ADD_CHART, elevation=0, on_click=lambda _e, value=item: self._choose_suggestion(value))
                    for item in self.suggestions
                ], spacing=8, wrap=True),
            ], spacing=6), padding=12))
        banner = feedback_banner(self.error or self.notice, error=bool(self.error))
        if banner is not None:
            controls.insert(1, banner)
        if self.progress:
            controls.append(progress_panel(self.progress))
        return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)

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
        toolbar = page_heading("单股分析报告", "报告使用本次分析冻结的数据时点和账户事实", [
            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="修改分析条件", on_click=self._back),
            ft.IconButton(ft.Icons.REFRESH, tooltip="重新分析", on_click=self._reanalyze, icon_color=PRIMARY),
            ft.IconButton(ft.Icons.DESCRIPTION_OUTLINED, tooltip="导出 Markdown", on_click=self._export(ExportFormat.MARKDOWN)),
            ft.IconButton(ft.Icons.HTML, tooltip="导出 HTML", on_click=self._export(ExportFormat.HTML)),
            ft.IconButton(ft.Icons.PICTURE_AS_PDF, tooltip="导出 PDF", on_click=self._export(ExportFormat.PDF)),
        ])
        controls = [toolbar]
        banner = feedback_banner(self.error or self.notice, error=bool(self.error))
        if banner is not None:
            controls.append(banner)
        controls.append(panel(report_view(self.document)))
        return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=14)

    def _content(self):
        return self._result_content() if self.document else self._input_content()

    def build(self):
        self._root = ft.Container(content=self._content(), expand=True, padding=24)
        return self._root
