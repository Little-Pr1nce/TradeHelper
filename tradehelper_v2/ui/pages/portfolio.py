from __future__ import annotations

import inspect

import flet as ft

from tradehelper_v2.contracts import ExportFormat, HistoricalEvaluationQuery, LedgerViewKind, Market
from ..components.evaluation_charts import evaluation_charts
from ..components.progress_panel import progress_panel
from ..components.report_view import report_view


class PortfolioPage:
    """Tab3 portfolio workbench with immutable account/watchlist edits."""

    def __init__(self, editor=None, lookup_port=None, analysis_port=None, export_port=None, evaluation_port=None, account_loader=None, watchlist_loader=None):
        self.document = None
        self.account = None
        self.watchlist_snapshot = None
        self.editor = editor
        self.lookup_port = lookup_port
        self.analysis_port = analysis_port
        self.export_port = export_port
        self.evaluation_port = evaluation_port
        self.account_loader = account_loader
        self.watchlist_loader = watchlist_loader
        self.analysis_mode = "eod"
        self.history_period = "3m"
        self.progress = None
        self.error = None
        self.evaluation_view = None
        self.running_task_id = None
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
        if self._root is not None and self._root.page is not None:
            self._root.content = self._content(); self._root.update()

    def set_document(self, document):
        self.document = document; self.progress = None; self.running_task_id = None; self._update()

    def set_account(self, account):
        self.account = account; self._update()

    def set_watchlist(self, snapshot):
        self.watchlist_snapshot = snapshot; self._update()

    def set_evaluation(self, view):
        self.evaluation_view = view; self._update()

    def edit_position(self, account, *, instrument, shares, cost_price):
        if self.editor is None:
            raise RuntimeError("portfolio editor is not configured")
        return self.editor.save_position(account, instrument=instrument, shares=shares, cost_price=cost_price)

    def _save_position(self, position, shares_field, cost_field):
        def handler(_event=None):
            try:
                self.account = self.edit_position(self.account, instrument=position.instrument, shares=shares_field.value, cost_price=cost_field.value)
                self.error = None
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    def _remove_position(self, position):
        def handler(_event=None):
            try:
                self.account = self.editor.remove_position(self.account, instrument=position.instrument)
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    def _save_cash(self, field):
        def handler(_event=None):
            try:
                self.account = self.editor.save_cash(self.account, cash=field.value)
                self.error = None
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
                self.error = None
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
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    def _remove_watch(self, instrument):
        def handler(_event=None):
            values = tuple(item for item in self.watchlist if item != instrument)
            self.watchlist_snapshot = self.editor.save_watchlist(market=instrument.market, instruments=values, held_instruments=tuple(item.instrument for item in self.positions))
            self._update()
        return handler

    def _add_watch(self, field):
        def handler(_event=None):
            try:
                query = field.value.strip()
                values = tuple(self.lookup_port(self.account.market.value, query)) if self.lookup_port and query else ()
                if not values:
                    raise ValueError("未找到股票")
                instrument = getattr(values[0], "instrument", values[0])
                self.watchlist_snapshot = self.editor.save_watchlist(market=self.account.market, instruments=(*self.watchlist, instrument), held_instruments=tuple(item.instrument for item in self.positions))
                self.error = None; field.value = ""
            except Exception as exc:
                self.error = str(exc)
            self._update()
        return handler

    async def _start(self, _event=None):
        if self.account is None:
            self.error = "请先录入真实账户余额和持仓"; self._update(); return
        if self.analysis_port is None:
            self.error = "组合分析服务尚未配置"; self._update(); return
        method = getattr(self.analysis_port, "start_portfolio", self.analysis_port)
        try:
            result = method({"market":self.account.market.value,"mode":self.analysis_mode,"history_period":self.history_period,"account": self.account, "watchlist": self.watchlist_snapshot}, on_progress=self._on_progress, on_complete=self.set_document, on_error=self._on_error)
            if inspect.isawaitable(result): result = await result
            if hasattr(result, "sections"): self.set_document(result)
            elif isinstance(result, str): self.running_task_id = result
        except Exception as exc:
            self._on_error(exc)

    def _on_progress(self, value):
        self.progress = value
        self.running_task_id = getattr(value, "task_id", self.running_task_id)
        self._update()

    def _on_error(self, error):
        self.error = str(error); self.running_task_id = None; self._update()

    def _cancel(self, _event=None):
        cancel = getattr(self.analysis_port, "cancel", None)
        if cancel and self.running_task_id: cancel(self.running_task_id)
        self.running_task_id = None; self._update()

    def _back(self, _event=None):
        self.document = None; self._update()

    def _export(self, format):
        return lambda _event=None: self.export_port(self.document, format=format) if self.export_port and self.document else None

    def _account_view(self):
        if self.account is None:
            market = ft.Dropdown(label="市场", value="US", options=[ft.dropdown.Option("US", "美股"), ft.dropdown.Option("A", "A股")], width=130)
            cash = ft.TextField(label="真实可用现金", keyboard_type=ft.KeyboardType.NUMBER, width=220)
            return ft.Column([
                ft.Text("首次使用请创建真实账户。系统不会填入模拟本金。", color=ft.Colors.BLUE_GREY_700),
                ft.Row([market, cash, ft.Button("创建账户", icon=ft.Icons.ACCOUNT_BALANCE_WALLET, on_click=self._create_account(market, cash))], wrap=True),
            ])
        cash = ft.TextField(label=f"现金 ({self.account.currency})", value=str(self.account.cash), keyboard_type=ft.KeyboardType.NUMBER, width=220)
        symbol = ft.TextField(label="新增持仓代码或公司名", width=220)
        new_shares = ft.TextField(label="持股数量", width=130, keyboard_type=ft.KeyboardType.NUMBER)
        new_cost = ft.TextField(label="成本价", width=130, keyboard_type=ft.KeyboardType.NUMBER)
        rows = []
        for position in self.account.positions:
            shares = ft.TextField(value=str(position.shares), label="持股数量", width=140, keyboard_type=ft.KeyboardType.NUMBER)
            cost = ft.TextField(value=str(position.cost_price), label="成本价", width=140, keyboard_type=ft.KeyboardType.NUMBER)
            rows.append(ft.Row([ft.Text(position.instrument.code, width=90, weight=ft.FontWeight.BOLD), shares, cost, ft.IconButton(ft.Icons.SAVE, tooltip="保存编辑", on_click=self._save_position(position, shares, cost)), ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="删除持仓", on_click=self._remove_position(position))], scroll=ft.ScrollMode.AUTO))
        return ft.Column([
            ft.Row([cash, ft.IconButton(ft.Icons.SAVE, tooltip="保存现金", on_click=self._save_cash(cash))]),
            ft.Row([symbol, new_shares, new_cost, ft.IconButton(ft.Icons.ADD, tooltip="新增持仓", on_click=self._add_position(symbol, new_shares, new_cost))], wrap=True),
            *rows,
        ], spacing=8, scroll=ft.ScrollMode.AUTO)

    def _watchlist_view(self):
        field = ft.TextField(label="添加股票代码或公司名", width=300)
        rows = [ft.Row([ft.Text(item.code, width=120), ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="移除关注", on_click=self._remove_watch(item))]) for item in self.watchlist]
        return ft.Column([ft.Row([field, ft.IconButton(ft.Icons.ADD, tooltip="添加关注", on_click=self._add_watch(field))], wrap=True), *rows], spacing=8)

    def _evaluation_view(self):
        market = ft.Dropdown(label="市场", value=(self.account.market.value if self.account else "US"), options=[ft.dropdown.Option("US", "美股"), ft.dropdown.Option("A", "A股")], width=125)
        ledger = ft.Dropdown(label="账本", value="all", options=[ft.dropdown.Option("all", "全部"), ft.dropdown.Option("forecast", "预测"), ft.dropdown.Option("strategy", "策略"), ft.dropdown.Option("joint", "联合"), ft.dropdown.Option("research", "研究")], width=125)
        instrument = ft.TextField(label="股票代码或公司名（可选）", width=220)
        horizon = ft.Dropdown(label="周期", value="all", options=[ft.dropdown.Option("all", "全部"), *(ft.dropdown.Option(str(item), f"{item}日") for item in (1, 3, 5, 10))], width=110)

        def refresh(_event=None):
            try:
                selected_market = Market(market.value)
                selected_instrument = None
                if instrument.value.strip():
                    if self.lookup_port is None:
                        raise ValueError("未配置股票检索服务")
                    matches = tuple(self.lookup_port(selected_market.value, instrument.value.strip()))
                    if not matches:
                        raise ValueError("未找到股票")
                    selected_instrument = getattr(matches[0], "instrument", matches[0])
                query = HistoricalEvaluationQuery(
                    selected_market,
                    None if ledger.value == "all" else LedgerViewKind(ledger.value),
                    selected_instrument,
                    None if horizon.value == "all" else int(horizon.value),
                )
                if self.evaluation_port is None:
                    raise ValueError("未配置历史评估服务")
                method = getattr(self.evaluation_port, "load", self.evaluation_port)
                self.evaluation_view = method(query)
                self.error = None
            except Exception as exc:
                self.error = str(exc)
            self._update()

        toolbar = ft.Row([market, ledger, instrument, horizon, ft.IconButton(ft.Icons.REFRESH, tooltip="刷新历史评估", on_click=refresh)], wrap=True)
        if self.evaluation_view is None:
            return ft.Column([toolbar, ft.Text("暂无历史评估读模型。运行分析后，成熟结果将在这里按市场和股票独立展示。")])
        tables = []
        from ..components.report_view import table_control
        for table in self.evaluation_view.tables:
            tables.append(table_control(table))
        return ft.Column([toolbar, evaluation_charts(self.evaluation_view), *tables], expand=True, scroll=ft.ScrollMode.AUTO)

    def _input_content(self):
        selected_market=self.account.market.value if self.account else "US"
        market=ft.Dropdown(label="市场",value=selected_market,options=[ft.dropdown.Option("US","美股"),ft.dropdown.Option("A","A股")],width=120)
        mode=ft.Dropdown(label="分析模式",value=self.analysis_mode,options=[ft.dropdown.Option("pre","盘前"),ft.dropdown.Option("intraday","盘中"),ft.dropdown.Option("eod","盘后")],width=140)
        period=ft.Dropdown(label="回看周期",value=self.history_period,options=[ft.dropdown.Option("1m","1个月"),ft.dropdown.Option("3m","3个月"),ft.dropdown.Option("6m","6个月"),ft.dropdown.Option("1y","1年")],width=130)
        def change_market(event):
            selected=Market(event.control.value)
            self.account=self.account_loader(selected) if self.account_loader else None
            self.watchlist_snapshot=self.watchlist_loader(selected) if self.watchlist_loader else None
            self.document=None; self._update()
        market.on_select=change_market
        mode.on_select=lambda e:setattr(self,"analysis_mode",e.control.value)
        period.on_select=lambda e:setattr(self,"history_period",e.control.value)
        tabs = ft.Tabs(
            content=ft.Column(
                [
                    ft.TabBar(tabs=[ft.Tab(label="账户与持仓"), ft.Tab(label="关注列表"), ft.Tab(label="历史评估")]),
                    ft.TabBarView(
                        controls=[self._account_view(), self._watchlist_view(), self._evaluation_view()],
                        expand=True,
                    ),
                ],
                expand=True,
            ),
            length=3,
            selected_index=0,
            expand=True,
        )
        controls = [ft.Text("我的持仓", theme_style=ft.TextThemeStyle.HEADLINE_SMALL),ft.Row([market,mode,period],wrap=True)]
        if self.error: controls.append(ft.Text(self.error, color=ft.Colors.RED_700))
        controls.extend([ft.Row([ft.Button("开始组合分析", icon=ft.Icons.PLAY_ARROW, on_click=self._start), ft.Button("取消", icon=ft.Icons.CANCEL_OUTLINED, on_click=self._cancel, disabled=self.running_task_id is None)]), tabs])
        if self.progress: controls.insert(-1, progress_panel(self.progress))
        return ft.Column(controls, expand=True, spacing=10)

    def _result_content(self):
        toolbar = ft.Row([ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回组合编辑", on_click=self._back), ft.IconButton(ft.Icons.DESCRIPTION_OUTLINED, tooltip="导出 Markdown", on_click=self._export(ExportFormat.MARKDOWN)), ft.IconButton(ft.Icons.HTML, tooltip="导出 HTML", on_click=self._export(ExportFormat.HTML)), ft.IconButton(ft.Icons.PICTURE_AS_PDF, tooltip="导出 PDF", on_click=self._export(ExportFormat.PDF))])
        return ft.Column([toolbar, report_view(self.document)], expand=True)

    def _content(self):
        return self._result_content() if self.document else self._input_content()

    def build(self):
        self._root = ft.Container(content=self._content(), expand=True, padding=16)
        return self._root
