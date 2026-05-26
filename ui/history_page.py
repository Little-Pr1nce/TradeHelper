"""
历史报告页面

提供以下功能：
  1. 历史分析报告列表（按时间倒序，支持按股票代码筛选）
  2. 报告详情查看（Markdown 渲染 + K 线图）
  3. 历史报告重新评分
  4. 历史报告重新导出 PDF
  5. 删除历史报告

数据来源：SQLite 数据库 reports 表。
页面布局：左侧报告列表 + 右侧报告详情（左右分栏）。

【扩展点】增强报告管理功能：
  1. 添加按评分排序和筛选（只看高分/低分报告）
  2. 添加按回测周期筛选
  3. 添加批量导出功能
  4. 添加报告对比功能（并排查看两只股票的报告）
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
        self._reports: list[AnalysisReport] = []   # 缓存当前加载的报告列表
        self._selected_report: AnalysisReport | None = None  # 当前选中的报告

    def build(self):
        """构建历史报告页控件树。"""
        # --- 搜索栏 ---
        self._search_field = ft.TextField(
            label="搜索股票代码",
            hint_text="输入股票代码筛选（如 600519、AAPL）",
            width=200,
            on_change=self._on_search_change,
        )

        # --- 报告列表（左侧面板） ---
        self._report_list = ft.ListView(
            expand=True,
            spacing=8,
            height=300,
        )

        # --- 报告详情（右侧面板） ---
        self._detail_content = ft.Markdown(
            value="",
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            visible=False,
        )

        self._detail_chart = ft.Image(
            src="",
            visible=False,
            fit="contain",
            width=600,
        )

        self._detail_rating = StarRating(
            max_stars=5,
            initial_rating=0,
            on_change=self._on_detail_rating,
        )
        self._detail_rating.visible = False

        self._export_btn = ft.Button(
            content=ft.Text("导出 PDF"),
            icon=ft.Icons.PICTURE_AS_PDF,
            on_click=self._on_export_pdf,
            visible=False,
        )
        self._delete_btn = ft.Button(
            content=ft.Text("删除", color=ft.Colors.RED),
            icon=ft.Icons.DELETE,
            icon_color=ft.Colors.RED,
            on_click=self._on_delete_report,
            visible=False,
        )

        # --- 左侧面板：列表 ---
        list_panel = ft.Container(
            expand=1,  # flex 比例 1
            content=ft.Column([
                ft.Text("历史报告", size=18, weight=ft.FontWeight.BOLD),
                self._search_field,
                self._report_list,
            ]),
        )

        # --- 右侧面板：详情 ---
        detail_panel = ft.Container(
            expand=2,  # flex 比例 2
            visible=False,
            content=ft.Column(
                scroll=ft.ScrollMode.AUTO,
                spacing=12,
                controls=[
                    ft.Text("报告详情", size=18, weight=ft.FontWeight.BOLD),
                    self._detail_chart,
                    self._detail_content,
                    ft.Row(
                        spacing=12,
                        controls=[
                            self._export_btn,
                            self._delete_btn,
                            ft.Text("评分：", size=14),
                            self._detail_rating,
                        ],
                    ),
                ],
            ),
        )

        self._detail_panel = detail_panel

        # 左右分栏布局
        self.content = ft.Row(
            expand=True,
            spacing=20,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[list_panel, detail_panel],
        )
        return self.content

    def did_mount(self):
        pass

    # ======================== 数据加载 ========================

    def _load_reports(self, code_filter: str = ""):
        """
        从数据库加载报告列表。

        Args:
            code_filter: 股票代码筛选（空字符串表示全部）
        """
        db = Database()
        if code_filter:
            self._reports = db.get_reports_by_code(code_filter.upper())
        else:
            self._reports = db.get_all_reports()

        self._report_list.controls.clear()

        if not self._reports:
            self._report_list.controls.append(
                ft.Container(
                    padding=20,
                    content=ft.Text("暂无历史报告", color=ft.Colors.GREY_500, size=14),
                )
            )
        else:
            for report in self._reports:
                self._report_list.controls.append(self._build_report_item(report))

        self._report_list.update()

    def _build_report_item(self, report: AnalysisReport) -> ft.Container:
        """
        构建单条报告的列表项控件。

        显示信息：
          - 股票名称和代码
          - 市场、回测周期、创建时间、评分星级

        Args:
            report: 报告数据

        Returns:
            可点击的 Container 控件
        """
        stars = "★" * report.rating + "☆" * (5 - report.rating) if report.rating else "未评分"
        market_label = "A股" if report.market == "A" else "美股"
        period_label = {"3m": "3个月", "6m": "6个月", "1y": "1年", "3y": "3年"}.get(
            report.backtest_period, report.backtest_period)

        title = ft.Text(
            f"{report.name} ({report.code})",
            size=15,
            weight=ft.FontWeight.BOLD,
        )
        subtitle = ft.Text(
            f"{market_label} | {period_label} | {report.create_time[:16]} | {stars}",
            size=12,
            color=ft.Colors.GREY_600,
        )

        return ft.Container(
            bgcolor=ft.Colors.GREY_100,
            border_radius=8,
            padding=12,
            on_click=lambda e, r=report: self._view_report(r),
            content=ft.Column(spacing=4, controls=[title, subtitle]),
        )

    # ======================== 报告查看与操作 ========================

    def _view_report(self, report: AnalysisReport):
        """
        查看报告详情。

        将选中报告的内容渲染到右侧面板，并显示操作按钮。
        如果报告关联了 K 线图，尝试加载展示。
        """
        self._selected_report = report
        self._detail_content.value = report.content
        self._detail_content.visible = True
        self._detail_content.update()

        # 加载关联的 K 线图：优先使用 chart_path 字段（生成报告时即写入），
        # 老报告若无该字段则隐藏图表
        import os
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

    def _on_search_change(self, e):
        """搜索框内容变化 → 重新筛选报告列表。"""
        self._load_reports(self._search_field.value)

    def _on_detail_rating(self, rating: int):
        """
        为历史报告重新评分。

        更新数据库并刷新列表显示。
        """
        if self._selected_report and self._selected_report.id:
            Database().update_report_rating(self._selected_report.id, rating)
            self._selected_report.rating = rating
            self._load_reports(self._search_field.value)  # 刷新列表中的星级显示

    def _on_export_pdf(self, e):
        """为历史报告重新导出 PDF。"""
        if not self._selected_report:
            return
        report = self._selected_report
        pdf_path = export_report_to_pdf(
            report.content, "", report.name, report.code, report.backtest_period
        )
        if pdf_path:
            if report.id:
                Database().update_report_pdf(report.id, pdf_path)
            self.page.snack_bar = ft.SnackBar(
                ft.Text(f"PDF 已导出：{pdf_path}"),
                bgcolor=ft.Colors.GREEN_700,
            )
            self.page.snack_bar.open = True
            self.page.update()

    def _on_delete_report(self, e):
        """删除选中的报告，同时清理磁盘上的 chart 和 PDF。"""
        import os
        if not self._selected_report or not self._selected_report.id:
            return
        report = self._selected_report
        # 先删除关联的磁盘文件（缺失或失败都不影响数据库删除）
        for path in (report.chart_path, report.pdf_path):
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        Database().delete_report(report.id)
        self._selected_report = None
        self._detail_panel.visible = False
        self._detail_panel.update()
        self._load_reports(self._search_field.value)  # 刷新列表
