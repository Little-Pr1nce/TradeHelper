"""
主分析页面 — 股票分析的核心交互页面。

功能：
  1. 选择市场（A 股 / 美股）
  2. 输入股票代码或名称，自动搜索匹配
  3. 选择回测周期和交易策略
  4. 开始 / 停止分析（按钮互斥 + 置灰）
  5. 展示 K 线图 + Markdown 报告 + 导出 PDF + 评分
"""

import logging
import threading
from datetime import datetime

import flet as ft

from config.settings import Settings
from data.database import Database
from data.stock_fetcher import get_stock_fetcher
from data.news_fetcher import fetch_news
from data.models import StockInfo, AnalysisReport
from analysis.technical import calc_all_indicators, summarize
from analysis.sentiment import analyze, aggregate
from analysis.strategy import get_strategy
from analysis.backtest import BacktestEngine
from report.chart import generate_kline_chart
from report.generator import generate_report
from report.pdf_exporter import export_report_to_pdf
from ui.components import StarRating
from utils.helpers import get_backtest_dates

logger = logging.getLogger(__name__)


# ======================== 按钮样式 ========================

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
        self._analysis_result = None
        self._current_report_id = None
        self._chart_path = None
        self._report_content = ""
        self._stop_flag = False

    # ======================== 构建 UI ========================

    def build(self):
        # --- 市场 ---
        self._market_dd = ft.Dropdown(
            label="市场", width=100, value="US",
            options=[
                ft.dropdown.Option("US", "美股"),
                ft.dropdown.Option("A", "A 股"),
            ],
        )

        # --- 代码/名称 ---
        self._code_input = ft.TextField(
            label="股票代码",
            hint_text="代码或名称（如 600519、茅台、AAPL、英伟达）",
            width=250,
            on_submit=self._start_analysis,
        )

        # --- 回测周期 ---
        self._period_dd = ft.Dropdown(
            label="回测周期", width=140, value="3m",
            options=[
                ft.dropdown.Option("3m", "3 个月"),
                ft.dropdown.Option("6m", "6 个月"),
                ft.dropdown.Option("1y", "1 年"),
                ft.dropdown.Option("3y", "3 年"),
            ],
        )

        # --- 策略 ---
        self._strategy_dd = ft.Dropdown(
            label="交易策略", width=200, value="ma_crossover",
            options=[
                ft.dropdown.Option("ma_crossover", "双均线交叉"),
                ft.dropdown.Option("macd", "MACD 金叉死叉"),
                ft.dropdown.Option("rsi", "RSI 超买超卖"),
                ft.dropdown.Option("bollinger", "布林带均值回归"),
                ft.dropdown.Option("buy_and_hold", "买入持有（基准）"),
                ft.dropdown.Option("triple_ma", "三均线排列"),
            ],
        )

        # --- 开始 / 停止 ---
        self._start_btn = ft.Button(
            content=ft.Text("开始分析", color=ft.Colors.WHITE),
            icon=ft.Icons.PLAY_ARROW,
            icon_color=ft.Colors.WHITE,
            style=_BTN_START_STYLE,
            on_click=self._start_analysis,
        )
        self._stop_btn = ft.Button(
            content=ft.Text("停止", color=ft.Colors.WHITE),
            icon=ft.Icons.STOP,
            icon_color=ft.Colors.WHITE,
            style=_BTN_STOP_STYLE,
            disabled=True,
            on_click=self._stop_analysis,
        )

        # --- 进度 ---
        self._progress_row = ft.Row(
            visible=False,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=12,
            controls=[
                ft.ProgressRing(width=20, height=20, stroke_width=3, color=ft.Colors.BLUE_700),
                ft.Text("", size=14, color=ft.Colors.BLUE_700),
            ],
        )
        self._progress_text = self._progress_row.controls[1]

        # --- 错误 ---
        self._error_text = ft.Text("", color=ft.Colors.RED, size=14)

        # --- 导出 & 评分 ---
        self._export_btn = ft.Button(
            content=ft.Text("导出 PDF"),
            icon=ft.Icons.PICTURE_AS_PDF,
            on_click=self._export_pdf, visible=False,
        )
        self._star_rating = StarRating(
            max_stars=5, initial_rating=0,
            on_change=self._on_rating_change,
        )
        self._star_rating.visible = False

        # --- 报告区 ---
        self._report_view = ft.Markdown(
            value="", selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
            code_theme="monokai-sublime",
            visible=False,
        )

        # --- K 线图 ---
        self._chart_image = ft.Image(src="", visible=False, fit="contain", width=700)

        # --- 容器 ---
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
                            self._export_btn,
                            self._star_rating,
                            ft.Text("报告评分", size=13, color=ft.Colors.GREY_600),
                        ]),
                    ],
                ),
            ]),
        )
        self._chart_container = chart_container
        self._report_container = report_container

        # --- 整体布局 ---
        self.content = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    margin=ft.Margin(top=20, right=0, bottom=0, left=0),
                    content=ft.ResponsiveRow(
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=16,
                        controls=[
                            ft.Column(col={"sm": 6, "md": 2}, controls=[self._market_dd]),
                            ft.Column(col={"sm": 6, "md": 3}, controls=[self._code_input]),
                            ft.Column(col={"sm": 6, "md": 2}, controls=[self._period_dd]),
                            ft.Column(col={"sm": 6, "md": 2}, controls=[self._strategy_dd]),
                            ft.Column(col={"sm": 12, "md": 2}, controls=[
                                ft.Row(spacing=8, controls=[self._start_btn, self._stop_btn]),
                            ]),
                        ],
                    ),
                ),
                self._progress_row,
                self._error_text,
                chart_container,
                report_container,
            ],
        )
        return self.content

    # ======================== 按钮控制 ========================

    def _set_running(self):
        self._start_btn.disabled = True
        self._stop_btn.disabled = False
        self._start_btn.update()
        self._stop_btn.update()

    def _set_idle(self):
        self._start_btn.disabled = False
        self._stop_btn.disabled = True
        self._start_btn.update()
        self._stop_btn.update()

    # ======================== 分析流程 ========================

    def _start_analysis(self, e):
        raw = self._code_input.value.strip()
        if not raw:
            self._show_error("请输入股票代码或名称")
            return

        if not Settings().get("work_dir", ""):
            self._show_error("请先在设置中配置工作目录")
            return

        self._show_error("")
        self._reset_result_state()
        self._set_running()
        self._stop_flag = False
        self._progress_row.visible = True
        self._progress_text.value = "正在搜索股票..."
        self._progress_row.update()
        self.page.update()

        market = self._market_dd.value
        period = self._period_dd.value
        strategy_name = self._strategy_dd.value
        thread = threading.Thread(
            target=self._do_search_then_analyze,
            args=(raw, market, period, strategy_name),
            daemon=True,
        )
        thread.start()

    def _stop_analysis(self, e):
        self._stop_flag = True
        self._show_error("正在停止...")

    def _reset_result_state(self):
        self._analysis_result = None
        self._current_report_id = None
        self._chart_path = None
        self._report_content = ""
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

    # ======================== 搜索 → 分析  ========================

    def _do_search_then_analyze(self, raw: str, market: str, period: str, strategy_name: str):
        """后台线程：搜索股票代码 → 调用分析流程。"""
        code = raw.strip().upper()

        if market == "A":
            if not (code.isascii() and code.isdigit() and len(code) == 6):
                from utils.helpers import _search_a_stock_fallback, _search_a_stock
                self._update_progress("正在搜索 A 股...")
                results = _search_a_stock_fallback(raw)
                if not results:
                    results = _search_a_stock(raw)
                if not results:
                    self._show_result_error(f"未找到与「{raw}」匹配的 A 股")
                    return
                code = results[0]["code"]
        else:
            if not (code.isascii() and code.replace(".", "").replace("-", "").isalpha()):
                from utils.helpers import _search_us_stock_fallback, _search_us_stock_online
                self._update_progress("正在搜索美股...")
                results = _search_us_stock_fallback(raw)
                if not results:
                    results = _search_us_stock_online(raw)
                if not results:
                    self._show_result_error(f"未找到与「{raw}」匹配的美股")
                    return
                code = results[0]["code"]

        if self._stop_flag:
            return
        self._run_analysis(code, market, period, strategy_name)

    def _run_analysis(self, code: str, market: str, period: str, strategy_name: str):
        try:
            # 1. 股票信息
            logger.info(f"[1/9] 获取股票信息: {code}")
            self._update_progress("正在获取股票信息...")
            fetcher = get_stock_fetcher()
            info = fetcher.fetch_stock_info(code)
            if info:
                Database().upsert_stock(info)
                logger.info(f"[1/9] 股票信息: {info.name} ({info.industry})")
            else:
                logger.warning(f"[1/9] 未获取到股票信息，使用默认名")
                info = StockInfo(code=code, name=code, market=market)

            if self._stop_flag: return

            # 2. 股价数据（优先从数据库读取缓存，缺失部分才联网获取）
            logger.info(f"[2/9] 获取股价数据...")
            self._update_progress("正在获取股价数据...")
            start, end = get_backtest_dates(period)
            prices = Database().get_prices(code, start, end)
            logger.info(f"[2/9] 缓存数据: {len(prices)} 条")
            need_fetch = len(prices) < 5
            if not need_fetch and prices:
                from datetime import date, timedelta
                if prices[-1].date < (date.today() - timedelta(days=7)).isoformat():
                    need_fetch = True
            if need_fetch:
                logger.info(f"[2/9] 缓存不足，联网获取 ({start} ~ {end})...")
                self._update_progress("正在联网获取最新股价数据...")
                new_prices = fetcher.fetch_price_history(code, start, end)
                if new_prices:
                    Database().insert_prices(new_prices)
                    prices = Database().get_prices(code, start, end)
                    logger.info(f"[2/9] 联网获取: {len(new_prices)} 条，总计 {len(prices)} 条")
            else:
                logger.info(f"[2/9] 使用缓存数据 ({len(prices)} 条，最新 {prices[-1].date})")

            if not prices:
                self._show_result_error(f"无法获取 {code} 的股价数据，请检查代码或网络")
                return

            if self._stop_flag: return

            # 3. DataFrame
            import pandas as pd
            df = pd.DataFrame([p.to_dict() for p in prices])
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            logger.info(f"[3/9] 数据预处理: {len(df)} 行, {df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {df['date'].iloc[-1].strftime('%Y-%m-%d')}")

            # 4. 技术指标
            logger.info(f"[4/9] 计算技术指标...")
            self._update_progress("正在计算技术指标...")
            df = calc_all_indicators(df)
            logger.info(f"[4/9] 技术指标计算完成 (MA/MACD/RSI/布林带/KDJ)")

            if self._stop_flag: return

            # 5. 新闻
            logger.info(f"[5/9] 获取新闻...")
            news_list = []
            try:
                self._update_progress("正在获取新闻...")
                news_list = fetch_news(code, market)
            except Exception as e:
                logger.warning(f"News fetch failed: {e}")
            logger.info(f"[5/9] 新闻: {len(news_list)} 条")

            logger.info(f"[5/9] 新闻情感分析...")
            self._update_progress("正在进行新闻情感分析...")
            news_list = analyze(news_list)
            news_agg = aggregate(news_list)
            logger.info(f"[5/9] 情感分析: {news_agg['sentiment_score']:.2f} ({news_agg['summary'][:50]}...)")

            if self._stop_flag: return

            # 6. 回测
            logger.info(f"[6/9] 运行回测: {strategy_name}...")
            self._update_progress("正在运行回测...")
            strategy = get_strategy(strategy_name)
            bt_df = df.copy()
            bt_df = strategy.generate_signals(bt_df)
            engine = BacktestEngine()
            bt_result = engine.run(bt_df, strategy)
            logger.info(f"[6/9] 回测: 总收益={bt_result['total_return']:.2%}, 夏普={bt_result['sharpe_ratio']:.2f}, 回撤={bt_result['max_drawdown']:.2%}")

            if self._stop_flag: return

            # 7. K 线图
            logger.info(f"[7/9] 生成 K 线图...")
            self._update_progress("正在生成 K 线图...")
            chart_df = df.copy()
            chart_df["signal"] = bt_df.get("signal", "")
            self._chart_path = generate_kline_chart(chart_df, code, info.name)
            logger.info(f"[7/9] K 线图: {self._chart_path or '生成失败'}")

            # 8. 报告
            logger.info(f"[8/9] 生成分析报告 (LLM: {Settings().get('llm_model')})...")
            self._update_progress("正在生成分析报告...")
            tech = summarize(df, info.name)
            self._report_content = generate_report(info.to_dict(), tech, news_agg, bt_result)
            if self._report_content:
                logger.info(f"[8/9] 报告生成完成: {len(self._report_content)} 字符")
            else:
                logger.error("[8/9] 报告生成失败！使用默认文本")
                self._report_content = "报告生成失败，请稍后重试。"

            # 9. 保存
            logger.info(f"[9/9] 保存报告到数据库...")
            self._update_progress("正在保存报告...")
            report = AnalysisReport(
                code=code, name=info.name, market=market,
                backtest_period=period,
                create_time=datetime.now().isoformat(),
                content=self._report_content,
            )
            self._current_report_id = Database().insert_report(report)
            self._analysis_result = {"stock": info.to_dict(), "backtest": bt_result}
            self._show_results()

        except Exception as ex:
            logger.exception(f"Analysis failed for {code}")
            self._show_result_error(f"分析出错：{ex}")
        finally:
            try:
                self.page.run_task(self._done_async)
            except Exception:
                pass

    # ======================== 线程安全 UI 更新 ========================

    def _update_progress(self, text: str):
        try:
            self.page.run_task(self._update_progress_async, text)
        except Exception:
            pass

    def _show_result_error(self, msg: str):
        try:
            self.page.run_task(self._show_result_error_async, msg)
        except Exception:
            pass

    def _show_results(self):
        try:
            self.page.run_task(self._show_results_async)
        except Exception:
            pass

    def _show_result_error(self, msg: str):
        print(f"[ERROR] {msg}")
        try:
            self.page.run_task(self._show_result_error_async, msg)
        except Exception as e:
            print(f"[ERROR] Failed to show error via run_task: {e}")

    def _show_results(self):
        try:
            self.page.run_task(self._show_results_async)
        except Exception:
            pass

    async def _update_progress_async(self, text: str):
        self._progress_text.value = text
        self._progress_row.update()

    async def _show_result_error_async(self, msg: str):
        self._show_error(msg)
        self._progress_row.visible = False
        self._progress_row.update()
        self._set_idle()
        self.page.update()

    async def _done_async(self):
        self._progress_row.visible = False
        self._progress_row.update()
        self._set_idle()
        self.page.update()

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

    def _export_pdf(self, e):
        if not self._report_content:
            self._show_error("没有可导出的报告")
            return
        si = self._analysis_result.get("stock", {}) if self._analysis_result else {}
        pdf_path = export_report_to_pdf(
            self._report_content, self._chart_path or "",
            si.get("name", ""), si.get("code", ""),
            self._period_dd.value,
        )
        if pdf_path:
            if self._current_report_id:
                Database().update_report_pdf(self._current_report_id, pdf_path)
            self.page.snack_bar = ft.SnackBar(
                ft.Text(f"PDF 已导出：{pdf_path}"),
                bgcolor=ft.Colors.GREEN_700,
            )
            self.page.snack_bar.open = True
            self.page.update()
        else:
            self._show_error("PDF 导出失败")

    def _on_rating_change(self, rating: int):
        if self._current_report_id and rating > 0:
            Database().update_report_rating(self._current_report_id, rating)
            self.page.snack_bar = ft.SnackBar(
                ft.Text(f"已评分：{rating} 星"),
                bgcolor=ft.Colors.BLUE_700,
            )
            self.page.snack_bar.open = True
            self.page.update()
