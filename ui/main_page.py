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
from ui.components import StarRating

logger = logging.getLogger(__name__)


class MainPage(ft.Container):
    """主分析页面 — 股票输入、参数选择、分析触发、结果展示。"""

    def __init__(self):
        super().__init__()
        self._service = AnalysisService()
        self._current_report_id = None
        self._chart_path = None
        self._report_content = ""
        self._stock_info = None
        self._backtest_results = None

    # ======================== 按钮样式 ========================

    _PANEL_STYLE = {
        "bgcolor": ft.Colors.WHITE,
        "border_radius": 12,
        "padding": ft.Padding(28, 24, 28, 24),
        "shadow": ft.BoxShadow(
            spread_radius=0, blur_radius=12,
            color=ft.Colors.with_opacity(0.08, ft.Colors.BLACK),
            offset=ft.Offset(0, 2),
        ),
    }

    _SECTION_TITLE_STYLE = {
        "size": 13,
        "color": ft.Colors.GREY_600,
        "weight": ft.FontWeight.W_500,
    }

    # ======================== 构建 UI ========================

    def build(self):
        # ── 市场选择 ──
        self._market_dd = ft.Dropdown(
            label="市场", width=110, value="US",
            options=[
                ft.dropdown.Option("US", "美股"),
                ft.dropdown.Option("A", "A 股"),
            ],
            border_radius=8,
        )

        # ── 股票代码（核心输入，最宽） ──
        self._code_input = ft.TextField(
            label="股票代码或名称",
            hint_text="如 600519、茅台、AAPL、英伟达",
            width=340,
            border_radius=8,
            on_submit=self._on_start,
            prefix_icon=ft.Icons.SEARCH,
        )

        # ── 回测周期 ──
        self._period_dd = ft.Dropdown(
            label="回测周期", width=130, value="1y",
            options=[
                ft.dropdown.Option("6m", "6 个月"),
                ft.dropdown.Option("1y", "1 年"),
                ft.dropdown.Option("3y", "3 年"),
            ],
            border_radius=8,
        )

        # ── 分析模式（盘后/盘中/盘前） ──
        has_token = bool(Settings().get("stock_token_us", ""))
        self._mode_dd = ft.Dropdown(
            label="分析模式", width=140, value="eod",
            options=[
                ft.dropdown.Option("eod", "📊 盘后分析"),
                ft.dropdown.Option("intraday", "⏱ 盘中分析", disabled=not has_token),
                ft.dropdown.Option("pre", "🌅 盘前分析", disabled=not has_token),
            ],
            border_radius=8,
        )
        self._mode_dd.on_change = self._on_mode_change

        # ── 按钮 ──
        self._start_btn = ft.ElevatedButton(
            content=ft.Row(spacing=8, controls=[
                ft.Icon(ft.Icons.PLAY_ARROW, size=18, color=ft.Colors.WHITE),
                ft.Text("开始分析", size=15, color=ft.Colors.WHITE, weight=ft.FontWeight.W_600),
            ]),
            style=ft.ButtonStyle(
                bgcolor={"": ft.Colors.BLUE_700, "hovered": ft.Colors.BLUE_900, "disabled": ft.Colors.GREY_400},
                color={"": ft.Colors.WHITE, "hovered": ft.Colors.WHITE, "disabled": ft.Colors.WHITE},
                elevation={"": 3, "hovered": 6, "disabled": 0},
                animation_duration=200,
                shape=ft.RoundedRectangleBorder(radius=10),
                padding=ft.Padding(28, 16, 28, 16),
            ),
            on_click=self._on_start,
        )
        self._stop_btn = ft.ElevatedButton(
            content=ft.Row(spacing=8, controls=[
                ft.Icon(ft.Icons.STOP, size=18, color=ft.Colors.WHITE),
                ft.Text("停止分析", size=15, color=ft.Colors.WHITE, weight=ft.FontWeight.W_600),
            ]),
            style=ft.ButtonStyle(
                bgcolor={"": ft.Colors.RED_500, "hovered": ft.Colors.RED_700, "disabled": ft.Colors.GREY_400},
                color={"": ft.Colors.WHITE, "hovered": ft.Colors.WHITE, "disabled": ft.Colors.WHITE},
                elevation={"": 3, "hovered": 6, "disabled": 0},
                animation_duration=200,
                shape=ft.RoundedRectangleBorder(radius=10),
                padding=ft.Padding(28, 16, 28, 16),
            ),
            disabled=True,
            on_click=self._on_stop,
        )

        # ── 进度 ──
        self._progress_row = ft.Row(
            visible=False, alignment=ft.MainAxisAlignment.CENTER, spacing=12,
            controls=[
                ft.ProgressRing(width=18, height=18, stroke_width=3, color=ft.Colors.BLUE_700),
                ft.Text("", size=13, color=ft.Colors.BLUE_700),
            ],
        )
        self._progress_text = self._progress_row.controls[1]
        self._error_text = ft.Text("", color=ft.Colors.RED, size=13)

        # ── 控制面板（白色卡片） ──
        control_panel = ft.Container(
            **self._PANEL_STYLE,
            content=ft.Column(spacing=16, controls=[
                # 标题行
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text("分析设置", size=18, weight=ft.FontWeight.BOLD),
                        ft.Row(spacing=8, controls=[self._start_btn, self._stop_btn]),
                    ],
                ),
                ft.Divider(height=1, color=ft.Colors.GREY_200),

                # 第一行：市场 + 周期 + 分析模式
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    spacing=16,
                    controls=[
                        self._market_dd,
                        self._period_dd,
                        self._mode_dd,
                    ],
                ),

                # 第二行：搜索框（独占全宽）
                self._code_input,
            ]),
        )

        # ── 报告区控件（必须在 report_container 之前创建） ──
        self._export_btn = ft.ElevatedButton(
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.PICTURE_AS_PDF, size=16),
                ft.Text("导出为文件"),
            ]),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=8),
                padding=ft.Padding(16, 10, 16, 10),
            ),
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

        # ── K 线图 + 报告容器 ──
        self._chart_image = ft.Image(src="", visible=False, fit="contain", width=700)
        chart_container = ft.Container(
            visible=False,
            margin=ft.Margin(0, 16, 0, 0),
            **self._PANEL_STYLE,
            content=ft.Column([
                ft.Text("K 线图", size=16, weight=ft.FontWeight.BOLD),
                self._chart_image,
            ]),
        )
        report_container = ft.Container(
            visible=False,
            margin=ft.Margin(0, 16, 0, 0),
            **self._PANEL_STYLE,
            content=ft.Column([
                ft.Text("分析报告", size=16, weight=ft.FontWeight.BOLD),
                ft.Divider(height=1, color=ft.Colors.GREY_200),
                self._report_view,
                ft.Divider(height=1, color=ft.Colors.GREY_200),
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        self._export_btn,
                        ft.Row(spacing=8, controls=[
                            self._star_rating,
                            ft.Text("评分", size=13, color=ft.Colors.GREY_600),
                        ]),
                    ],
                ),
            ]),
        )
        self._chart_container = chart_container
        self._report_container = report_container

        # ── 整体布局 ──
        self.content = ft.Column(
            scroll=ft.ScrollMode.AUTO, expand=True,
            spacing=0,
            controls=[
                control_panel,
                self._progress_row,
                self._error_text,
                chart_container,
                report_container,
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

    def refresh_modes(self):
        """刷新分析模式下拉框（设置保存后调用，重新检查 token）。"""
        has_token = bool(Settings().get("stock_token_us", ""))
        self._mode_dd.options = [
            ft.dropdown.Option("eod", "📊 盘后分析"),
            ft.dropdown.Option("intraday", "⏱ 盘中分析", disabled=not has_token),
            ft.dropdown.Option("pre", "🌅 盘前分析", disabled=not has_token),
        ]
        self._mode_dd.update()

    def _on_mode_change(self, e):
        """分析模式切换时，自动调整市场选择。"""
        mode = self._mode_dd.value
        if mode == "pre":
            self._market_dd.value = "US"
            self._market_dd.disabled = True
            self._market_dd.update()
            self._show_error("💡 盘前分析目前仅支持美股。已自动切换为美股市场。")
        else:
            self._market_dd.disabled = False
            self._market_dd.update()

    def _on_start(self, e):
        raw = self._code_input.value.strip()
        if not raw:
            self._show_error("请输入股票代码或名称")
            return

        settings = Settings()
        if not settings.is_fully_configured():
            missing = settings.missing_fields()
            labels = [Settings.FIELD_LABELS.get(f, f) for f in missing]
            self._show_error(f"请先在「设置」中配置：{'、'.join(labels)}")
            return

        # 根据市场检查数据源 token
        market = self._market_dd.value
        mode = self._mode_dd.value
        if market == "US":
            if not settings.get("stock_token_us"):
                self._show_error("美股分析需要配置「美股数据源 Token」")
                return
            if not settings.get("news_token_us"):
                self._show_error("美股分析需要配置「新闻数据源 Token - 美股」")
                return
        elif market == "A":
            if not settings.get("stock_token_a"):
                self._show_error("A 股分析需要配置「A 股数据源 Token」")
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
            args=(raw, self._market_dd.value, self._period_dd.value, mode),
            daemon=True,
        )
        thread.start()

    def _on_stop(self, e):
        self._service.cancel()
        self._show_error("正在停止...")

    def _reset_ui(self):
        """清空上一次分析结果的全部展示控件。"""
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

    @staticmethod
    def _mode_name(mode: str) -> str:
        return {"eod": "盘后分析", "intraday": "盘中分析", "pre": "盘前分析"}.get(mode, mode)

    def _run_analysis(self, raw: str, market: str, period: str, mode: str = "eod"):
        """后台线程：调用 Service 执行分析。"""
        request = AnalysisRequest(raw_input=raw, market=market, period=period, mode=mode)
        self._service = AnalysisService()  # 新实例，重置取消状态

        def on_progress(msg: str):
            try:
                self.page.run_task(self._update_progress_async, msg)
            except Exception:
                pass

        try:
            if mode == "intraday":
                response = self._service.analyze_intraday(request, on_progress=on_progress)
            elif mode == "pre":
                response = self._service.analyze_premarket(request, on_progress=on_progress)
            else:
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
        try:
            path = self._export_html()
            if path and self._current_report_id:
                Database().update_report_pdf(self._current_report_id, path)
            if path:
                self._show_export_dialog(path)
            else:
                self._show_error("导出失败")
        except Exception as ex:
            logger.exception("导出失败")
            self._show_error(f"导出失败：{ex}")

    def _export_html(self) -> str:
        """将 Markdown 报告导出为 HTML 文件。内嵌 K 线图为 base64。"""
        import os, base64
        from config.settings import Settings

        html_dir = Settings().pdf_dir
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        si = self._stock_info
        code = si.code if si else "report"
        filename = f"report_{code}_{timestamp}.html"
        filepath = os.path.join(html_dir, filename)

        # K 线图 → base64 内嵌
        chart_html = ""
        if self._chart_path and os.path.exists(self._chart_path):
            with open(self._chart_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            chart_html = f'<p align="center"><img src="data:image/png;base64,{b64}" style="max-width:100%;"></p>'

        # Markdown → HTML
        from markdown_it import MarkdownIt
        md = MarkdownIt().enable(["table", "strikethrough"])
        body_html = md.render(self._report_content)

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{si.name if si else code} 分析报告</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #333; line-height: 1.8; }}
  h1 {{ border-bottom: 2px solid #2a6496; padding-bottom: 8px; }}
  h2 {{ color: #2a6496; margin-top: 32px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #2a6496; color: #fff; }}
  tr:nth-child(even) {{ background: #f5f7fa; }}
  blockquote {{ border-left: 3px solid #2a6496; padding-left: 16px; color: #666; margin: 12px 0; }}
  code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  pre {{ background: #f5f5f5; padding: 16px; border-radius: 6px; overflow-x: auto; }}
  img {{ max-width: 100%; }}
</style>
</head>
<body>
{chart_html}
{body_html}
</body>
</html>"""

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"HTML 报告已导出: {filepath}")
        return filepath

    def _show_export_dialog(self, filepath: str):
        """导出成功：SnackBar 提示，5 秒自动消失。"""
        import os, subprocess, sys, threading

        folder = os.path.dirname(filepath)

        def on_action(e):
            if sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            elif sys.platform == "win32":
                os.startfile(folder)
            else:
                subprocess.Popen(["xdg-open", folder])

        # 清理旧的 snackbar / dialog 状态
        self.page.dialog = None
        self.page.snack_bar = ft.SnackBar(
            content=ft.Text(f"已导出至：{os.path.basename(filepath)}"),
            bgcolor=ft.Colors.GREEN_700,
            action="打开目录",
            on_action=on_action,
            duration=5000,
        )
        self.page.snack_bar.open = True
        self.page.update()

        # 后台打开浏览器
        def _open():
            import webbrowser
            webbrowser.open(f"file://{filepath}")
        threading.Thread(target=_open, daemon=True).start()

    def _on_rating_change(self, rating: int):
        if self._current_report_id and rating > 0:
            Database().update_report_rating(self._current_report_id, rating)
            self.page.snack_bar = ft.SnackBar(
                ft.Text(f"已评分：{rating} 星"), bgcolor=ft.Colors.BLUE_700,
            )
            self.page.snack_bar.open = True
            self.page.update()
