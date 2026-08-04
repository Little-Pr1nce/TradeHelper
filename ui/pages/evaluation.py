"""Standalone system capability evaluation page."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import flet as ft

from contracts import DecisionMode, HistoricalEvaluationQuery, LedgerViewKind, Market, ReportKind
from ..components.date_field import calendar_date_range_field
from ..components.report_view import chart_control, table_control
from ..theme import (
    BORDER, DANGER, PRIMARY, SUCCESS, SURFACE, SURFACE_MUTED, TEXT, TEXT_MUTED,
    WARNING, configure_field, feedback_banner, page_heading, panel, primary_button,
)
from ..update_dispatch import rebuild_on_page


_MODE_OPTIONS = (
    ft.dropdown.Option("all", "全部模式"),
    ft.dropdown.Option("pre", "盘前"),
    ft.dropdown.Option("intraday", "盘中"),
    ft.dropdown.Option("eod", "盘后"),
)


class AbilityEvaluationPage:
    """Read-only view over forecast, strategy, and joint outcome ledgers."""

    def __init__(self, evaluation_port=None, lookup_port=None):
        self.evaluation_port = evaluation_port
        self.lookup_port = lookup_port
        self.active_view = "overview"
        self.market = Market.US
        self.analysis_mode = "all"
        self.symbol = ""
        self.horizon = "all"
        self.source = "all"
        self.date_from = ""
        self.date_to = ""
        self.view = None
        self.resolved_instrument = None
        self.error = None
        self.notice = None
        self.busy = False
        self._cache = {}
        self._root = None

    def _update(self):
        rebuild_on_page(self._root, self._content)

    @staticmethod
    def _date(value, market, *, end=False):
        if not value.strip():
            return None
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
        zone = ZoneInfo("America/New_York") if market is Market.US else ZoneInfo("Asia/Shanghai")
        return datetime.combine(parsed, time.max if end else time.min, tzinfo=zone).astimezone(timezone.utc)

    def _resolve_instrument(self, raw):
        value = raw.strip()
        if not value:
            return None
        if self.lookup_port is None:
            raise ValueError("股票检索服务尚未配置")
        matches = tuple(self.lookup_port(self.market.value, value))
        if not matches:
            raise ValueError(f"未找到股票：{value}")
        instruments = tuple(getattr(item, "instrument", item) for item in matches)
        normalized = value.upper().split(".", 1)[0]
        exact = tuple(item for item in instruments if item.code.upper() == normalized)
        selected = exact[0] if exact else instruments[0]
        if len(exact) > 1 or not exact and len(instruments) > 1:
            names = "、".join(item.code for item in (exact or instruments)[:5])
            raise ValueError(f"找到多个候选：{names}，请补充更完整代码或名称")
        return selected

    def _ledger_kind(self):
        return {
            "single_forecast": LedgerViewKind.FORECAST,
            "single_strategy": LedgerViewKind.STRATEGY,
            "portfolio_forecast": LedgerViewKind.FORECAST,
            "portfolio_strategy": LedgerViewKind.JOINT,
        }.get(self.active_view)

    def _query(self):
        single = self.active_view in {"single_forecast", "single_strategy"}
        self.resolved_instrument = self._resolve_instrument(self.symbol) if single else None
        report_kind = None
        if self.active_view == "single_strategy" and self.source != "all":
            report_kind = ReportKind(self.source)
        elif self.active_view == "portfolio_forecast":
            report_kind = ReportKind.PORTFOLIO
        return HistoricalEvaluationQuery(
            market=self.market,
            ledger_kind=self._ledger_kind(),
            instrument=self.resolved_instrument,
            horizon=None if self.horizon == "all" else int(self.horizon),
            date_from=self._date(self.date_from, self.market),
            date_to=self._date(self.date_to, self.market, end=True),
            analysis_mode=None if self.analysis_mode == "all" else DecisionMode(self.analysis_mode),
            report_kind=report_kind,
        )

    def load(self, *, force=False):
        if self.evaluation_port is None:
            self.error = "历史评估服务尚未配置"
            return None
        try:
            query = self._query()
            if not force and query in self._cache:
                self.view = self._cache[query]
                self.error = None
                self.notice = (
                    f"已查询 {self.resolved_instrument.code}"
                    if self.resolved_instrument is not None else "能力评估已刷新"
                )
                return self.view
            if force:
                invalidate = getattr(self.evaluation_port, "invalidate", None)
                if callable(invalidate):
                    invalidate(self.market)
            method = getattr(self.evaluation_port, "load", self.evaluation_port)
            self.view = method(query)
            self._cache[query] = self.view
            self.error = None
            self.notice = (
                f"已查询 {self.resolved_instrument.code}"
                if self.resolved_instrument is not None else "能力评估已刷新"
            )
        except Exception as exc:
            self.error = str(exc)
        return self.view

    async def _reload(self, *, force=False):
        self.busy = True
        self._update()
        try:
            await asyncio.to_thread(self.load, force=force)
        finally:
            self.busy = False
            self._update()

    def on_show(self):
        if self.view is not None or self.busy:
            return
        try:
            page = None if self._root is None else self._root.page
        except RuntimeError:
            page = None
        if page is not None:
            page.run_task(self._reload)
        else:
            self.load()
            self._update()

    def _refresh(self, controls):
        async def handler(_event=None):
            market, mode, symbol, horizon, source, date_range = controls
            self.market = Market(market.value)
            self.analysis_mode = mode.value
            self.symbol = symbol.value.strip()
            self.horizon = horizon.value
            self.source = source.value
            self.date_from = date_range.start_value
            self.date_to = date_range.end_value
            await self._reload(force=True)
        return handler

    async def _switch_view(self, event):
        selected = tuple(getattr(event.control, "selected", ()))
        if not selected:
            return
        self.active_view = selected[0]
        self.symbol = "" if self.active_view.startswith("portfolio") else self.symbol
        await self._reload()

    def _toolbar(self):
        single = self.active_view in {"single_forecast", "single_strategy"}
        market = configure_field(ft.Dropdown(
            label="市场", value=self.market.value,
            options=[ft.dropdown.Option("US", "美股"), ft.dropdown.Option("A", "A股")],
            col={"sm": 6, "md": 2},
        ))
        mode = configure_field(ft.Dropdown(
            label="分析模式", value=self.analysis_mode, options=list(_MODE_OPTIONS),
            col={"sm": 6, "md": 2},
        ))
        symbol = configure_field(ft.TextField(
            label="股票代码或公司名", hint_text="MU / 600519 / 公司名",
            value=self.symbol, prefix_icon=ft.Icons.SEARCH,
            col={"sm": 12, "md": 4},
            disabled=not single,
        ))
        horizon = configure_field(ft.Dropdown(
            label="预测周期", value=self.horizon,
            options=[ft.dropdown.Option("all", "全部周期"), *(ft.dropdown.Option(str(item), f"{item}日") for item in (1, 3, 5, 10))],
            col={"sm": 6, "md": 2},
        ))
        source = configure_field(ft.Dropdown(
            label="建议来源", value=self.source,
            options=[ft.dropdown.Option("all", "全部来源"), ft.dropdown.Option("single_stock", "单股分析"), ft.dropdown.Option("portfolio", "我的持仓")],
            col={"sm": 6, "md": 2}, disabled=self.active_view != "single_strategy",
        ))
        date_range = calendar_date_range_field(
            "日期范围", self.date_from, self.date_to, width=260,
        )
        controls = (market, mode, symbol, horizon, source, date_range)
        visible = [market, mode]
        if single:
            visible.append(symbol)
        if self.active_view != "overview":
            visible.append(horizon)
        if self.active_view == "single_strategy":
            visible.append(source)
        return ft.Column([
            ft.ResponsiveRow(
                visible, columns=12, spacing=10, run_spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.Container(
                ft.Row([
                    date_range.control,
                    ft.Text(
                        "留空时汇总统计全部历史，逐次明细只展示最近30天。",
                        size=11, color=TEXT_MUTED, expand=True,
                    ),
                    primary_button("查询", ft.Icons.SEARCH, self._refresh(controls)),
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.Padding.only(top=8),
                border=ft.Border(top=ft.BorderSide(1, BORDER)),
            ),
        ], spacing=8)

    @staticmethod
    def _tables(view):
        return {} if view is None else {item.table_id: item for item in view.tables}

    @staticmethod
    def _status_color(cells):
        text = " ".join(cells)
        if "暂无" in text or "尚无" in text:
            return TEXT_MUTED
        if "负" in text or "错误" in text or any(cell.strip().startswith("-") for cell in cells):
            return DANGER
        if "样本" in text or "观察" in text:
            return WARNING
        return PRIMARY if "预测" in cells[0] else SUCCESS

    def _overview(self, tables):
        table = tables.get("capability_overview")
        if table is None:
            return ft.Text("暂无能力总览", color=TEXT_MUTED)
        cards = []
        for row in table.rows:
            capability, samples, result, meaning, conclusion = row.cells
            color = self._status_color(row.cells)
            cards.append(ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(capability, size=16, weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Container(ft.Text(f"{samples} 个成熟样本", size=10, color=TEXT_MUTED), padding=ft.Padding(7, 3, 7, 3), bgcolor=SURFACE_MUTED, border_radius=4),
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Text(result, size=20, weight=ft.FontWeight.BOLD, color=color),
                    ft.Text(meaning, size=12, color=TEXT),
                    ft.Container(ft.Text(conclusion, size=11, color=TEXT_MUTED), padding=ft.Padding.only(left=9), border=ft.Border(left=ft.BorderSide(3, color))),
                ], spacing=8),
                padding=15, bgcolor=SURFACE,
                border=ft.Border.all(1, BORDER), border_radius=7,
                col={"sm": 12, "lg": 6},
            ))
        mode_table = tables.get("mode_forecast_summary")
        return ft.Column([
            ft.Container(
                content=ft.Column([
                    ft.Text("总体判断", size=13, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text("四项能力分开评价，能直接看出问题出在预测、策略、单股还是组合。", size=12, color=TEXT_MUTED),
                ], spacing=3),
                padding=13, bgcolor="#EAF4F7", border=ft.Border(left=ft.BorderSide(4, PRIMARY)),
            ),
            ft.ResponsiveRow(cards, spacing=12, run_spacing=12),
            *([] if mode_table is None else [table_control(mode_table)]),
        ], spacing=16)

    def _forecast_content(self, tables, *, portfolio=False):
        mode_summary = tables.get("mode_forecast_summary")
        issued = tables.get("issued_forecast_details")
        summary = tables.get("forecast_performance_summary")
        charts = [
            chart_control(chart) for chart in (self.view.charts if self.view else ())
            if chart.chart_kind.value in {"forecast_timeline", "calibration"}
        ]
        controls = [ft.Text(
            "组合预测验证组合内成分股方向；专用强弱排序账尚未形成时会明确提示，不用单股正确率冒充。"
            if portfolio else
            "Tab1和Tab3的同一只股票进入统一预测账，按分析模式分别验证，结果按生成时间倒序。",
            size=12, color=TEXT_MUTED,
        )]
        for table in (mode_summary, summary):
            if table is not None:
                controls.append(table_control(table))
        controls.extend(charts)
        if issued is not None:
            controls.extend((
                ft.Divider(height=1),
                ft.Text("逐次预测记录", size=16, weight=ft.FontWeight.BOLD),
                table_control(issued),
            ))
        return ft.Column(controls, spacing=16)

    def _strategy_content(self, tables, *, portfolio=False):
        source = tables.get("strategy_source_summary")
        summary = tables.get("joint_performance_summary" if portfolio else "strategy_performance_summary")
        exit_summary = None if portfolio else tables.get("strategy_exit_quality_summary")
        details = tables.get("strategy_event_details")
        charts = [
            chart_control(chart) for chart in (self.view.charts if self.view else ())
            if chart.chart_kind.value in {"cumulative_performance", "drawdown"}
        ]
        controls = [ft.Container(
            ft.Text(
                "连续OOF回测衡量系统完整历史能力；实际建议回放只使用用户真实运行时产生的最后有效建议，空窗期不补造新决策。",
                size=12, color=TEXT,
            ),
            padding=12, bgcolor="#EAF4F7", border=ft.Border(left=ft.BorderSide(4, PRIMARY)),
        )]
        controls.append(ft.Text(
            "日期范围只筛选在该期间完成验证的记录，不代表系统从开始日连续持仓到结束日。"
            "入场收益、退出质量和组合账户净值采用不同口径，互不混算。",
            size=11, color=TEXT_MUTED,
        ))
        for table in (source, summary, exit_summary):
            if table is not None:
                controls.append(table_control(table))
        controls.extend(charts)
        if details is not None:
            controls.extend((ft.Divider(height=1), ft.Text("逐次建议明细", size=16, weight=ft.FontWeight.BOLD), table_control(details)))
        return ft.Column(controls, spacing=16)

    def _body(self):
        tables = self._tables(self.view)
        return {
            "overview": lambda: self._overview(tables),
            "single_forecast": lambda: self._forecast_content(tables),
            "single_strategy": lambda: self._strategy_content(tables),
            "portfolio_forecast": lambda: self._forecast_content(tables, portfolio=True),
            "portfolio_strategy": lambda: self._strategy_content(tables, portfolio=True),
        }[self.active_view]()

    def _content(self):
        tabs = ft.SegmentedButton(
            selected=[self.active_view],
            segments=[
                ft.Segment(value="overview", label=ft.Text("能力总览"), icon=ft.Icon(ft.Icons.DASHBOARD_OUTLINED)),
                ft.Segment(value="single_forecast", label=ft.Text("单股预测"), icon=ft.Icon(ft.Icons.TROUBLESHOOT)),
                ft.Segment(value="single_strategy", label=ft.Text("单股策略"), icon=ft.Icon(ft.Icons.SHOW_CHART)),
                ft.Segment(value="portfolio_forecast", label=ft.Text("组合预测"), icon=ft.Icon(ft.Icons.BUBBLE_CHART_OUTLINED)),
                ft.Segment(value="portfolio_strategy", label=ft.Text("组合策略"), icon=ft.Icon(ft.Icons.ACCOUNT_BALANCE_OUTLINED)),
            ],
            on_change=self._switch_view,
        )
        controls = [
            page_heading("系统能力评估", "分别检查预测与策略、单股与组合，明确系统哪里可靠、哪里需要优化"),
            panel(ft.Column([tabs, ft.Divider(height=1), self._toolbar()], spacing=13)),
        ]
        banner = feedback_banner(self.error or self.notice, error=bool(self.error))
        if banner is not None:
            controls.append(banner)
        if self.busy:
            controls.append(panel(ft.Row([
                ft.ProgressRing(width=20, height=20, stroke_width=2, color=PRIMARY),
                ft.Text("正在读取冻结预测、策略与历史验证结果…", color=TEXT_MUTED),
            ], spacing=10)))
        elif self.view is None and self.error is None:
            controls.append(panel(ft.Text("进入能力评估后将自动读取历史结果。", color=TEXT_MUTED)))
        elif self.view is not None:
            controls.append(panel(self._body(), padding=18))
        return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)

    def build(self):
        self._root = ft.Container(content=self._content(), expand=True, padding=24)
        return self._root
