"""
主分析页面

股票分析的核心交互页面，提供以下功能：
  1. 股票代码输入（自动识别 A 股/美股格式）
  2. 回测周期选择（3 个月 / 6 个月 / 1 年 / 3 年）
  3. 一键分析流程：数据获取 → 指标计算 → 新闻情感 → 回测 → 报告生成
  4. 分析结果展示：K 线图 + Markdown 报告
  5. 报告导出为 PDF
  6. 报告评分（1-5 星）

分析流程在后台线程中执行（避免阻塞 UI），通过 ProgressOverlay
显示实时进度。支持 LLM 增强报告和无 LLM 的回退报告两种模式。

【扩展点】添加新的分析维度：
  1. 在 _run_analysis() 中添加新的分析步骤
  2. 在 _update_progress() 中添加对应的进度提示
  3. 将新模块的结果传入 generate_report() 的 user_prompt
  4. 在 UI 中可能需要新增展示区域
"""

import logging
import threading
from datetime import datetime

import flet as ft

from config.settings import Settings
from data.database import Database
from data.stock_fetcher import get_stock_fetcher
from data.news_fetcher import fetch_news
from data.models import PriceData, StockInfo, AnalysisReport
from analysis.technical import calc_all_indicators, summarize
from analysis.sentiment import analyze, aggregate
from analysis.strategy import get_strategy
from analysis.backtest import BacktestEngine
from report.chart import generate_kline_chart
from report.generator import generate_report
from report.pdf_exporter import export_report_to_pdf
from ui.components import StarRating, ProgressOverlay
from utils.helpers import is_valid_stock_code, detect_market, get_backtest_dates

logger = logging.getLogger(__name__)


class MainPage(ft.Container):
    """
    主分析页面。

    使用 Stack 布局叠加进度遮罩和主内容区域。
    分析操作在后台线程中执行，通过 Flet 的 update() 机制刷新 UI。

    关键属性：
      _current_report_id: 当前报告数据库 ID（用于评分和 PDF 更新）
      _chart_path:        K 线图文件路径
      _report_content:    报告 Markdown 文本
    """

    def __init__(self):
        super().__init__()
        self._analysis_result = None     # 缓存的分析结果（用于 PDF 导出）
        self._current_report_id = None   # 当前报告数据库 ID
        self._chart_path = None          # K 线图路径
        self._report_content = ""        # 报告内容

    def build(self):
        """构建主页面控件树。"""
        # --- 股票代码输入 ---
        self._code_input = ft.TextField(
            label="股票代码",
            hint_text="A股输入6位数字，美股输入字母代码（如 AAPL）",
            width=200,
            on_submit=self._start_analysis,  # 回车触发分析
        )

        # --- 回测周期选择 ---
        self._period_dd = ft.Dropdown(
            label="回测周期",
            width=150,
            value="3m",
            options=[
                ft.dropdown.Option("3m", "3 个月"),
                ft.dropdown.Option("6m", "6 个月"),
                ft.dropdown.Option("1y", "1 年"),
                ft.dropdown.Option("3y", "3 年"),
            ],
        )

        # --- 策略选择 ---
        self._strategy_dd = ft.Dropdown(
            label="交易策略",
            width=200,
            value="ma_crossover",
            options=[
                ft.dropdown.Option("ma_crossover", "双均线交叉"),
                ft.dropdown.Option("macd", "MACD 金叉死叉"),
                ft.dropdown.Option("rsi", "RSI 超买超卖"),
                ft.dropdown.Option("bollinger", "布林带均值回归"),
                ft.dropdown.Option("buy_and_hold", "买入持有（基准）"),
                ft.dropdown.Option("triple_ma", "三均线排列"),
            ],
        )

        # --- 分析按钮 ---
        self._analyze_btn = ft.ElevatedButton(
            "开始分析",
            icon=ft.Icons.ANALYTICS,
            on_click=self._start_analysis,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE_700,
                color=ft.Colors.WHITE,
            ),
        )

        # --- 进度遮罩 ---
        self._progress = ProgressOverlay()

        # --- 导出 PDF 按钮（初始隐藏，分析完成后显示） ---
        self._export_btn = ft.ElevatedButton(
            "导出 PDF",
            icon=ft.Icons.PICTURE_AS_PDF,
            on_click=self._export_pdf,
            visible=False,
        )

        # --- 星级评分（初始隐藏） ---
        self._star_rating = StarRating(
            max_stars=5,
            initial_rating=0,
            on_change=self._on_rating_change,
        )
        self._star_rating.visible = False

        # --- 报告展示区（Markdown 渲染） ---
        self._report_view = ft.Markdown(
            value="",
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_style=ft.TextStyle(font_family="monospace"),
            visible=False,
        )

        # --- K 线图展示 ---
        self._chart_image = ft.Image(
            visible=False,
            fit=ft.ImageFit.CONTAIN,
            width=700,
        )

        # --- 错误提示 ---
        self._error_text = ft.Text("", color=ft.Colors.RED, size=14)

        # --- 图表容器 ---
        chart_container = ft.Container(
            visible=False,
            content=ft.Column([
                ft.Text("K线图", size=16, weight=ft.FontWeight.BOLD),
                self._chart_image,
            ]),
        )

        # --- 报告容器（含评分和导出按钮） ---
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
                        ft.Row(
                            spacing=16,
                            controls=[
                                self._export_btn,
                                self._star_rating,
                                ft.Text("报告评分", size=13, color=ft.Colors.GREY_600),
                            ],
                        ),
                    ],
                ),
            ]),
        )

        self._chart_container = chart_container
        self._report_container = report_container

        # --- 整体布局：Stack(主内容 + 进度遮罩) ---
        return ft.Stack(
            expand=True,
            controls=[
                # 主内容区（可滚动）
                ft.Column(
                    scroll=ft.ScrollMode.AUTO,
                    expand=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        # 输入区域（响应式布局：桌面三列，小屏单列）
                        ft.Container(
                            margin=ft.margin.only(top=20),
                            content=ft.ResponsiveRow(
                                alignment=ft.MainAxisAlignment.CENTER,
                                spacing=20,
                                controls=[
                                    ft.Column(col={"sm": 12, "md": 3},
                                              controls=[self._code_input]),
                                    ft.Column(col={"sm": 12, "md": 2},
                                              controls=[self._period_dd]),
                                    ft.Column(col={"sm": 12, "md": 3},
                                              controls=[self._strategy_dd]),
                                    ft.Column(col={"sm": 12, "md": 2},
                                              controls=[self._analyze_btn]),
                                ],
                            ),
                        ),
                        self._error_text,
                        chart_container,
                        report_container,
                    ],
                ),
                # 进度遮罩（覆盖在上面）
                self._progress,
            ],
        )

    # ======================== 分析流程 ========================

    def _start_analysis(self, e):
        """
        用户点击"开始分析"按钮。

        流程：
          1. 校验输入（代码格式、工作目录配置）
          2. 清空上次结果
          3. 启动后台分析线程（避免阻塞 UI）
        """
        code = self._code_input.value.strip().upper()
        if not code:
            self._show_error("请输入股票代码")
            return

        valid, market = is_valid_stock_code(code)
        if not valid:
            self._show_error("无效的股票代码格式")
            return

        settings = Settings()
        if not settings.get("work_dir", ""):
            self._show_error("请先在设置中配置工作目录")
            return

        self._clear_results()
        self._show_error("")

        # 显示进度遮罩，禁用按钮防重复点击
        self._progress.visible = True
        self._analyze_btn.disabled = True
        self.update()

        # 后台线程执行分析（daemon=True 确保应用退出时线程自动终止）
        thread = threading.Thread(
            target=self._run_analysis, args=(code, market), daemon=True
        )
        thread.start()

    def _run_analysis(self, code: str, market: str):
        """
        后台分析主流程（在独立线程中执行）。

        步骤：
          1. 获取股票基本信息
          2. 获取历史 K 线数据
          3. 计算技术指标
          4. 获取并分析新闻情感
          5. 运行回测
          6. 生成 K 线图
          7. 生成分析报告（LLM 或回退）
          8. 保存报告到数据库
          9. 展示结果

        【扩展点】在此方法中添加新的分析步骤。
        """
        try:
            # ---- 第 1 步：获取股票信息 ----
            self._update_progress("正在获取股票信息...")
            fetcher = get_stock_fetcher()
            stock_info = fetcher.fetch_stock_info(code)
            if stock_info:
                Database().upsert_stock(stock_info)  # 缓存到数据库
            else:
                # 即使 API 失败也创建一个基本记录
                stock_info = StockInfo(code=code, name=code, market=market)

            # ---- 第 2 步：获取股价数据 ----
            period = self._period_dd.value
            start_date, end_date = get_backtest_dates(period)

            self._update_progress("正在获取股价数据...")
            prices = fetcher.fetch_price_history(code, start_date, end_date)
            if prices:
                Database().insert_prices(prices)  # 缓存到数据库
            else:
                # API 失败时尝试从数据库读取已有数据
                prices = Database().get_prices(code, start_date, end_date)

            if not prices:
                self._show_result_error("无法获取该股票的股价数据，请检查代码是否正确。")
                return

            # 转换为 DataFrame（分析引擎的标准输入格式）
            import pandas as pd
            df = pd.DataFrame([p.to_dict() for p in prices])
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")

            # ---- 第 3 步：技术指标计算 ----
            self._update_progress("正在计算技术指标...")
            df = calc_all_indicators(df)

            # ---- 第 4 步：新闻情感分析 ----
            news_list = []
            try:
                self._update_progress("正在获取新闻...")
                news_list = fetch_news(code, market)
            except Exception as e:
                logger.warning(f"News fetch failed: {e}")

            self._update_progress("正在进行新闻情感分析...")
            news_list = analyze(news_list)       # FinBERT 推理
            news_aggregation = aggregate(news_list)  # 汇总统计

            # ---- 第 5 步：回测 ----
            self._update_progress("正在运行回测...")
            strategy = get_strategy(self._strategy_dd.value)
            backtest_df = df.copy()
            backtest_df = strategy.generate_signals(backtest_df)
            engine = BacktestEngine()
            backtest_result = engine.run(backtest_df, strategy)

            # ---- 第 6 步：生成 K 线图 ----
            self._update_progress("正在生成K线图...")
            chart_df = df.copy()
            chart_df["signal"] = backtest_df.get("signal", "")
            self._chart_path = generate_kline_chart(chart_df, code, stock_info.name)

            # ---- 第 7 步：生成报告 ----
            self._update_progress("正在生成分析报告...")
            tech_summary = summarize(df, stock_info.name)
            stock_dict = stock_info.to_dict()
            self._report_content = generate_report(
                stock_dict, tech_summary, news_aggregation, backtest_result,
                self._chart_path or "",
            )

            # ---- 第 8 步：保存到数据库 ----
            self._update_progress("正在保存报告...")
            report = AnalysisReport(
                code=code,
                name=stock_info.name,
                market=market,
                backtest_period=period,
                create_time=datetime.now().isoformat(),
                content=self._report_content,
            )
            self._current_report_id = Database().insert_report(report)

            # 缓存结果供 PDF 导出使用
            self._analysis_result = {
                "stock": stock_dict,
                "backtest": backtest_result,
            }

            # ---- 第 9 步：展示结果 ----
            self._show_results()

        except Exception as ex:
            logger.exception(f"Analysis failed for {code}")
            self._show_result_error(f"分析过程中出错：{str(ex)}")
        finally:
            # 恢复按钮状态，隐藏进度遮罩
            self._progress.visible = False
            self._analyze_btn.disabled = False
            self.update()

    # ======================== UI 更新方法 ========================

    def _show_results(self):
        """分析完成后展示结果（K 线图 + 报告 + 导出/评分按钮）。"""
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
        else:
            self._chart_container.visible = False
            self._chart_container.update()

        self._report_container.visible = True
        self._report_container.update()

        self._export_btn.visible = True
        self._export_btn.update()

        self._star_rating.rating = 0
        self._star_rating.visible = True
        self._star_rating.update()

    def _clear_results(self):
        """清空上次分析结果（新分析开始前调用）。"""
        self._analysis_result = None
        self._current_report_id = None
        self._chart_path = None
        self._report_content = ""
        self._report_view.value = ""
        self._report_view.visible = False
        self._report_container.visible = False
        self._chart_container.visible = False
        self._chart_image.visible = False
        self._chart_image.src = ""
        self._export_btn.visible = False
        self._star_rating.visible = False
        self._star_rating.rating = 0

    def _show_error(self, msg: str):
        """显示错误提示文字。"""
        self._error_text.value = msg
        self._error_text.update()

    def _show_result_error(self, msg: str):
        """显示分析过程中的错误（恢复 UI 状态）。"""
        self._show_error(msg)
        self._progress.visible = False
        self._analyze_btn.disabled = False
        self.update()

    def _update_progress(self, text: str):
        """更新进度遮罩上的状态文字（线程安全）。"""
        try:
            self._progress.set_status(text)
        except Exception:
            pass  # 忽略 UI 更新异常（如控件已被销毁）

    # ======================== PDF 导出 ========================

    def _export_pdf(self, e):
        """导出当前报告为 PDF 文件。"""
        if not self._report_content:
            self._show_error("没有可导出的报告")
            return

        stock_info = self._analysis_result.get("stock", {}) if self._analysis_result else {}
        pdf_path = export_report_to_pdf(
            self._report_content,
            self._chart_path or "",
            stock_info.get("name", ""),
            stock_info.get("code", ""),
            self._period_dd.value,
        )
        if pdf_path:
            # 更新数据库中的 PDF 路径
            if self._current_report_id:
                Database().update_report_pdf(self._current_report_id, pdf_path)
            self._show_error("")
            # SnackBar 提示
            self.page.snack_bar = ft.SnackBar(
                ft.Text(f"PDF 已导出：{pdf_path}"),
                bgcolor=ft.Colors.GREEN_700,
            )
            self.page.snack_bar.open = True
            self.page.update()
        else:
            self._show_error("PDF 导出失败，请查看日志")

    # ======================== 报告评分 ========================

    def _on_rating_change(self, rating: int):
        """
        用户评分后的回调。

        评分数据存入 reports 表的 rating 字段，
        后续可用于分析用户偏好、优化策略参数。
        """
        if self._current_report_id and rating > 0:
            Database().update_report_rating(self._current_report_id, rating)
            self.page.snack_bar = ft.SnackBar(
                ft.Text(f"已评分：{rating} 星"),
                bgcolor=ft.Colors.BLUE_700,
            )
            self.page.snack_bar.open = True
            self.page.update()
