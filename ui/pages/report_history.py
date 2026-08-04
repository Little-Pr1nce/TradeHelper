"""Historical report browser over immutable ReportSnapshot records."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import flet as ft

from contracts import DecisionMode, Market, ReportHistoryQuery, ReportKind
from presentation.formatting import format_datetime
from ..components.date_field import calendar_date_range_field
from ..components.report_view import report_view
from ..theme import DANGER, PRIMARY, TEXT_MUTED, configure_field, feedback_banner, page_heading, panel, primary_button, secondary_button
from ..update_dispatch import rebuild_on_page


class ReportHistoryPage:
    def __init__(self, service, clock=None, lookup_port=None):
        self.service = service
        self.clock = clock
        self.lookup_port = lookup_port
        self.query = ReportHistoryQuery()
        self.page = None
        self.document = None
        self.selected = []
        self.comparison = ()
        self.error = None
        self._root = None

    def _update(self):
        rebuild_on_page(self._root, self._content)

    def load(self, query=None):
        if query is not None: self.query = query
        self.page = self.service.list(self.query)
        return self.page

    def open(self, report_id):
        self.document = self.service.get_document(report_id); self._update(); return self.document

    def _back(self, _event=None):
        self.document = None; self._update()

    @staticmethod
    def _date(value, market=None, *, end=False):
        if not value.strip():
            return None
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
        zone = ZoneInfo("America/New_York") if market is Market.US else ZoneInfo("Asia/Shanghai")
        return datetime.combine(parsed, time.max if end else time.min, tzinfo=zone).astimezone(timezone.utc)

    @staticmethod
    def _date_value(value, market=None):
        if value is None:
            return ""
        zone = ZoneInfo("America/New_York") if market is Market.US else ZoneInfo("Asia/Shanghai")
        return value.astimezone(zone).date().isoformat()

    def _filter(self, market, kind, instrument, mode, period, date_range, rating, archived):
        def handler(_event=None):
            try:
                selected_market = None if market.value == "all" else Market(market.value)
                selected_instrument = None
                if instrument.value.strip():
                    if selected_market is None or self.lookup_port is None:
                        raise ValueError("按股票筛选时请先选择市场，并配置股票检索服务")
                    matches = tuple(self.lookup_port(selected_market.value, instrument.value.strip()))
                    if not matches:
                        raise ValueError("未找到用于筛选的股票")
                    selected_instrument = getattr(matches[0], "instrument", matches[0])
                self.query = ReportHistoryQuery(
                    market=selected_market,
                    report_kind=None if kind.value == "all" else ReportKind(kind.value),
                    instrument=selected_instrument,
                    analysis_mode=None if mode.value == "all" else DecisionMode(mode.value),
                    history_period=None if period.value == "all" else period.value,
                    date_from=self._date(date_range.start_value, selected_market),
                    date_to=self._date(date_range.end_value, selected_market, end=True),
                    minimum_rating=None if rating.value == "all" else int(rating.value),
                    include_archived=archived.value,
                    page_size=self.query.page_size,
                )
                self.load(); self.error = None
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    def _change_page(self, delta):
        def handler(_event=None):
            target = max(1, self.query.page + delta)
            self.load(replace(self.query, page=target)); self._update()
        return handler

    def _select(self, report_id):
        def handler(event):
            if event.control.value and report_id not in self.selected:
                if len(self.selected) >= 3:
                    event.control.value = False; self.error = "最多比较 3 份报告"
                else: self.selected.append(report_id)
            elif not event.control.value and report_id in self.selected:
                self.selected.remove(report_id)
            self._update()
        return handler

    def _compare(self, _event=None):
        try:
            documents = tuple(self.service.get_document(report_id) for report_id in self.selected)
            self.comparison = self.service.compare(documents)
            self.error = None
        except Exception as exc:
            self.error = str(exc)
        self._update()

    @staticmethod
    def _comparison_value(document, section_id):
        section = next((item for item in document.sections if item.section_id == section_id), None)
        if section is None:
            return "暂无"
        for block in section.blocks:
            table = block.payload if hasattr(block.payload, "rows") else None
            if table is not None:
                return "暂无" if not table.rows else " / ".join(table.rows[0].cells[:4])
        return " / ".join(str(block.payload) for block in section.blocks)[:240] or "暂无"

    def _archive(self, report_id, archived):
        def handler(_event=None):
            try:
                self.service.archive(report_id, archived=archived)
                self.load()
                self.error = "报告已归档。" if archived else "报告已恢复。"
            except Exception as exc:
                self.error = f"报告状态更新失败：{exc}"
            self._update()
        return handler

    def _rate(self, rating, note):
        def handler(_event=None):
            if self.document is None or self.clock is None: return
            self.service.rate(self.document.report_id, int(rating.value), note=note.value.strip() or None, created_at=self.clock())
            self.error = "评分已记录"
            self._update()
        return handler

    def _list_content(self):
        market = configure_field(ft.Dropdown(label="市场", value=self.query.market.value if self.query.market else "all", options=[ft.dropdown.Option("all", "全部"), ft.dropdown.Option("US", "美股"), ft.dropdown.Option("A", "A股")], col={"sm": 6, "md": 2}))
        kind = configure_field(ft.Dropdown(label="报告类型", value=self.query.report_kind.value if self.query.report_kind else "all", options=[ft.dropdown.Option("all", "全部"), ft.dropdown.Option("single_stock", "单股"), ft.dropdown.Option("portfolio", "组合")], col={"sm": 6, "md": 2}))
        instrument = configure_field(ft.TextField(label="股票代码或公司名", hint_text="代码 / 公司名", value=self.query.instrument.code if self.query.instrument else "", col={"sm": 12, "md": 5}))
        mode = configure_field(ft.Dropdown(label="模式", value=self.query.analysis_mode.value if self.query.analysis_mode else "all", options=[ft.dropdown.Option("all", "全部"), ft.dropdown.Option("pre", "盘前"), ft.dropdown.Option("intraday", "盘中"), ft.dropdown.Option("eod", "盘后")], col={"sm": 6, "md": 3}))
        period = configure_field(ft.Dropdown(label="周期", value=self.query.history_period or "all", options=[ft.dropdown.Option("all", "全部"), ft.dropdown.Option("1m", "1个月"), ft.dropdown.Option("3m", "3个月"), ft.dropdown.Option("6m", "6个月"), ft.dropdown.Option("1y", "1年")], width=125))
        date_range = calendar_date_range_field(
            "日期范围",
            self._date_value(self.query.date_from, self.query.market),
            self._date_value(self.query.date_to, self.query.market),
            width=260,
        )
        rating = configure_field(ft.Dropdown(label="最低评分", value=str(self.query.minimum_rating) if self.query.minimum_rating else "all", options=[ft.dropdown.Option("all", "全部"), *(ft.dropdown.Option(str(item), f"{item} 星及以上") for item in range(1, 6))], width=140))
        archived = ft.Checkbox(label="显示已归档", value=self.query.include_archived)
        rows = []
        for item in (() if self.page is None else self.page.items):
            subject = item.instrument.code if item.instrument else "组合"
            rows.append(ft.Row([
                ft.Checkbox(value=item.report_id in self.selected, on_change=self._select(item.report_id)),
                ft.Text(format_datetime(item.as_of, item.market), width=190),
                ft.Text(item.market.value, width=50), ft.Text(subject, width=90),
                ft.Text(item.analysis_mode.value, width=75), ft.Text(item.history_period, width=65),
                ft.Text("未评分" if item.latest_rating is None else f"{item.latest_rating}/5", width=70),
                ft.IconButton(ft.Icons.OPEN_IN_NEW, tooltip="打开报告", on_click=lambda _e, report_id=item.report_id: self.open(report_id)),
                ft.IconButton(ft.Icons.UNARCHIVE if item.archived else ft.Icons.ARCHIVE_OUTLINED, tooltip="恢复" if item.archived else "归档", on_click=self._archive(item.report_id, not item.archived)),
            ], scroll=ft.ScrollMode.AUTO))
        filter_action = primary_button("筛选", ft.Icons.FILTER_ALT, self._filter(market, kind, instrument, mode, period, date_range, rating, archived))
        compare_action = secondary_button("比较报告", ft.Icons.COMPARE_ARROWS, self._compare, disabled=not self.selected)
        result_controls = [ft.Row([
            ft.Text(f"共 {0 if self.page is None else self.page.total_count} 份冻结报告", weight=ft.FontWeight.BOLD),
            ft.Text("最多选择 3 份报告横向比较", size=11, color=TEXT_MUTED),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True)]
        if rows:
            result_controls.append(ft.Container(
                ft.Row([
                    ft.Text("", width=40), ft.Text("生成时间", width=190),
                    ft.Text("市场", width=50), ft.Text("对象", width=90),
                    ft.Text("模式", width=75), ft.Text("周期", width=65),
                    ft.Text("评分", width=70), ft.Text("操作", width=96),
                ], scroll=ft.ScrollMode.AUTO),
                padding=ft.Padding.symmetric(vertical=7), bgcolor="#EAF4F7",
            ))
        result_controls.extend(rows or [ft.Text("暂无匹配报告", color=TEXT_MUTED)])
        result_controls.append(ft.Row([
            ft.IconButton(ft.Icons.CHEVRON_LEFT, tooltip="上一页", on_click=self._change_page(-1), disabled=self.query.page <= 1),
            ft.Text(f"第 {self.query.page} 页"),
            ft.IconButton(ft.Icons.CHEVRON_RIGHT, tooltip="下一页", on_click=self._change_page(1), disabled=self.page is None or not self.page.has_next),
        ]))
        if self.comparison:
            result_controls.append(ft.Text("报告比较", theme_style=ft.TextThemeStyle.TITLE_LARGE))
            comparison = ft.DataTable(
                columns=[ft.DataColumn(ft.Text(name)) for name in ("报告", "当前动作", "预测", "风险", "数据质量", "历史结果", "数据时点")],
                rows=[ft.DataRow(cells=[ft.DataCell(ft.Text(value, selectable=True)) for value in (
                    item.title, item.summary,
                    self._comparison_value(item, "forecast"),
                    self._comparison_value(item, "risk"),
                    self._comparison_value(item, "facts"),
                    self._comparison_value(item, "history"),
                    item.as_of.isoformat(),
                )]) for item in self.comparison],
            )
            result_controls.append(ft.Row([comparison], scroll=ft.ScrollMode.AUTO))
        controls = [
            page_heading("报告记录", "查找过去生成的单股和组合报告；常用条件放在第一层"),
            panel(ft.Column([
                ft.Text("查找报告", size=14, weight=ft.FontWeight.BOLD),
                ft.ResponsiveRow(
                    [market, kind, instrument, mode],
                    columns=12, spacing=10, run_spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Container(
                    ft.Row([
                        date_range.control,
                        ft.Text(
                            "按报告生成日期筛选；留空表示全部日期。",
                            size=11, color=TEXT_MUTED, expand=True,
                        ),
                        filter_action,
                    ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding.only(top=8),
                    border=ft.Border(top=ft.BorderSide(1, "#D9E0E6")),
                ),
                ft.ExpansionTile(
                    title=ft.Text("更多条件与报告比较", size=13, weight=ft.FontWeight.W_600),
                    subtitle=ft.Text("周期、评分、归档和横向比较", size=11, color=TEXT_MUTED),
                    controls=[ft.Container(
                        ft.Row(
                            [period, rating, archived, compare_action],
                            wrap=True, spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.Padding(8, 4, 8, 10),
                    )],
                    maintain_state=True,
                    dense=True,
                    min_tile_height=52,
                    tile_padding=ft.Padding(4, 0, 4, 0),
                    controls_padding=0,
                    expanded_cross_axis_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ], spacing=12)),
        ]
        if self.error:
            controls.append(ft.Text(self.error, color=DANGER if "最多" in self.error else PRIMARY))
        controls.append(panel(ft.Column(result_controls, spacing=8)))
        return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)

    def _detail_content(self):
        rating = ft.Dropdown(label="评分", value="5", options=[ft.dropdown.Option(str(item)) for item in range(1, 6)], width=100)
        note = ft.TextField(label="评分备注（可选）", max_length=1000, width=320)
        toolbar = ft.Row([ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回历史列表", on_click=self._back), rating, note, ft.IconButton(ft.Icons.STAR, tooltip="提交评分", on_click=self._rate(rating, note))])
        controls = [page_heading("报告详情", "该报告使用当时冻结的数据与模型版本", [*toolbar.controls])]
        banner = feedback_banner(self.error, error=bool(self.error and ("失败" in self.error or "最多" in self.error)))
        if banner is not None:
            controls.append(banner)
        controls.append(panel(report_view(self.document)))
        return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=14)

    def _content(self):
        return self._detail_content() if self.document else self._list_content()

    def build(self):
        if self.page is None: self.load()
        self._root = ft.Container(content=self._content(), expand=True, padding=24)
        return self._root
