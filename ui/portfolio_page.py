"""
我的持仓页面 — 持仓管理、关注列表和综合分析。

功能：
  1. 账户资金（美股 USD + A股 CNY）
  2. 当前持仓表格（市场/代码/名称/股数/成本价）
  3. 关注股票表格（市场/代码/名称）
  4. 持仓综合分析（结合余额 + 持仓 + 关注，LLM 生成完整报告）
"""

import logging
import threading
import time
from datetime import datetime

import flet as ft

from config.settings import Settings
from data.database import Database
from data.models import AnalysisReport
from services.portfolio_service import PortfolioService
from ui.components import StarRating

logger = logging.getLogger(__name__)


class PortfolioPage(ft.Container):
    """我的持仓页面 — 持仓管理 + 综合分析。"""

    def __init__(self):
        super().__init__()
        self._service = PortfolioService()
        self._current_report_id = None
        self._report_content = ""
        self._analysis_started_at = None
        self._editing_holding_id = None
        self._active_editor_tab = "holdings"
        self._evaluation_market = "US"

    # ======================== 样式 ========================

    _PANEL_STYLE = {
        "bgcolor": ft.Colors.WHITE,
        "border_radius": 8,
        "padding": ft.Padding(20, 18, 20, 18),
        "shadow": ft.BoxShadow(
            spread_radius=0, blur_radius=8,
            color=ft.Colors.with_opacity(0.06, ft.Colors.BLACK),
            offset=ft.Offset(0, 1),
        ),
    }

    # ======================== 构建 UI ========================

    def build(self):
        # ── 账户资金 ──
        balance = self._service.get_balance()
        self._us_balance = ft.TextField(
            label="美股可用资金 (USD)", value=str(balance.us_balance),
            width=250, border_radius=8,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        self._a_balance = ft.TextField(
            label="A股可用资金 (CNY)", value=str(balance.a_balance),
            width=250, border_radius=8,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        save_balance_btn = ft.IconButton(
            icon=ft.Icons.SAVE, tooltip="保存余额",
            on_click=self._on_save_balance,
        )

        balance_panel = ft.Container(
            **self._PANEL_STYLE,
            content=ft.Column(spacing=12, controls=[
                ft.Text("账户资金", size=17, weight=ft.FontWeight.BOLD),
                ft.Row(spacing=10, wrap=True, controls=[
                    self._us_balance,
                    self._a_balance,
                    save_balance_btn,
                ]),
            ]),
        )

        # ── 当前持仓 ──
        self._holdings_list = ft.Column(spacing=6)
        # 添加持仓行（顺序与表头一致：市场 → 代码 → 名称 → 股数 → 成本价 → 按钮）
        self._h_market_dd = ft.Dropdown(
            width=100, value="US",
            options=[
                ft.dropdown.Option("US", "美股"),
                ft.dropdown.Option("A", "A股"),
            ],
        )
        self._h_code_input = ft.TextField(
            label="代码", hint_text="AAPL", width=118, border_radius=8,
            on_change=self._on_h_code_change,
        )
        self._h_name_text = ft.Text("", size=13, width=130, color=ft.Colors.GREY_600)
        self._h_shares = ft.TextField(label="股数", value="0", width=96, border_radius=8)
        self._h_cost = ft.TextField(label="成本价", value="0.00", width=106, border_radius=8)
        self._h_add_btn = ft.IconButton(
            icon=ft.Icons.ADD_CIRCLE, tooltip="添加持仓",
            on_click=self._on_add_holding,
            icon_color=ft.Colors.GREEN_700,
        )

        holdings_panel = ft.Container(
            **self._PANEL_STYLE,
            content=ft.Column(spacing=12, controls=[
                ft.Text("当前持仓", size=17, weight=ft.FontWeight.BOLD),
                # 表头
                ft.Row(spacing=0, controls=[
                    ft.Container(ft.Text("市场", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=64),
                    ft.Container(ft.Text("代码", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=104),
                    ft.Container(ft.Text("名称", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=150),
                    ft.Container(ft.Text("股数", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=92),
                    ft.Container(ft.Text("成本价", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=98),
                    ft.Container(ft.Text("操作", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=92),
                ]),
                ft.Divider(height=1, color=ft.Colors.GREY_200),
                self._holdings_list,
                # 添加行（顺序与表头对齐）
                ft.Row(spacing=8, wrap=True, controls=[
                    self._h_market_dd,
                    self._h_code_input,
                    self._h_name_text,
                    self._h_shares,
                    self._h_cost,
                    self._h_add_btn,
                ]),
            ]),
        )

        # ── 关注股票 ──
        self._watchlist_list = ft.Column(spacing=6)
        # 添加关注行（顺序与表头一致：市场 → 代码 → 名称 → 按钮）
        self._w_market_dd = ft.Dropdown(
            width=100, value="US",
            options=[
                ft.dropdown.Option("US", "美股"),
                ft.dropdown.Option("A", "A股"),
            ],
        )
        self._w_code_input = ft.TextField(
            label="代码", hint_text="NVDA", width=118, border_radius=8,
            on_change=self._on_w_code_change,
        )
        self._w_name_text = ft.Text("", size=13, width=180, color=ft.Colors.GREY_600)
        self._w_add_btn = ft.IconButton(
            icon=ft.Icons.ADD_CIRCLE, tooltip="添加关注",
            on_click=self._on_add_watch,
            icon_color=ft.Colors.GREEN_700,
        )

        watchlist_panel = ft.Container(
            **self._PANEL_STYLE,
            content=ft.Column(spacing=12, controls=[
                ft.Text("关注股票", size=17, weight=ft.FontWeight.BOLD),
                # 表头
                ft.Row(spacing=0, controls=[
                    ft.Container(ft.Text("市场", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=64),
                    ft.Container(ft.Text("代码", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=104),
                    ft.Container(ft.Text("名称", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=240),
                    ft.Container(ft.Text("操作", size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_600), width=42),
                ]),
                ft.Divider(height=1, color=ft.Colors.GREY_200),
                self._watchlist_list,
                # 添加行（顺序与表头对齐：市场 → 代码 → 名称 → 按钮）
                ft.Row(spacing=8, wrap=True, controls=[
                    self._w_market_dd,
                    self._w_code_input,
                    self._w_name_text,
                    self._w_add_btn,
                ]),
            ]),
        )

        # ── 分析设置 ──
        self._market_toggle = ft.Row(spacing=16, controls=[
            ft.ElevatedButton(
                content=ft.Text("🇺🇸 美股"),
                on_click=lambda e: self._select_market("US"),
                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            ),
            ft.ElevatedButton(
                content=ft.Text("🇨🇳 A股"),
                on_click=lambda e: self._select_market("A"),
                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            ),
        ])
        self._selected_market = "US"
        self._us_btn = self._market_toggle.controls[0]
        self._a_btn = self._market_toggle.controls[1]
        self._update_market_buttons()

        self._period_dd = ft.Dropdown(
            label="回测周期", width=130, value="1y",
            options=[
                ft.dropdown.Option("6m", "6 个月"),
                ft.dropdown.Option("1y", "1 年"),
                ft.dropdown.Option("3y", "3 年"),
            ],
            border_radius=8,
        )

        has_realtime = bool(Settings().get("stock_token_us", "") or Settings().get("stock_token_a", ""))
        self._mode_dd = ft.Dropdown(
            label="分析模式", width=140, value="eod",
            options=[
                ft.dropdown.Option("eod", "📊 盘后分析"),
                ft.dropdown.Option("intraday", "⏱ 盘中分析", disabled=not has_realtime),
                ft.dropdown.Option("pre", "🌅 盘前分析"),
            ],
            border_radius=8,
        )

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
            on_click=self._on_analyze,
        )

        self._progress_row = ft.Row(
            visible=False, alignment=ft.MainAxisAlignment.CENTER, spacing=12,
            controls=[
                ft.ProgressRing(width=18, height=18, stroke_width=3, color=ft.Colors.BLUE_700),
                ft.Text("", size=13, color=ft.Colors.BLUE_700),
            ],
        )
        self._progress_text = self._progress_row.controls[1]
        self._error_text = ft.Text("", color=ft.Colors.RED, size=13)

        analysis_panel = ft.Container(
            **self._PANEL_STYLE,
            content=ft.Column(spacing=16, controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Column(spacing=2, controls=[
                            ft.Text("组合分析", size=18, weight=ft.FontWeight.BOLD),
                            ft.Text("先生成可执行计划，再阅读历史评估和完整报告", size=12, color=ft.Colors.GREY_600),
                        ]),
                        self._start_btn,
                    ],
                ),
                ft.Row(spacing=12, wrap=True, controls=[
                    ft.Container(content=self._market_toggle),
                    self._period_dd,
                    self._mode_dd,
                ]),
                self._progress_row,
                self._error_text,
            ]),
        )

        # ── 历史预测评估面板 ──
        self._evaluation_view = ft.Markdown(
            value="",
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
        )
        self._evaluation_refresh_btn = ft.ElevatedButton(
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.REFRESH, size=16),
                ft.Text("刷新评估"),
            ]),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=8),
                padding=ft.Padding(14, 10, 14, 10),
            ),
            on_click=self._on_refresh_evaluation,
        )
        self._evaluation_us_btn = ft.ElevatedButton(
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.PUBLIC, size=16),
                ft.Text("美股"),
            ]),
            on_click=lambda e: self._select_evaluation_market("US"),
        )
        self._evaluation_a_btn = ft.ElevatedButton(
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.SHOW_CHART, size=16),
                ft.Text("A股"),
            ]),
            on_click=lambda e: self._select_evaluation_market("A"),
        )
        self._update_evaluation_market_buttons()
        self._evaluation_container = ft.Container(
            **self._PANEL_STYLE,
            content=ft.Column(spacing=12, controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text("历史预测评估", size=17, weight=ft.FontWeight.BOLD),
                        ft.Row(spacing=8, controls=[
                            self._evaluation_us_btn,
                            self._evaluation_a_btn,
                            self._evaluation_refresh_btn,
                        ]),
                    ],
                ),
                ft.Divider(height=1, color=ft.Colors.GREY_200),
                self._evaluation_view,
            ]),
        )

        # ── 报告展示区 ──
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

        self._report_container = ft.Container(
            visible=False,
            margin=ft.Margin(0, 16, 0, 0),
            **self._PANEL_STYLE,
            content=ft.Column([
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Column(spacing=2, controls=[
                            ft.Text("报告正文", size=18, weight=ft.FontWeight.BOLD),
                            ft.Text("先看顶部操作方案，再看研究员观察和历史验证", size=12, color=ft.Colors.GREY_600),
                        ]),
                        self._export_btn,
                    ],
                ),
                ft.Divider(height=1, color=ft.Colors.GREY_200),
                self._report_view,
                ft.Divider(height=1, color=ft.Colors.GREY_200),
                ft.Row(
                    alignment=ft.MainAxisAlignment.END,
                    controls=[
                        ft.Row(spacing=8, controls=[
                            self._star_rating,
                            ft.Text("评分", size=13, color=ft.Colors.GREY_600),
                        ]),
                    ],
                ),
            ]),
        )

        # ── 编辑工作台：生成报告前显示 ──
        self._tab_holdings_btn = ft.ElevatedButton(
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.ACCOUNT_BALANCE_WALLET, size=16),
                ft.Text("账户与持仓"),
            ]),
            on_click=lambda e: self._select_editor_tab("holdings"),
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
        )
        self._tab_watchlist_btn = ft.ElevatedButton(
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.STAR, size=16),
                ft.Text("关注股票"),
            ]),
            on_click=lambda e: self._select_editor_tab("watchlist"),
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
        )
        self._tab_evaluation_btn = ft.ElevatedButton(
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.INSIGHTS, size=16),
                ft.Text("历史评估"),
            ]),
            on_click=lambda e: self._select_editor_tab("evaluation"),
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
        )
        self._holdings_tab_container = ft.Container(
            content=ft.Column(spacing=14, controls=[balance_panel, holdings_panel]),
        )
        self._watchlist_tab_container = ft.Container(content=watchlist_panel, visible=False)
        self._evaluation_tab_container = ft.Container(content=self._evaluation_container, visible=False)
        self._editor_container = ft.Column(
            spacing=14,
            controls=[
                analysis_panel,
                ft.Row(
                    spacing=10,
                    wrap=True,
                    controls=[
                        self._tab_holdings_btn,
                        self._tab_watchlist_btn,
                        self._tab_evaluation_btn,
                    ],
                ),
                self._holdings_tab_container,
                self._watchlist_tab_container,
                self._evaluation_tab_container,
            ],
        )
        self._update_editor_tab_buttons()

        # ── 报告阅读模式：生成报告后全宽显示 ──
        self._back_to_editor_btn = ft.ElevatedButton(
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.ARROW_BACK, size=16),
                ft.Text("返回编辑"),
            ]),
            on_click=self._on_back_to_editor,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=8),
                padding=ft.Padding(14, 10, 14, 10),
            ),
        )
        self._rerun_btn = ft.ElevatedButton(
            content=ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.REPLAY, size=16),
                ft.Text("重新分析"),
            ]),
            on_click=self._on_analyze,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=8),
                padding=ft.Padding(14, 10, 14, 10),
            ),
        )
        self._report_page_container = ft.Container(
            visible=False,
            content=ft.Column(spacing=12, controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Column(spacing=3, controls=[
                            ft.Text("综合分析报告", size=24, weight=ft.FontWeight.BOLD),
                            ft.Text("全宽阅读模式：先看一分钟操作台，再看条件触发计划", size=13, color=ft.Colors.GREY_600),
                        ]),
                        ft.Row(spacing=8, controls=[
                            self._back_to_editor_btn,
                            self._rerun_btn,
                        ]),
                    ],
                ),
                self._report_container,
            ]),
        )

        self.content = ft.Column(
            scroll=ft.ScrollMode.AUTO, expand=True,
            spacing=14,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Column(spacing=3, controls=[
                            ft.Text("我的持仓", size=24, weight=ft.FontWeight.BOLD),
                            ft.Text("录入账户和标的后，在右侧生成组合级交易计划", size=13, color=ft.Colors.GREY_600),
                        ]),
                    ],
                ),
                self._editor_container,
                self._report_page_container,
            ],
        )

        # 延迟加载数据（did_mount 中执行，此时控件已挂载到页面）
        return self.content

    def did_mount(self):
        """页面挂载后加载数据。"""
        self._load_holdings()
        self._load_watchlist()
        try:
            self._update_market_buttons()
        except Exception:
            pass
        self._load_evaluation_panel()

    # ======================== 市场切换 ========================

    def _select_market(self, market: str):
        self._selected_market = market
        self._update_market_buttons()
        self._load_evaluation_panel()

    def _update_market_buttons(self):
        us_active = self._selected_market == "US"
        self._us_btn.style = ft.ButtonStyle(
            bgcolor={"": ft.Colors.BLUE_700 if us_active else ft.Colors.GREY_300, "hovered": ft.Colors.BLUE_900},
            color={"": ft.Colors.WHITE if us_active else ft.Colors.GREY_700, "hovered": ft.Colors.WHITE},
            shape=ft.RoundedRectangleBorder(radius=8),
        )
        self._a_btn.style = ft.ButtonStyle(
            bgcolor={"": ft.Colors.BLUE_700 if not us_active else ft.Colors.GREY_300, "hovered": ft.Colors.BLUE_900},
            color={"": ft.Colors.WHITE if not us_active else ft.Colors.GREY_700, "hovered": ft.Colors.WHITE},
            shape=ft.RoundedRectangleBorder(radius=8),
        )
        try:
            self._us_btn.update()
            self._a_btn.update()
        except Exception:
            pass  # 控件尚未挂载到页面

    # ======================== 编辑区切换 ========================

    def _select_editor_tab(self, name: str):
        self._active_editor_tab = name
        self._holdings_tab_container.visible = name == "holdings"
        self._watchlist_tab_container.visible = name == "watchlist"
        self._evaluation_tab_container.visible = name == "evaluation"
        self._update_editor_tab_buttons()
        try:
            self._holdings_tab_container.update()
            self._watchlist_tab_container.update()
            self._evaluation_tab_container.update()
        except Exception:
            pass

    def _update_editor_tab_buttons(self):
        active = self._active_editor_tab

        def style_for(name: str):
            is_active = active == name
            return ft.ButtonStyle(
                bgcolor={"": ft.Colors.BLUE_700 if is_active else ft.Colors.GREY_200},
                color={"": ft.Colors.WHITE if is_active else ft.Colors.GREY_700},
                shape=ft.RoundedRectangleBorder(radius=8),
                padding=ft.Padding(14, 10, 14, 10),
            )

        self._tab_holdings_btn.style = style_for("holdings")
        self._tab_watchlist_btn.style = style_for("watchlist")
        self._tab_evaluation_btn.style = style_for("evaluation")
        try:
            self._tab_holdings_btn.update()
            self._tab_watchlist_btn.update()
            self._tab_evaluation_btn.update()
        except Exception:
            pass

    def _on_back_to_editor(self, e):
        self._report_page_container.visible = False
        self._editor_container.visible = True
        try:
            self._report_page_container.update()
            self._editor_container.update()
        except Exception:
            pass

    # ======================== 历史预测评估 ========================

    def _select_evaluation_market(self, market: str):
        self._evaluation_market = market
        self._update_evaluation_market_buttons()
        self._load_evaluation_panel()

    def _update_evaluation_market_buttons(self):
        us_active = self._evaluation_market == "US"

        def style(active: bool):
            return ft.ButtonStyle(
                bgcolor={"": ft.Colors.BLUE_700 if active else ft.Colors.GREY_200},
                color={"": ft.Colors.WHITE if active else ft.Colors.GREY_700},
                shape=ft.RoundedRectangleBorder(radius=8),
                padding=ft.Padding(12, 9, 12, 9),
            )

        self._evaluation_us_btn.style = style(us_active)
        self._evaluation_a_btn.style = style(not us_active)
        try:
            self._evaluation_us_btn.update()
            self._evaluation_a_btn.update()
        except Exception:
            pass

    def _load_evaluation_panel(self):
        try:
            self._evaluation_view.value = self._service.build_historical_evaluation_panel(self._evaluation_market)
            self._evaluation_view.update()
        except Exception as ex:
            logger.warning(f"历史预测评估面板加载失败: {ex}")
            self._evaluation_view.value = f"### 历史预测评估面板\n\n- 加载失败：{ex}"
            try:
                self._evaluation_view.update()
            except Exception:
                pass

    def _on_refresh_evaluation(self, e):
        self._load_evaluation_panel()
        self._show_snack("历史预测评估已刷新", "blue")

    # ======================== 余额保存 ========================

    def _on_save_balance(self, e):
        try:
            us = float(self._us_balance.value or 0)
            a = float(self._a_balance.value or 0)
            self._service.save_balance(us, a)
            self._show_snack("余额已保存", "green")
        except ValueError:
            self._show_snack("请输入有效数字", "red")

    # ======================== 持仓 CRUD ========================

    def _load_holdings(self):
        self._holdings_list.controls.clear()
        holdings = self._service.list_holdings()
        if not holdings:
            self._holdings_list.controls.append(
                ft.Text("暂无持仓，请在下方添加", size=13, color=ft.Colors.GREY_500)
            )
        else:
            for h in holdings:
                self._holdings_list.controls.append(self._build_holding_row(h))
        self._holdings_list.update()

    def _build_holding_row(self, h) -> ft.Row:
        market_label = "美股" if h.market == "US" else "A股"
        if self._editing_holding_id == h.id:
            shares_field = ft.TextField(
                value=f"{h.shares:g}",
                width=86,
                height=38,
                dense=True,
                content_padding=ft.Padding(8, 6, 8, 6),
                keyboard_type=ft.KeyboardType.NUMBER,
            )
            cost_field = ft.TextField(
                value=f"{h.cost_price:.2f}",
                width=92,
                height=38,
                dense=True,
                content_padding=ft.Padding(8, 6, 8, 6),
                keyboard_type=ft.KeyboardType.NUMBER,
            )
            return ft.Row(spacing=0, controls=[
                ft.Container(ft.Text(market_label, size=13, weight=ft.FontWeight.W_500), width=64),
                ft.Container(ft.Text(h.code, size=13, weight=ft.FontWeight.BOLD), width=104),
                ft.Container(ft.Text(h.name, size=13, overflow=ft.TextOverflow.ELLIPSIS), width=150),
                ft.Container(shares_field, width=92),
                ft.Container(cost_field, width=98),
                ft.Row(spacing=0, controls=[
                    ft.IconButton(
                        icon=ft.Icons.CHECK, icon_size=18,
                        icon_color=ft.Colors.GREEN_700,
                        tooltip="保存",
                        on_click=lambda e, holding=h, sf=shares_field, cf=cost_field:
                            self._on_save_holding_edit(holding, sf.value, cf.value),
                    ),
                    ft.IconButton(
                        icon=ft.Icons.CLOSE, icon_size=18,
                        icon_color=ft.Colors.GREY_600,
                        tooltip="取消",
                        on_click=self._on_cancel_holding_edit,
                    ),
                ], width=92),
            ])

        return ft.Row(spacing=0, controls=[
            ft.Container(ft.Text(market_label, size=13, weight=ft.FontWeight.W_500), width=64),
            ft.Container(ft.Text(h.code, size=13, weight=ft.FontWeight.BOLD), width=104),
            ft.Container(ft.Text(h.name, size=13, overflow=ft.TextOverflow.ELLIPSIS), width=150),
            ft.Container(ft.Text(f"{h.shares:,.0f}", size=13), width=92),
            ft.Container(ft.Text(f"{h.cost_price:.2f}", size=13), width=98),
            ft.Row(spacing=0, controls=[
                ft.IconButton(
                    icon=ft.Icons.EDIT, icon_size=18,
                    icon_color=ft.Colors.BLUE_700,
                    tooltip="编辑股数和成本价",
                    on_click=lambda e, hid=h.id: self._on_edit_holding(hid),
                ),
                ft.IconButton(
                    icon=ft.Icons.DELETE, icon_size=18,
                    icon_color=ft.Colors.RED_400,
                    tooltip="删除",
                    on_click=lambda e, hid=h.id: self._on_delete_holding(hid),
                ),
            ], width=92),
        ])

    async def _on_h_code_change(self, e):
        """输入代码后自动识别市场 + 返显名称。"""
        code = self._h_code_input.value.strip()
        if len(code) < 1:
            return
        if len(code) >= 2 or (len(code) == 6 and code.isdigit()):
            result = self._service.search_stock(code)
            if result:
                self._h_name_text.value = result["name"][:20]
                self._h_market_dd.value = result["market"]
                self._h_name_text.update()
                self._h_market_dd.update()

    def _on_add_holding(self, e):
        code = self._h_code_input.value.strip()
        if not code:
            self._show_snack("请输入股票代码", "orange")
            return
        name = self._h_name_text.value or code
        market = self._h_market_dd.value or "US"

        try:
            shares = float(self._h_shares.value or 0)
            cost = float(self._h_cost.value or 0)
        except ValueError:
            self._show_snack("股数和成本价请输入有效数字", "orange")
            return
        if shares <= 0:
            self._show_snack("股数必须大于 0", "orange")
            return

        self._service.add_or_update_holding(code, name, market, shares, cost)
        self._h_code_input.value = ""
        self._h_name_text.value = ""
        self._h_market_dd.value = "US"
        self._h_shares.value = "0"
        self._h_cost.value = "0.00"
        self._h_code_input.update()
        self._h_name_text.update()
        self._h_market_dd.update()
        self._h_shares.update()
        self._h_cost.update()
        self._load_holdings()
        self._show_snack(f"已添加 {code}", "green")

    def _on_delete_holding(self, holding_id: int | None):
        if holding_id:
            if self._editing_holding_id == holding_id:
                self._editing_holding_id = None
            self._service.delete_holding(holding_id)
            self._load_holdings()

    def _on_edit_holding(self, holding_id: int | None):
        if not holding_id:
            return
        self._editing_holding_id = holding_id
        self._load_holdings()

    def _on_cancel_holding_edit(self, e):
        self._editing_holding_id = None
        self._load_holdings()

    def _on_save_holding_edit(self, holding, shares_value: str, cost_value: str):
        try:
            shares = float(shares_value or 0)
            cost = float(cost_value or 0)
        except ValueError:
            self._show_snack("股数和成本价请输入有效数字", "orange")
            return
        if shares <= 0:
            self._show_snack("股数必须大于 0；清仓请使用删除", "orange")
            return
        if cost < 0:
            self._show_snack("成本价不能小于 0", "orange")
            return
        self._service.add_or_update_holding(
            holding.code, holding.name, holding.market, shares, cost
        )
        self._editing_holding_id = None
        self._load_holdings()
        self._show_snack(f"{holding.code} 持仓已更新", "green")

    # ======================== 关注 CRUD ========================

    def _load_watchlist(self):
        self._watchlist_list.controls.clear()
        watchlist = self._service.list_watchlist()
        if not watchlist:
            self._watchlist_list.controls.append(
                ft.Text("暂无关注股票，请在下方添加", size=13, color=ft.Colors.GREY_500)
            )
        else:
            for w in watchlist:
                self._watchlist_list.controls.append(self._build_watch_row(w))
        self._watchlist_list.update()

    def _build_watch_row(self, w) -> ft.Row:
        market_label = "美股" if w.market == "US" else "A股"
        return ft.Row(spacing=0, controls=[
            ft.Container(ft.Text(market_label, size=13, weight=ft.FontWeight.W_500), width=64),
            ft.Container(ft.Text(w.code, size=13, weight=ft.FontWeight.BOLD), width=104),
            ft.Container(ft.Text(w.name, size=13, overflow=ft.TextOverflow.ELLIPSIS), width=240),
            ft.IconButton(
                icon=ft.Icons.DELETE, icon_size=18,
                icon_color=ft.Colors.RED_400,
                tooltip="删除",
                on_click=lambda e, wid=w.id: self._on_delete_watch(wid),
            ),
        ])

    async def _on_w_code_change(self, e):
        """输入代码后自动识别市场 + 返显名称。"""
        code = self._w_code_input.value.strip()
        if len(code) >= 2:
            result = self._service.search_stock(code)
            if result:
                self._w_name_text.value = result["name"][:20]
                self._w_market_dd.value = result["market"]
                self._w_name_text.update()
                self._w_market_dd.update()

    def _on_add_watch(self, e):
        code = self._w_code_input.value.strip()
        if not code:
            self._show_snack("请输入股票代码", "orange")
            return
        name = self._w_name_text.value or code
        market = self._w_market_dd.value or "US"

        self._service.add_watch_item(code, name, market)
        self._w_code_input.value = ""
        self._w_name_text.value = ""
        self._w_market_dd.value = "US"
        self._w_code_input.update()
        self._w_name_text.update()
        self._w_market_dd.update()
        self._load_watchlist()
        self._show_snack(f"已添加关注 {code}", "green")

    def _on_delete_watch(self, item_id: int | None):
        if item_id:
            self._service.delete_watch_item(item_id)
            self._load_watchlist()

    # ======================== 综合分析 ========================

    def _on_analyze(self, e):
        settings = Settings()
        if not settings.is_fully_configured():
            missing = settings.missing_fields()
            from config.settings import Settings as S
            labels = [S.FIELD_LABELS.get(f, f) for f in missing]
            self._show_snack(f"请先在「设置」中配置：{'、'.join(labels)}", "orange")
            return

        market = self._selected_market
        market_label = "美股" if market == "US" else "A股"
        mode = self._mode_dd.value
        token_key = "stock_token_us" if market == "US" else "stock_token_a"
        if (mode == "intraday" or (mode == "pre" and market == "A")) and not settings.get(token_key):
            self._show_snack(
                f"{market_label}盘中/盘前分析需要配置「{market_label}数据源 Token」",
                "orange",
            )
            return

        self._reset_report_ui()
        self._analysis_started_at = time.monotonic()
        self._start_btn.disabled = True
        self._start_btn.update()
        self._progress_row.visible = True
        self._progress_text.value = "正在准备分析..."
        self._progress_row.update()
        self._error_text.value = ""
        self._error_text.update()
        self.page.update()

        thread = threading.Thread(
            target=self._run_analysis,
            args=(market, self._period_dd.value, mode),
            daemon=True,
        )
        thread.start()

    def _reset_report_ui(self):
        self._current_report_id = None
        self._report_content = ""
        self._report_view.value = ""
        self._report_view.visible = False
        self._report_container.visible = False
        self._report_page_container.visible = False
        self._editor_container.visible = True
        self._export_btn.visible = False
        self._star_rating.visible = False
        self._star_rating.rating = 0

    def _run_analysis(self, market: str, period: str, mode: str):
        def on_progress(msg: str):
            try:
                self.page.run_task(self._update_progress_async, msg)
            except Exception:
                pass

        try:
            result = self._service.analyze_portfolio(
                market=market, period=period, mode=mode,
                on_progress=on_progress,
            )
            self.page.run_task(self._show_results_async, result)
        except ValueError as ex:
            self.page.run_task(self._show_error_async, str(ex))
        except RuntimeError as ex:
            self.page.run_task(self._show_error_async, str(ex))
        except Exception as ex:
            logger.exception("Portfolio analysis failed")
            self.page.run_task(self._show_error_async, f"分析失败：{ex}")
        finally:
            try:
                self.page.run_task(self._done_async)
            except Exception:
                pass

    async def _update_progress_async(self, text: str):
        prefix = ""
        if self._analysis_started_at:
            elapsed = max(0, int(time.monotonic() - self._analysis_started_at))
            prefix = f"已用时 {elapsed//60:02d}:{elapsed%60:02d} · "
        self._progress_text.value = prefix + text
        self._progress_row.update()

    async def _show_results_async(self, result: dict):
        self._report_content = result.get("report_content", "")
        self._current_report_id = result.get("report_id")
        if not self._report_content:
            return
        self._report_view.value = self._report_content
        self._report_view.visible = True
        self._report_view.update()
        self._report_container.visible = True
        self._report_container.update()
        self._editor_container.visible = False
        self._report_page_container.visible = True
        self._editor_container.update()
        self._report_page_container.update()
        self._export_btn.visible = True
        self._export_btn.update()
        self._star_rating.rating = 0
        self._star_rating.visible = True
        self._star_rating.update()
        self.page.update()

    async def _show_error_async(self, msg: str):
        self._error_text.value = msg
        self._error_text.update()

    async def _done_async(self):
        self._progress_row.visible = False
        self._progress_row.update()
        self._analysis_started_at = None
        self._start_btn.disabled = False
        self._start_btn.update()
        self.page.update()

    # ======================== 导出 / 评分 ========================

    def _on_export_pdf(self, e):
        if not self._report_content:
            self._show_snack("没有可导出的报告", "orange")
            return
        try:
            path = self._export_html()
            if path and self._current_report_id:
                Database().update_report_pdf(self._current_report_id, path)
            if path:
                self._show_export_dialog(path)
            else:
                self._show_snack("导出失败", "red")
        except Exception as ex:
            logger.exception("导出失败")
            self._show_snack(f"导出失败：{ex}", "red")

    def _export_html(self) -> str:
        import os, base64
        html_dir = Settings().pdf_dir
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"portfolio_report_{timestamp}.html"
        filepath = os.path.join(html_dir, filename)

        from markdown_it import MarkdownIt
        md = MarkdownIt().enable(["table", "strikethrough"])
        body_html = md.render(self._report_content)

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>持仓综合分析报告</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; max-width: 1200px; margin: 32px auto; padding: 0 24px; color: #263238; line-height: 1.72; background: #fbfcfd; }}
  h1 {{ border-bottom: 2px solid #1f5f8b; padding-bottom: 8px; margin-top: 0; }}
  h2 {{ color: #1f5f8b; margin-top: 34px; padding-bottom: 6px; border-bottom: 1px solid #d8e2ec; }}
  h3 {{ margin-top: 24px; padding-left: 10px; border-left: 4px solid #1f5f8b; color: #25465f; }}
  h4 {{ margin-top: 18px; color: #334; }}
  table {{ border-collapse: collapse; display: block; width: 100%; margin: 14px 0 20px; overflow-x: auto; font-size: 14px; background: #fff; }}
  th, td {{ border: 1px solid #d7dde5; padding: 8px 10px; text-align: left; vertical-align: top; min-width: 72px; }}
  td {{ word-break: break-word; }}
  th {{ background: #1f5f8b; color: #fff; }}
  tr:nth-child(even) {{ background: #f5f7fa; }}
  blockquote {{ border-left: 3px solid #1f5f8b; padding: 8px 14px; color: #4f5b62; margin: 12px 0; background: #f4f8fb; }}
  li {{ margin: 4px 0; }}
  code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  pre {{ background: #f5f5f5; padding: 16px; border-radius: 6px; overflow-x: auto; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"HTML 报告已导出: {filepath}")
        return filepath

    def _show_export_dialog(self, filepath: str):
        import os, subprocess, sys, threading
        folder = os.path.dirname(filepath)

        def on_action(e):
            if sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            elif sys.platform == "win32":
                os.startfile(folder)
            else:
                subprocess.Popen(["xdg-open", folder])

        self.page.snack_bar = ft.SnackBar(
            content=ft.Text(f"已导出至：{os.path.basename(filepath)}"),
            bgcolor=ft.Colors.GREEN_700,
            action="打开目录",
            on_action=on_action,
            duration=5000,
        )
        self.page.snack_bar.open = True
        self.page.update()

        def _open():
            import webbrowser
            webbrowser.open(f"file://{filepath}")
        threading.Thread(target=_open, daemon=True).start()

    def _on_rating_change(self, rating: int):
        if self._current_report_id and rating > 0:
            Database().update_report_rating(self._current_report_id, rating)
            self._show_snack(f"已评分：{rating} 星", "blue")

    # ======================== 工具方法 ========================

    def _show_snack(self, msg: str, color: str = "blue"):
        bg = {"green": ft.Colors.GREEN_700, "red": ft.Colors.RED_500,
              "orange": ft.Colors.ORANGE_700, "blue": ft.Colors.BLUE_700}.get(color, ft.Colors.BLUE_700)
        self.page.snack_bar = ft.SnackBar(
            ft.Text(msg, color=ft.Colors.WHITE), bgcolor=bg, duration=3000,
        )
        self.page.snack_bar.open = True
        self.page.update()
