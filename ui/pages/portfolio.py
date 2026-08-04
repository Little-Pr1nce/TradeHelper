from __future__ import annotations

import inspect
import logging

import flet as ft

from contracts import ExportFormat, Market
from ..components.progress_panel import progress_panel
from ..components.report_view import report_view
from ..theme import DANGER, PRIMARY, SURFACE_MUTED, TEXT, TEXT_MUTED, configure_field, feedback_banner, page_heading, panel, primary_button, secondary_button
from ..update_dispatch import rebuild_on_page

logger = logging.getLogger(__name__)


class PortfolioPage:
    """Tab3 portfolio workbench with immutable account/watchlist edits."""

    def __init__(self, editor=None, lookup_port=None, analysis_port=None, export_port=None, account_loader=None, watchlist_loader=None):
        self.document = None
        self.account = None
        self.watchlist_snapshot = None
        self.editor = editor
        self.lookup_port = lookup_port
        self.analysis_port = analysis_port
        self.export_port = export_port
        self.account_loader = account_loader
        self.watchlist_loader = watchlist_loader
        self.analysis_mode = "eod"
        self.history_period = "1y"
        self.progress = None
        self.error = None
        self.running_task_id = None
        self.busy = False
        self.notice = None
        self.selected_market = Market.US
        self.active_section = "account"
        self._root = None

    @property
    def positions(self):
        return [] if self.account is None else list(self.account.positions)

    @property
    def watchlist(self):
        return [] if self.watchlist_snapshot is None else list(self.watchlist_snapshot.instruments)

    @property
    def is_full_width_result(self):
        return self.document is not None

    def _update(self):
        rebuild_on_page(self._root, self._content)

    def set_document(self, document):
        revised = self.document is not None and self.document.report_id != document.report_id
        self.document = document
        self.progress = None
        self.running_task_id = None
        self.busy = False
        self.error = None
        if revised:
            self.notice = "研究员观察已返回，报告已自动更新并保存。"
        else:
            pending = any(
                "正在后台研究" in str(getattr(block, "payload", ""))
                for section in document.sections if section.section_id == "research"
                for block in section.blocks
            )
            self.notice = (
                "组合主报告已完成；外部研究员正在后台分析，返回后本页会自动更新。"
                if pending else "组合分析完成，报告已冻结并保存。"
            )
        self._update()

    def set_account(self, account):
        self.account = account
        if account is not None:
            self.selected_market = account.market
        self._update()

    def set_watchlist(self, snapshot):
        self.watchlist_snapshot = snapshot; self._update()

    def _select_market(self, market):
        selected = market if isinstance(market, Market) else Market(str(market))
        self.selected_market = selected
        self.account = self.account_loader(selected) if self.account_loader else None
        self.watchlist_snapshot = self.watchlist_loader(selected) if self.watchlist_loader else None
        self.document = None
        self.error = None
        self.notice = None
        self._update()

    def _section_changed(self, event):
        selected = tuple(getattr(event.control, "selected", ()))
        if selected and selected[0] in {"account", "watchlist"}:
            self.active_section = selected[0]
            self._update()

    def _market_changed(self, event):
        value = getattr(event.control, "value", None)
        if value is None:
            selected = tuple(getattr(event.control, "selected", ()))
            value = selected[0] if selected else self.selected_market.value
        self._select_market(value)

    def edit_position(self, account, *, instrument, shares, cost_price):
        if self.editor is None:
            raise RuntimeError("portfolio editor is not configured")
        return self.editor.save_position(account, instrument=instrument, shares=shares, cost_price=cost_price)

    def _save_position(self, position, shares_field, cost_field):
        def handler(_event=None):
            try:
                self.account = self.edit_position(self.account, instrument=position.instrument, shares=shares_field.value, cost_price=cost_field.value)
                self.error = None
                self.notice = f"{position.instrument.code} 持仓已更新。"
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    def _remove_position(self, position):
        def handler(_event=None):
            try:
                self.account = self.editor.remove_position(self.account, instrument=position.instrument)
                self.error = None
                self.notice = f"{position.instrument.code} 持仓已删除。"
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    def _save_cash(self, field):
        def handler(_event=None):
            try:
                self.account = self.editor.save_cash(self.account, cash=field.value)
                self.error = None
                self.notice = "账户现金已保存。"
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    def _create_account(self, market, cash):
        def handler(_event=None):
            try:
                if self.editor is None:
                    raise RuntimeError("portfolio editor is not configured")
                self.account = self.editor.create_account(market=market.value, cash=cash.value)
                self.selected_market = self.account.market
                if self.watchlist_loader is not None:
                    self.watchlist_snapshot = self.watchlist_loader(self.selected_market)
                self.error = None
                self.notice = f"{self.account.market.value} 账户已创建。"
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    def _add_position(self, symbol, shares, cost):
        def handler(_event=None):
            try:
                if self.account is None or self.editor is None or self.lookup_port is None:
                    raise RuntimeError("账户编辑或股票检索服务尚未配置")
                matches = tuple(self.lookup_port(self.account.market.value, symbol.value.strip()))
                if not matches:
                    raise ValueError("未找到股票")
                instrument = getattr(matches[0], "instrument", matches[0])
                self.account = self.editor.save_position(
                    self.account, instrument=instrument, shares=shares.value, cost_price=cost.value,
                )
                if instrument in self.watchlist:
                    self.watchlist_snapshot = self.editor.save_watchlist(
                        market=self.account.market,
                        instruments=tuple(item for item in self.watchlist if item != instrument),
                        held_instruments=tuple(item.instrument for item in self.positions),
                    )
                symbol.value = shares.value = cost.value = ""
                self.error = None
                self.notice = f"{instrument.code} 已加入持仓。"
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    def _remove_watch(self, instrument):
        def handler(_event=None):
            try:
                values = tuple(item for item in self.watchlist if item != instrument)
                self.watchlist_snapshot = self.editor.save_watchlist(market=instrument.market, instruments=values, held_instruments=tuple(item.instrument for item in self.positions))
                self.error = None
                self.notice = f"{instrument.code} 已移出关注列表。"
            except Exception as exc:
                self.error = str(exc)
                self.notice = None
            self._update()
        return handler

    def _add_watch(self, field):
        def handler(_event=None):
            try:
                query = field.value.strip()
                values = tuple(self.lookup_port(self.selected_market.value, query)) if self.lookup_port and query else ()
                if not values:
                    raise ValueError("未找到股票")
                instrument = getattr(values[0], "instrument", values[0])
                self.watchlist_snapshot = self.editor.save_watchlist(market=self.selected_market, instruments=(*self.watchlist, instrument), held_instruments=tuple(item.instrument for item in self.positions))
                self.error = None; field.value = ""
                self.notice = f"{instrument.code} 已加入关注列表。"
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    async def _start(self, _event=None):
        if self.account is None:
            self.error = "请先录入真实账户余额和持仓"; self._update(); return
        if self.analysis_port is None:
            self.error = "组合分析服务尚未配置"; self._update(); return
        self.error = None
        self.notice = "正在创建组合分析任务，请稍候。"
        self.busy = True
        self._update()
        method = getattr(self.analysis_port, "start_portfolio", self.analysis_port)
        try:
            result = method({"market":self.account.market.value,"mode":self.analysis_mode,"history_period":self.history_period,"account": self.account, "watchlist": self.watchlist_snapshot}, on_progress=self._on_progress, on_complete=self.set_document, on_error=self._on_error)
            if inspect.isawaitable(result): result = await result
            if hasattr(result, "sections"): self.set_document(result)
            elif isinstance(result, str):
                self.running_task_id = result
                self.notice = "组合分析已启动，逐股进度会持续更新。"
                self._update()
        except Exception as exc:
            self._on_error(exc)

    def _on_progress(self, value):
        self.progress = value
        self.running_task_id = getattr(value, "task_id", self.running_task_id)
        self._update()

    def _on_error(self, error):
        message = str(error) or "组合分析失败"
        if "ANALYSIS_CANCELLED" in message:
            self.error = None
            self.notice = "组合分析已取消。"
        else:
            self.error = message
            self.notice = None
        self.progress = None
        self.running_task_id = None
        self.busy = False
        self._update()

    def _cancel(self, _event=None):
        cancel = getattr(self.analysis_port, "cancel", None)
        if cancel and self.running_task_id: cancel(self.running_task_id)
        self.running_task_id = None
        self.busy = False
        self.progress = None
        self.notice = "已请求取消组合分析。"
        self._update()

    def _back(self, _event=None):
        self.document = None; self._update()

    def _export(self, format):
        def handler(_event=None):
            if self.export_port is None or self.document is None:
                self.error = "导出服务尚未配置"
                self.notice = None
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

    def _account_view(self):
        market_switch = ft.SegmentedButton(
            width=260,
            selected=[self.selected_market.value],
            segments=[
                ft.Segment(value="US", label=ft.Text("美股账户", no_wrap=True), icon=ft.Icon(ft.Icons.PUBLIC, size=16)),
                ft.Segment(value="A", label=ft.Text("A股账户", no_wrap=True), icon=ft.Icon(ft.Icons.SHOW_CHART, size=16)),
            ],
            on_change=self._market_changed,
        )
        account_header = ft.Row([
            ft.Column([
                ft.Text("账户市场", size=12, color=TEXT_MUTED),
                ft.Text("美股与 A 股的现金、持仓和关注列表相互独立", size=11, color=TEXT_MUTED),
            ], spacing=1),
            market_switch,
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=True)
        if self.account is None:
            currency = "CNY" if self.selected_market is Market.A else "USD"
            market_name = "A股" if self.selected_market is Market.A else "美股"
            cash = configure_field(ft.TextField(label=f"真实可用现金 ({currency})", keyboard_type=ft.KeyboardType.NUMBER, width=220))
            return ft.Column([
                account_header,
                ft.Divider(height=1),
                ft.Container(
                    bgcolor="#FFF7E8", border_radius=8, padding=12,
                    content=ft.Row([ft.Icon(ft.Icons.INFO_OUTLINE, color="#A45F00", size=18), ft.Text(f"尚未创建{market_name}账户。请录入真实余额，系统不会填入模拟本金。", color="#774600", expand=True)]),
                ),
                ft.Row([cash, primary_button("创建账户", ft.Icons.ACCOUNT_BALANCE_WALLET, self._create_account(self.selected_market, cash))], wrap=True, spacing=12),
            ], spacing=14)
        cash = configure_field(ft.TextField(label=f"可用现金 ({self.account.currency})", value=str(self.account.cash), keyboard_type=ft.KeyboardType.NUMBER, width=220))
        symbol = configure_field(ft.TextField(label="股票代码或公司名", hint_text="新增持仓", width=240, prefix_icon=ft.Icons.SEARCH))
        new_shares = configure_field(ft.TextField(label="持股数量", width=130, keyboard_type=ft.KeyboardType.NUMBER))
        new_cost = configure_field(ft.TextField(label="成本价", width=130, keyboard_type=ft.KeyboardType.NUMBER))
        rows = []
        for position in self.account.positions:
            shares = configure_field(ft.TextField(value=str(position.shares), label="持股数量", width=150, keyboard_type=ft.KeyboardType.NUMBER))
            cost = configure_field(ft.TextField(value=str(position.cost_price), label="成本价", width=150, keyboard_type=ft.KeyboardType.NUMBER))
            rows.append(ft.Container(
                padding=ft.Padding(12, 8, 8, 8),
                border=ft.Border(bottom=ft.BorderSide(1, "#E8EDF2")),
                content=ft.Row([
                    ft.Container(width=110, content=ft.Column([
                        ft.Text(position.instrument.code, weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Text(position.instrument.market.value, size=11, color=TEXT_MUTED),
                    ], spacing=0)),
                    shares,
                    cost,
                    ft.IconButton(ft.Icons.SAVE_OUTLINED, tooltip="保存编辑", icon_color=PRIMARY, on_click=self._save_position(position, shares, cost)),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="删除持仓", icon_color=DANGER, on_click=self._remove_position(position)),
                ], scroll=ft.ScrollMode.AUTO, spacing=10),
            ))
        return ft.Column([
            account_header,
            ft.Divider(height=1),
            ft.Row([
                ft.Column([ft.Text("账户现金", size=12, color=TEXT_MUTED), ft.Text(f"{self.account.market.value} · {self.account.currency}", weight=ft.FontWeight.BOLD)], spacing=1),
                ft.Row([cash, ft.IconButton(ft.Icons.SAVE_OUTLINED, tooltip="保存现金", icon_color=PRIMARY, on_click=self._save_cash(cash))]),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
            ft.Divider(height=1),
            ft.Text("新增持仓", size=13, weight=ft.FontWeight.BOLD),
            ft.Row([symbol, new_shares, new_cost, ft.IconButton(ft.Icons.ADD_CIRCLE, tooltip="新增持仓", icon_color=PRIMARY, on_click=self._add_position(symbol, new_shares, new_cost))], wrap=True, spacing=10),
            ft.Container(
                bgcolor=SURFACE_MUTED,
                padding=ft.Padding(12, 8, 12, 8),
                content=ft.Row([ft.Text("标的", width=110, weight=ft.FontWeight.BOLD), ft.Text("持股数量", width=150, weight=ft.FontWeight.BOLD), ft.Text("成本价", width=150, weight=ft.FontWeight.BOLD), ft.Text("操作", weight=ft.FontWeight.BOLD)], scroll=ft.ScrollMode.AUTO),
            ),
            *rows,
            *([] if rows else [ft.Text("尚未录入持仓", color=TEXT_MUTED)]),
        ], spacing=12)

    def _watchlist_view(self):
        field = configure_field(ft.TextField(label="股票代码或公司名", hint_text="添加到关注列表", width=320, prefix_icon=ft.Icons.SEARCH))
        rows = [ft.Container(
            bgcolor=SURFACE_MUTED,
            border_radius=6,
            padding=ft.Padding(12, 7, 6, 7),
            content=ft.Row([
                ft.Row([ft.Icon(ft.Icons.STAR_OUTLINE, size=18, color="#B26A00"), ft.Text(item.code, weight=ft.FontWeight.BOLD)], spacing=8),
                ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="移除关注", icon_color=DANGER, on_click=self._remove_watch(item)),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ) for item in self.watchlist]
        return ft.Column([
            ft.Row([field, ft.IconButton(ft.Icons.ADD_CIRCLE, tooltip="添加关注", icon_color=PRIMARY, on_click=self._add_watch(field))], wrap=True),
            *rows,
            *([] if rows else [ft.Text("关注列表为空", color=TEXT_MUTED)]),
        ], spacing=8)

    def _input_content(self):
        market=configure_field(ft.Dropdown(label="账户/市场",value=self.selected_market.value,options=[ft.dropdown.Option("US","美股"),ft.dropdown.Option("A","A股")],width=140))
        mode=configure_field(ft.Dropdown(label="分析时段",value=self.analysis_mode,options=[ft.dropdown.Option("pre","盘前"),ft.dropdown.Option("intraday","盘中"),ft.dropdown.Option("eod","盘后")],width=145))
        period=configure_field(ft.Dropdown(label="历史窗口",value=self.history_period,options=[ft.dropdown.Option("1m","1个月"),ft.dropdown.Option("3m","3个月"),ft.dropdown.Option("6m","6个月"),ft.dropdown.Option("1y","1年")],width=135))
        market.on_select=self._market_changed
        mode.on_select=lambda e:setattr(self,"analysis_mode",e.control.value)
        period.on_select=lambda e:setattr(self,"history_period",e.control.value)
        section_switch = ft.SegmentedButton(
            selected=[self.active_section],
            segments=[
                ft.Segment(value="account", label=ft.Text("账户与持仓"), icon=ft.Icon(ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED)),
                ft.Segment(value="watchlist", label=ft.Text("关注列表"), icon=ft.Icon(ft.Icons.STAR_OUTLINE)),
            ],
            on_change=self._section_changed,
        )
        section_content = {
            "account": self._account_view,
            "watchlist": self._watchlist_view,
        }[self.active_section]()
        actions=[
            primary_button("分析运行中" if self.busy else "开始组合分析",ft.Icons.HOURGLASS_TOP if self.busy else ft.Icons.PLAY_ARROW,self._start,disabled=self.busy),
            secondary_button("取消",ft.Icons.STOP_CIRCLE_OUTLINED,self._cancel,disabled=self.running_task_id is None),
        ]
        controls = [
            page_heading("我的持仓", "维护真实账户与关注列表，按组合风险容量生成调仓计划", actions),
            panel(ft.Column([
                ft.Row([
                    ft.Column([ft.Text("组合分析条件", size=15, weight=ft.FontWeight.BOLD), ft.Text("同时考虑现金、持仓成本、集中度与替换机会", size=11, color=TEXT_MUTED)], spacing=1),
                    ft.Row([market,mode,period],wrap=True,spacing=10),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
                ft.Divider(height=1),
                ft.Text("默认使用 1 年历史数据完成样本外验证；较短窗口可能只生成技术观察，不能作为已验证预测。", size=11, color=TEXT_MUTED),
                section_switch,
                ft.Divider(height=1),
                section_content,
            ], spacing=14)),
        ]
        banner = feedback_banner(self.error or self.notice, error=bool(self.error))
        if banner is not None:
            controls.insert(1, banner)
        if self.progress:
            controls.insert(-1, progress_panel(self.progress))
        return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)

    def _result_content(self):
        toolbar = page_heading("组合分析报告", "先处理持仓风险，再比较关注股与替换机会", [
            ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回组合编辑", on_click=self._back),
            ft.IconButton(ft.Icons.DESCRIPTION_OUTLINED, tooltip="导出 Markdown", on_click=self._export(ExportFormat.MARKDOWN)),
            ft.IconButton(ft.Icons.HTML, tooltip="导出 HTML", on_click=self._export(ExportFormat.HTML)),
            ft.IconButton(ft.Icons.PICTURE_AS_PDF, tooltip="导出 PDF", on_click=self._export(ExportFormat.PDF)),
        ])
        controls = [toolbar]
        banner = feedback_banner(self.error or self.notice, error=bool(self.error))
        if banner is not None:
            controls.append(banner)
        controls.append(report_view(self.document))
        return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=14)

    def _content(self):
        return self._result_content() if self.document else self._input_content()

    def build(self):
        self._root = ft.Container(content=self._content(), expand=True, padding=24)
        return self._root
