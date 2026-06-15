"""
历史报告页面。
"""

import flet as ft

from data.database import Database
from data.models import AnalysisReport
from ui.components import StarRating
from report.pdf_exporter import export_report_to_pdf


class HistoryPage(ft.Container):
    """历史报告页面。"""

    def __init__(self):
        super().__init__()
        self._reports: list[AnalysisReport] = []
        self._selected_report: AnalysisReport | None = None
        self._checked_report_ids: set[int] = set()

    def build(self):
        self._search_field = ft.TextField(
            label="搜索股票代码", hint_text="如 600519、AAPL",
            width=180, on_change=self._on_filter_change,
        )
        self._market_filter = ft.Dropdown(
            label="市场", width=110, value="",
            options=[ft.dropdown.Option("", "全部"), ft.dropdown.Option("US", "美股"), ft.dropdown.Option("A", "A股")],
        )
        self._mode_filter = ft.Dropdown(
            label="模式", width=130, value="",
            options=[
                ft.dropdown.Option("", "全部"), ft.dropdown.Option("eod", "盘后"),
                ft.dropdown.Option("intraday", "盘中"), ft.dropdown.Option("pre", "盘前"),
            ],
        )
        self._period_filter = ft.Dropdown(
            label="周期", width=110, value="",
            options=[
                ft.dropdown.Option("", "全部"), ft.dropdown.Option("6m", "6个月"),
                ft.dropdown.Option("1y", "1年"), ft.dropdown.Option("3y", "3年"),
            ],
        )
        self._rating_filter = ft.Dropdown(
            label="评分", width=120, value="",
            options=[
                ft.dropdown.Option("", "全部"), ft.dropdown.Option("5", "≥5星"),
                ft.dropdown.Option("4", "≥4星"), ft.dropdown.Option("3", "≥3星"),
            ],
        )

        for dropdown in (
            self._market_filter, self._mode_filter,
            self._period_filter, self._rating_filter,
        ):
            dropdown.on_change = self._on_filter_change

        self._report_list = ft.ListView(expand=True, spacing=8, height=300)
        self._detail_content = ft.Markdown(
            value="", selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            visible=False,
        )
        self._detail_chart = ft.Image(src="", visible=False, fit="contain", width=600)
        self._detail_rating = StarRating(max_stars=5, initial_rating=0, on_change=self._on_detail_rating)
        self._detail_rating.visible = False
        self._export_btn = ft.Button(
            content=ft.Text("导出 PDF"), icon=ft.Icons.PICTURE_AS_PDF,
            on_click=self._on_export_pdf, visible=False,
        )
        self._delete_btn = ft.Button(
            content=ft.Text("删除", color=ft.Colors.RED), icon=ft.Icons.DELETE,
            icon_color=ft.Colors.RED, on_click=self._on_delete_report, visible=False,
        )
        self._compare_btn = ft.Button(
            content=ft.Text("对比选中"), icon=ft.Icons.COMPARE_ARROWS,
            on_click=self._on_compare_reports,
        )
        self._batch_export_btn = ft.Button(
            content=ft.Text("批量导出"), icon=ft.Icons.DOWNLOAD,
            on_click=self._on_batch_export,
        )

        list_panel = ft.Container(
            expand=1,
            content=ft.Column([
                ft.Text("历史报告", size=18, weight=ft.FontWeight.BOLD),
                ft.Row(wrap=True, spacing=8, controls=[
                    self._search_field, self._market_filter, self._mode_filter,
                    self._period_filter, self._rating_filter,
                ]),
                ft.Row(spacing=8, controls=[self._compare_btn, self._batch_export_btn]),
                self._report_list,
            ]),
        )
        detail_panel = ft.Container(
            expand=2, visible=False,
            content=ft.Column(
                scroll=ft.ScrollMode.AUTO, spacing=12,
                controls=[
                    ft.Text("报告详情", size=18, weight=ft.FontWeight.BOLD),
                    self._detail_chart,
                    self._detail_content,
                    ft.Row(spacing=12, controls=[
                        self._export_btn, self._delete_btn,
                        ft.Text("评分：", size=14), self._detail_rating,
                    ]),
                ],
            ),
        )
        self._detail_panel = detail_panel
        self.content = ft.Row(
            expand=True, spacing=20,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[list_panel, detail_panel],
        )
        return self.content

    def did_mount(self):
        pass

    def _load_reports(self, code_filter: str = ""):
        db = Database()
        min_rating = int(self._rating_filter.value) if getattr(self, "_rating_filter", None) and self._rating_filter.value else None
        code = code_filter or (self._search_field.value if getattr(self, "_search_field", None) else "")
        self._reports = db.filter_reports(
            code=code,
            market=self._market_filter.value if getattr(self, "_market_filter", None) else "",
            mode=self._mode_filter.value if getattr(self, "_mode_filter", None) else "",
            period=self._period_filter.value if getattr(self, "_period_filter", None) else "",
            min_rating=min_rating,
        )
        self._report_list.controls.clear()
        if not self._reports:
            self._report_list.controls.append(ft.Container(
                padding=20, content=ft.Text("暂无历史报告", color=ft.Colors.GREY_500, size=14),
            ))
        else:
            for report in self._reports:
                self._report_list.controls.append(self._build_report_item(report))
        self._report_list.update()

    def _build_report_item(self, report: AnalysisReport) -> ft.Container:
        stars = "★" * report.rating + "☆" * (5 - report.rating) if report.rating else "未评分"
        market_label = "A股" if report.market == "A" else "美股"
        mode_label = {"eod": "盘后", "intraday": "盘中", "pre": "盘前"}.get(report.mode, report.mode)
        period_label = {"3m": "3个月", "6m": "6个月", "1y": "1年", "3y": "3年"}.get(report.backtest_period, report.backtest_period)
        checkbox = ft.Checkbox(
            value=bool(report.id and report.id in self._checked_report_ids),
            on_change=lambda e, r=report: self._toggle_checked(r, e.control.value),
        )
        return ft.Container(
            bgcolor=ft.Colors.GREY_100, border_radius=8, padding=12,
            on_click=lambda e, r=report: self._view_report(r),
            content=ft.Row(controls=[
                checkbox,
                ft.Column(expand=True, spacing=4, controls=[
                    ft.Text(f"{report.name} ({report.code})", size=15, weight=ft.FontWeight.BOLD),
                    ft.Text(
                        f"{market_label} | {mode_label} | {period_label} | {report.create_time[:16]} | {stars}",
                        size=12, color=ft.Colors.GREY_600,
                    ),
                ]),
            ]),
        )

    def _toggle_checked(self, report: AnalysisReport, checked: bool):
        if not report.id:
            return
        if checked:
            self._checked_report_ids.add(report.id)
        else:
            self._checked_report_ids.discard(report.id)

    def _view_report(self, report: AnalysisReport):
        import os
        self._selected_report = report
        self._detail_content.value = report.content
        self._detail_content.visible = True
        self._detail_content.update()
        chart_path = report.chart_path or ""
        if chart_path and os.path.exists(chart_path):
            self._detail_chart.src = chart_path
            self._detail_chart.visible = True
        else:
            self._detail_chart.visible = False
        self._detail_chart.update()
        self._detail_rating.rating = report.rating or 0
        self._detail_rating.visible = True
        self._detail_rating.update()
        self._export_btn.visible = True
        self._export_btn.update()
        self._delete_btn.visible = True
        self._delete_btn.update()
        self._detail_panel.visible = True
        self._detail_panel.update()

    def _on_filter_change(self, e):
        self._load_reports(self._search_field.value)

    def _on_search_change(self, e):
        self._on_filter_change(e)

    def _on_detail_rating(self, rating: int):
        if self._selected_report and self._selected_report.id:
            Database().update_report_rating(self._selected_report.id, rating)
            self._selected_report.rating = rating
            self._load_reports(self._search_field.value)

    def _on_export_pdf(self, e):
        if not self._selected_report:
            return
        self._export_one(self._selected_report, show_message=True)

    def _export_one(self, report: AnalysisReport, show_message: bool = False) -> str | None:
        pdf_path = export_report_to_pdf(
            report.content, report.chart_path or "", report.name, report.code, report.backtest_period,
        )
        if pdf_path and report.id:
            Database().update_report_pdf(report.id, pdf_path)
        if pdf_path and show_message:
            self.page.snack_bar = ft.SnackBar(ft.Text(f"PDF 已导出：{pdf_path}"), bgcolor=ft.Colors.GREEN_700)
            self.page.snack_bar.open = True
            self.page.update()
        return pdf_path

    def _on_batch_export(self, e):
        selected = [r for r in self._reports if r.id in self._checked_report_ids]
        if not selected:
            selected = self._reports
        count = 0
        for report in selected:
            if self._export_one(report):
                count += 1
        self.page.snack_bar = ft.SnackBar(ft.Text(f"已导出 {count} 份报告"), bgcolor=ft.Colors.GREEN_700)
        self.page.snack_bar.open = True
        self.page.update()

    def _on_compare_reports(self, e):
        selected = [r for r in self._reports if r.id in self._checked_report_ids][:2]
        if len(selected) < 2:
            self.page.snack_bar = ft.SnackBar(ft.Text("请至少勾选 2 份报告进行对比"), bgcolor=ft.Colors.ORANGE_700)
            self.page.snack_bar.open = True
            self.page.update()
            return
        left, right = selected
        self._detail_content.value = (
            f"# 报告对比\n\n"
            f"| 项目 | {left.name} ({left.code}) | {right.name} ({right.code}) |\n"
            f"|------|----------------|----------------|\n"
            f"| 市场 | {left.market} | {right.market} |\n"
            f"| 模式 | {left.mode} | {right.mode} |\n"
            f"| 周期 | {left.backtest_period} | {right.backtest_period} |\n"
            f"| 评分 | {left.rating or '未评分'} | {right.rating or '未评分'} |\n"
            f"| 时间 | {left.create_time[:16]} | {right.create_time[:16]} |\n\n"
            f"## {left.name}\n\n{left.content}\n\n---\n\n## {right.name}\n\n{right.content}"
        )
        self._detail_content.visible = True
        self._detail_chart.visible = False
        self._detail_panel.visible = True
        self._detail_content.update()
        self._detail_chart.update()
        self._detail_panel.update()

    def _on_delete_report(self, e):
        import os
        if not self._selected_report or not self._selected_report.id:
            return
        report = self._selected_report
        for path in (report.chart_path, report.pdf_path):
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        Database().delete_report(report.id)
        self._checked_report_ids.discard(report.id)
        self._selected_report = None
        self._detail_panel.visible = False
        self._detail_panel.update()
        self._load_reports(self._search_field.value)
