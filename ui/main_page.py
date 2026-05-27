"""
主分析页面 — 股票分析的核心交互页面（纯 UI 层）。

所有业务逻辑已剥离到 services/analysis_service.py，
本模块仅负责：
  1. 渲染控件
  2. 响应用户交互
  3. 启动后台分析线程
  4. 展示结果
"""

import logging
import threading
from datetime import datetime

import flet as ft

from config.settings import Settings
from data.database import Database
from data.models import StockInfo
from services.analysis_service import AnalysisService, AnalysisRequest, AnalysisResponse
from report.pdf_exporter import export_report_to_pdf
from ui.components import StarRating

logger = logging.getLogger(__name__)


_BTN_START_STYLE = ft.ButtonStyle(
    bgcolor={"": ft.Colors.BLUE_700, "disabled": ft.Colors.GREY_400},
    color={"": ft.Colors.WHITE, "disabled": ft.Colors.WHITE},
    elevation={"": 2, "hovered": 4},
    animation_duration=200,
    padding={"": ft.Padding(left=24, top=14, right=24, bottom=14)},
    shape=ft.RoundedRectangleBorder(radius=8),
)

_BTN_STOP_STYLE = ft.ButtonStyle(
    bgcolor={"": ft.Colors.RED_400, "disabled": ft.Colors.GREY_400},
    color={"": ft.Colors.WHITE, "disabled": ft.Colors.WHITE},
    elevation={"": 2, "hovered": 4},
    animation_duration=200,
    padding={"": ft.Padding(left=24, top=14, right=24, bottom=14)},
    shape=ft.RoundedRectangleBorder(radius=8),
)


class MainPage(ft.Container):

    def __init__(self):
        super().__init__()
        self._service = AnalysisService()
        self._current_report_id = None
        self._chart_path = None
        self._report_content = ""
        self._stock_info = None
        self._backtest_results = None

    # ======================== 构建 UI ========================

    def build(self):
        self._market_dd = ft.Dropdown(
            label="市场", width=100, value="US",
            options=[
                ft.dropdown.Option("US", "美股"),
                ft.dropdown.Option("A", "A 股"),
            ],
        )
        self._code_input = ft.TextField(
            label="股票代码",
            hint_text="输入代码或公司名称（如 600519、茅台、AAPL、英伟达）",
            width=280,
            on_submit=self._on_start,
        )
        self._period_dd = ft.Dropdown(
            label="回测周期", width=140, value="3m",
            options=[
                ft.dropdown.Option("3m", "3 个月"),
                ft.dropdown.Option("6m", "6 个月"),
                ft.dropdown.Option("1y", "1 年"),
                ft.dropdown.Option("3y", "3 年"),
            ],
        )
        self._source_dd = ft.Dropdown(
            label="数据源", width=120, value="free",
            options=[
                ft.dropdown.Option("free", "免费数据"),
                ft.dropdown.Option("custom", "付费数据"),
            ],
        )
        self._start_btn = ft.Button(
            content=ft.Text("开始分析", color=ft.Colors.WHITE),
            icon=ft.Icons.PLAY_ARROW, icon_color=ft.Colors.WHITE,
            style=_BTN_START_STYLE, on_click=self._on_start,
        )
        self._stop_btn = ft.Button(
            content=ft.Text("停止", color=ft.Colors.WHITE),
            icon=ft.Icons.STOP, icon_color=ft.Colors.WHITE,
            style=_BTN_STOP_STYLE, disabled=True, on_click=self._on_stop,
        )
        self._progress_row = ft.Row(
            visible=False, alignment=ft.MainAxisAlignment.CENTER, spacing=12,
            controls=[
                ft.ProgressRing(width=20, height=20, stroke_width=3, color=ft.Colors.BLUE_700),
                ft.Text("", size=14, color=ft.Colors.BLUE_700),
            ],
        )
        self._progress_text = self._progress_row.controls[1]
        self._error_text = ft.Text("", color=ft.Colors.RED, size=14)
        self._export_btn = ft.Button(
            content=ft.Text("导出 PDF"), icon=ft.Icons.PICTURE_AS_PDF,
            on_click=self._on_export_pdf, visible=False,
        )
        self._star_rating = StarRating(
            max_stars=5, initial_rating=0, on_change=self._on_rating_change,
        )
        self._star_rating.visible = False
        self._report_view = ft.Markdown(
            value="", selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_theme="monokai-sublime", visible=False,
        )
        self._chart_image = ft.Image(src="", visible=False, fit="contain", width=700)

        chart_container = ft.Container(
            visible=False,
            content=ft.Column([
                ft.Text("K 线图", size=16, weight=ft.FontWeight.BOLD),
                self._chart_image,
            ]),
        )
        report_container = ft.Container(
            visible=False,
            content=ft.Column([
                ft.Divider(),
                ft.Text("分析报告", size=16, weight=ft.FontWeight.BOLD),
                self._report_view,
                ft.Divider(),
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Row(spacing=16, controls=[
                            self._export_btn, self._star_rating,
                            ft.Text("报告评分", size=13, color=ft.Colors.GREY_600),
                        ]),
                    ],
                ),
            ]),
        )
        self._chart_container = chart_container
        self._report_container = report_container

        self.content = ft.Column(
            scroll=ft.ScrollMode.AUTO, expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    margin=ft.Margin(top=20, right=0, bottom=0, left=0),
                    content=ft.ResponsiveRow(
                        alignment=ft.MainAxisAlignment.CENTER, spacing=16,
                        controls=[
                            ft.Column(col={"sm": 6, "md": 2}, controls=[self._market_dd]),
                            ft.Column(col={"sm": 6, "md": 3}, controls=[self._code_input]),
                            ft.Column(col={"sm": 6, "md": 2}, controls=[self._period_dd]),
                            ft.Column(col={"sm": 6, "md": 2}, controls=[self._source_dd]),
                            ft.Column(col={"sm": 12, "md": 3}, controls=[
                                ft.Row(spacing=8, controls=[self._start_btn, self._stop_btn]),
                            ]),
                        ],
                    ),
                ),
                self._progress_row, self._error_text,
                chart_container, report_container,
            ],
        )
        return self.content

    # ======================== 按钮状态 ========================

    def _set_running(self):
        self._start_btn.disabled = True
        self._stop_btn.disabled = False
        self._start_btn.update(); self._stop_btn.update()

    def _set_idle(self):
        self._start_btn.disabled = False
        self._stop_btn.disabled = True
        self._start_btn.update(); self._stop_btn.update()

    # ======================== 事件处理 ========================

    def _on_start(self, e):
        raw = self._code_input.value.strip()
        if not raw:
            self._show_error("请输入股票代码或名称")
            return
        if not Settings().get("work_dir", ""):
            self._show_error("请先在设置中配置工作目录")
            return

        self._show_error("")
        self._reset_ui()
        self._set_running()
        self._progress_row.visible = True
        self._progress_text.value = "正在准备..."
        self._progress_row.update()
        self.page.update()

        thread = threading.Thread(
            target=self._run_analysis,
            args=(raw, self._market_dd.value, self._period_dd.value, self._source_dd.value),
            daemon=True,
        )
        thread.start()

    def _on_stop(self, e):
        self._service.cancel()
        self._show_error("正在停止...")

    def _reset_ui(self):
        self._current_report_id = None
        self._chart_path = None
        self._report_content = ""
        self._stock_info = None
        self._backtest_results = None
        self._report_view.value = ""
        self._report_view.visible = False
        self._chart_container.visible = False
        self._report_container.visible = False
        self._chart_image.visible = False
        self._chart_image.src = ""
        self._export_btn.visible = False
        self._star_rating.visible = False
        self._star_rating.rating = 0

    def _show_error(self, msg: str):
        self._error_text.value = msg
        self._error_text.update()

    # ======================== 后台分析线程 ========================

    def _run_analysis(self, raw: str, market: str, period: str, data_source: str):
        """后台线程：调用 Service 执行分析。"""
        request = AnalysisRequest(raw_input=raw, market=market, period=period,
                                  data_source=data_source)
        self._service = AnalysisService()  # 新实例，重置取消状态

        def on_progress(msg: str):
            try:
                self.page.run_task(self._update_progress_async, msg)
            except Exception:
                pass

        try:
            response = self._service.analyze(request, on_progress=on_progress)
            self._on_analysis_done(response)
        except ValueError as e:
            self._show_result_error(str(e))
        except RuntimeError as e:
            self._show_result_error(str(e))
        except Exception as e:
            logger.exception(f"Analysis failed")
            self._show_result_error(f"分析出错：{e}")
        finally:
            try:
                self.page.run_task(self._done_async)
            except Exception:
                pass

    def _on_analysis_done(self, response: AnalysisResponse):
        """分析完成 → 更新 UI 状态。"""
        self._chart_path = response.chart_path
        self._report_content = response.report_content
        self._current_report_id = response.report_id
        self._stock_info = response.stock_info
        self._backtest_results = response.backtest_results
        self._show_results()

    # ======================== UI 更新（线程安全） ========================

    async def _update_progress_async(self, text: str):
        self._progress_text.value = text
        self._progress_row.update()

    async def _done_async(self):
        self._progress_row.visible = False
        self._progress_row.update()
        self._set_idle()
        self.page.update()

    def _show_result_error(self, msg: str):
        logger.error(msg)
        try:
            self.page.run_task(self._show_result_error_async, msg)
        except Exception:
            pass

    async def _show_result_error_async(self, msg: str):
        self._show_error(msg)
        self._progress_row.visible = False
        self._progress_row.update()
        self._set_idle()
        self.page.update()

    def _show_results(self):
        try:
            self.page.run_task(self._show_results_async)
        except Exception:
            pass

    async def _show_results_async(self):
        if not self._report_content:
            return
        self._report_view.value = self._report_content
        self._report_view.visible = True
        self._report_view.update()
        if self._chart_path:
            self._chart_image.src = self._chart_path
            self._chart_image.visible = True
            self._chart_image.update()
            self._chart_container.visible = True
            self._chart_container.update()
        self._report_container.visible = True
        self._report_container.update()
        self._export_btn.visible = True
        self._export_btn.update()
        self._star_rating.rating = 0
        self._star_rating.visible = True
        self._star_rating.update()
        self.page.update()

    # ======================== PDF 导出 / 评分 ========================

    def _on_export_pdf(self, e):
        if not self._report_content:
            self._show_error("没有可导出的报告")
            return
        si = self._stock_info
        pdf_path = export_report_to_pdf(
            self._report_content, self._chart_path or "",
            si.name if si else "", si.code if si else "",
            self._period_dd.value,
        )
        if pdf_path:
            if self._current_report_id:
                Database().update_report_pdf(self._current_report_id, pdf_path)
            self.page.snack_bar = ft.SnackBar(
                ft.Text(f"PDF 已导出：{pdf_path}"), bgcolor=ft.Colors.GREEN_700,
            )
            self.page.snack_bar.open = True
            self.page.update()
        else:
            self._show_error("PDF 导出失败")

    def _on_rating_change(self, rating: int):
        if self._current_report_id and rating > 0:
            Database().update_report_rating(self._current_report_id, rating)
            self.page.snack_bar = ft.SnackBar(
                ft.Text(f"已评分：{rating} 星"), bgcolor=ft.Colors.BLUE_700,
            )
            self.page.snack_bar.open = True
            self.page.update()
