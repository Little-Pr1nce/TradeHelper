"""
组合管理页面。
"""

import flet as ft

from data.database import Database
from services.portfolio_service import PortfolioService


class PortfolioPage(ft.Container):
    """组合创建、持仓维护和批量分析。"""

    def __init__(self):
        super().__init__()
        self._service = PortfolioService()
        self._selected_portfolio_id: int | None = None

    def build(self):
        self._portfolio_name = ft.TextField(label="组合名称", width=180)
        self._portfolio_desc = ft.TextField(label="描述", width=260)
        self._risk_stop = ft.TextField(label="回撤熔断", value="0.08", width=110)
        self._portfolio_dd = ft.Dropdown(label="当前组合", width=220)
        self._portfolio_dd.on_change = self._on_select_portfolio
        self._holding_code = ft.TextField(label="代码", width=120)
        self._holding_name = ft.TextField(label="名称", width=140)
        self._holding_market = ft.Dropdown(
            label="市场", value="US", width=100,
            options=[ft.dropdown.Option("US", "美股"), ft.dropdown.Option("A", "A股")],
        )
        self._holding_industry = ft.TextField(label="行业", width=130)
        self._holding_weight = ft.TextField(label="权重", value="0.0", width=90)
        self._period = ft.Dropdown(
            label="分析周期", value="1y", width=120,
            options=[ft.dropdown.Option("6m", "6个月"), ft.dropdown.Option("1y", "1年"), ft.dropdown.Option("3y", "3年")],
        )
        self._holdings_list = ft.ListView(expand=True, spacing=8, height=260)
        self._result_view = ft.Markdown(value="", selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB)
        self._status = ft.Text("", size=13, color=ft.Colors.BLUE_700)

        self.content = ft.Column(
            expand=True, scroll=ft.ScrollMode.AUTO, spacing=14,
            controls=[
                ft.Text("组合管理", size=24, weight=ft.FontWeight.BOLD),
                ft.Row(wrap=True, spacing=8, controls=[
                    self._portfolio_dd,
                    self._portfolio_name,
                    self._portfolio_desc,
                    self._risk_stop,
                    ft.Button("保存组合", icon=ft.Icons.SAVE, on_click=self._on_save_portfolio),
                    ft.Button("删除组合", icon=ft.Icons.DELETE, on_click=self._on_delete_portfolio),
                ]),
                ft.Divider(),
                ft.Text("持仓/候选池", size=16, weight=ft.FontWeight.W_600),
                ft.Row(wrap=True, spacing=8, controls=[
                    self._holding_code, self._holding_name, self._holding_market,
                    self._holding_industry, self._holding_weight,
                    ft.Button("添加/更新", icon=ft.Icons.ADD, on_click=self._on_add_holding),
                ]),
                self._holdings_list,
                ft.Row(spacing=8, controls=[
                    self._period,
                    ft.Button("批量分析组合", icon=ft.Icons.ANALYTICS, on_click=self._on_analyze),
                    self._status,
                ]),
                ft.Divider(),
                self._result_view,
            ],
        )
        self._load_portfolios()
        return self.content

    def _load_portfolios(self):
        portfolios = Database().list_portfolios()
        self._portfolio_dd.options = [ft.dropdown.Option(str(p.id), p.name) for p in portfolios if p.id]
        if portfolios and not self._selected_portfolio_id:
            self._selected_portfolio_id = portfolios[0].id
            self._portfolio_dd.value = str(portfolios[0].id)
        self._load_holdings()
        try:
            self._portfolio_dd.update()
        except Exception:
            pass

    def _load_holdings(self):
        self._holdings_list.controls.clear()
        if not self._selected_portfolio_id:
            self._holdings_list.controls.append(ft.Text("请先创建或选择组合", color=ft.Colors.GREY_600))
        else:
            holdings = Database().list_portfolio_holdings(self._selected_portfolio_id)
            if not holdings:
                self._holdings_list.controls.append(ft.Text("暂无持仓", color=ft.Colors.GREY_600))
            for holding in holdings:
                self._holdings_list.controls.append(ft.Container(
                    bgcolor=ft.Colors.GREY_100, border_radius=8, padding=10,
                    content=ft.Row(controls=[
                        ft.Text(f"{holding.code} {holding.name}", width=180, weight=ft.FontWeight.BOLD),
                        ft.Text(holding.market, width=60),
                        ft.Text(holding.industry or "未分类", width=120),
                        ft.Text(f"{holding.weight:.1%}", width=80),
                        ft.IconButton(icon=ft.Icons.DELETE, on_click=lambda e, hid=holding.id: self._on_delete_holding(hid)),
                    ]),
                ))
        try:
            self._holdings_list.update()
        except Exception:
            pass

    def _on_select_portfolio(self, e):
        self._selected_portfolio_id = int(self._portfolio_dd.value) if self._portfolio_dd.value else None
        self._load_holdings()

    def _on_save_portfolio(self, e):
        if not self._portfolio_name.value.strip():
            self._set_status("请填写组合名称", error=True)
            return
        try:
            risk_stop = float(self._risk_stop.value or 0.08)
            self._selected_portfolio_id = self._service.create_or_update_portfolio(
                self._portfolio_name.value, self._portfolio_desc.value or "", risk_stop,
            )
            self._set_status("组合已保存")
            self._load_portfolios()
        except Exception as ex:
            self._set_status(f"保存失败：{ex}", error=True)

    def _on_delete_portfolio(self, e):
        if self._selected_portfolio_id:
            Database().delete_portfolio(self._selected_portfolio_id)
            self._selected_portfolio_id = None
            self._portfolio_dd.value = None
            self._set_status("组合已删除")
            self._load_portfolios()

    def _on_add_holding(self, e):
        if not self._selected_portfolio_id:
            self._set_status("请先选择组合", error=True)
            return
        if not self._holding_code.value.strip():
            self._set_status("请填写代码", error=True)
            return
        try:
            self._service.add_or_update_holding(
                portfolio_id=self._selected_portfolio_id,
                code=self._holding_code.value,
                name=self._holding_name.value or self._holding_code.value,
                market=self._holding_market.value,
                industry=self._holding_industry.value or "未分类",
                weight=float(self._holding_weight.value or 0),
            )
            self._set_status("持仓已保存")
            self._load_holdings()
        except Exception as ex:
            self._set_status(f"保存持仓失败：{ex}", error=True)

    def _on_delete_holding(self, holding_id: int | None):
        if holding_id:
            Database().delete_portfolio_holding(holding_id)
            self._load_holdings()

    def _on_analyze(self, e):
        if not self._selected_portfolio_id:
            self._set_status("请先选择组合", error=True)
            return
        try:
            self._set_status("正在批量分析...")
            result = self._service.analyze_portfolio(self._selected_portfolio_id, self._period.value or "1y")
            self._result_view.value = result.summary
            self._result_view.update()
            self._set_status(f"分析完成，快照 #{result.analysis_id}")
        except Exception as ex:
            self._set_status(f"分析失败：{ex}", error=True)

    def _set_status(self, text: str, error: bool = False):
        self._status.value = text
        self._status.color = ft.Colors.RED_500 if error else ft.Colors.BLUE_700
        try:
            self._status.update()
        except Exception:
            pass
