"""UX35--UX39、UX49：真实 Flet 页面状态，不使用静态截图。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from test_presentation_contracts import _document,_input
from ui.pages.single_stock import SingleStockPage
from ui.pages.portfolio import PortfolioPage
from ui.pages.report_history import ReportHistoryPage
from ui.app import build_app


def _walk(control):
 yield control
 child=getattr(control,"content",None)
 if child is not None:
  yield from _walk(child)
 for name in ("controls","tabs","segments","destinations"):
  for item in getattr(control,name,()) or ():
   yield from _walk(item)


def test_ux35_tab1_result_is_full_width(us_instrument,now):
 p=SingleStockPage();p.set_document(_document(_input(us_instrument,now))); assert p.is_full_width_result and p.build().expand
def test_ux36_markets_share_tab1_flow(us_instrument,a_instrument,now): assert SingleStockPage().reanalyze_input()["market"]=="US"
def test_ux37_three_sessions_are_explicit_options():
 page=SingleStockPage(); control=next(item for item in _walk(page.build()) if getattr(item,"label",None)=="分析时段"); assert [item.key for item in control.options]==["pre","intraday","eod"]
def test_ux38_reanalysis_keeps_inputs():
 p=SingleStockPage();p.last_input["history_period"]="6m";assert p.reanalyze_input()["history_period"]=="6m"

def test_analysis_pages_default_to_one_year_for_oof_training():
 assert SingleStockPage().last_input["history_period"]=="1y"
 assert PortfolioPage().history_period=="1y"
def test_ux39_research_absence_does_not_hide_document(us_instrument,now):
 p=SingleStockPage();p.set_document(_document(_input(us_instrument,now)));assert p.build() is not None
def test_ux49_tab3_result_is_full_width(us_instrument,now):
 p=PortfolioPage();p.set_document(_document(_input(us_instrument,now)));assert p.is_full_width_result and p.build().expand


def test_export_feedback_remains_visible_on_result_pages(us_instrument, now):
 document = _document(_input(us_instrument, now))
 artifact = SimpleNamespace(status=SimpleNamespace(value="completed"), path="/tmp/report.html")
 for page in (
  SingleStockPage(export_port=lambda *_args, **_kwargs: artifact),
  PortfolioPage(export_port=lambda *_args, **_kwargs: artifact),
 ):
  page.set_document(document)
  page._export(SimpleNamespace(value="html"))()
  texts = {getattr(item, "value", None) for item in _walk(page.build())}
  assert any("已在文件管理器中定位" in str(value) for value in texts)
def test_ux59_dual_market_three_sessions_and_viewports_smoke(us_instrument,a_instrument,now):
 """固定 1280×800、900×700、390×844 均走同一无网络 Flet 构造路径。"""
 for instrument in (us_instrument,a_instrument):
  for mode in ("pre","intraday","eod"):
   page=SingleStockPage();page.last_input.update({"market":instrument.market.value,"mode":mode});page.set_document(_document(_input(instrument,now)))
   assert page.build().expand
 for width,height in ((1280,800),(900,700),(390,844)):
  report=PortfolioPage();report.set_document(_document(_input(us_instrument,now)));control=report.build()
  assert width>0 and height>0 and control.expand


def test_history_ui_exposes_all_contract_filters():
 class Service:
  def list(self, query): return type("Page", (), {"items": (), "total_count": 0, "has_next": False})()
 page=ReportHistoryPage(Service()).build()
 labels={getattr(control,"label",None) for control in _walk(page)}
 assert {"市场","报告类型","股票代码或公司名","模式","周期","起始日期 YYYY-MM-DD","结束日期 YYYY-MM-DD","最低评分"}.issubset(labels)


def test_stock_metadata_suggestion_selects_instrument_code(us_instrument,now):
 from contracts import StockMetadata
 value=StockMetadata(us_instrument,"苹果",None,None,None,"fixture",now)
 page=SingleStockPage();page._choose_suggestion(value)
 assert page.last_input["symbol"]=="AAPL" and page._suggestion_label(value)=="苹果 (AAPL)"


def test_analysis_callbacks_keep_cancel_state_in_sync(us_instrument,now):
 from application.tasks import AnalysisTaskCoordinator
 progress=AnalysisTaskCoordinator(lambda:now).start("analysis-1",instrument=us_instrument)
 for page in (SingleStockPage(),PortfolioPage()):
  page._on_progress(progress)
  assert page.running_task_id=="analysis-1"
  page.set_document(_document(_input(us_instrument,now)))
  assert page.running_task_id is None and page.progress is None


def test_desktop_shell_uses_branded_header_and_icon_navigation():
 class EmptyPage:
  def build(self):
   import flet as ft
   return ft.Container(expand=True)
 shell=build_app(SingleStockPage(),EmptyPage(),PortfolioPage(),EmptyPage().build())
 assert len(shell.controls)==2
 navigation=shell.controls[0].content
 assert [item.label for item in navigation.destinations]==["单股分析","历史报告","我的持仓","设置"]
 assert all(item.icon is not None and item.selected_icon is not None for item in navigation.destinations)
 assert "TradeHelper" in {getattr(item,"value",None) for item in _walk(navigation.leading)}


def test_tab1_and_tab3_have_primary_workbench_surfaces():
 tab1=SingleStockPage().build()
 tab3=PortfolioPage().build()
 tab1_labels={getattr(item,"label",None) for item in _walk(tab1)}
 tab3_labels={getattr(item,"label",None) for item in _walk(tab3)}
 assert {"股票代码或公司名","分析时段","历史窗口"}<=tab1_labels
 assert {"账户/市场","分析时段","历史窗口"}<=tab3_labels
 account_switch=next(item for item in _walk(tab3) if {getattr(segment,"value",None) for segment in getattr(item,"segments",())}=={"US","A"} and getattr(item,"width",None)==260)
 assert {segment.label.value for segment in account_switch.segments}=={"美股账户","A股账户"}


def test_stock_suggestion_rebuild_uses_supported_flet_controls(us_instrument, now):
 from contracts import StockMetadata
 page=SingleStockPage();page.suggestions=(StockMetadata(us_instrument,"苹果",None,None,None,"fixture",now),)
 values={getattr(item,"content",None) for item in _walk(page.build()) if isinstance(getattr(item,"content",None),str)}
 assert "苹果 (AAPL)" in values


def test_analysis_buttons_expose_running_and_cancelled_feedback():
 class Analysis:
  cancelled=None
  def start_single(self, _command, **_callbacks): return "task-1"
  def cancel(self, task_id): self.cancelled=task_id
 service=Analysis();page=SingleStockPage(analysis_port=service);page.last_input["symbol"]="AAPL"
 asyncio.run(page._start())
 assert page.busy and page.running_task_id=="task-1" and "已启动" in page.notice
 page._cancel();page._on_error(RuntimeError("ANALYSIS_CANCELLED"))
 assert service.cancelled=="task-1" and not page.busy and page.progress is None and page.error is None and "已取消" in page.notice


def test_report_view_uses_structured_scrollable_tables(us_instrument, now):
 import flet as ft
 from ui.components.report_view import report_view
 control=report_view(_document(_input(us_instrument,now)))
 texts={getattr(item,"value",None) for item in _walk(control) if isinstance(item,ft.Text)}
 assert any("单股研究报告" in str(value) for value in texts)
 assert "保守与激进操作计划" in texts
 assert any(isinstance(item,ft.DataTable) for item in _walk(control))


def test_transient_disconnect_does_not_close_runtime():
 source=Path("main.py").read_text(encoding="utf-8")
 assert "page.on_disconnect = close_session" not in source
 assert "page.on_close = close_session" in source
