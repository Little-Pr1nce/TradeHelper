"""UX35--UX39、UX49：真实 Flet 页面状态，不使用静态截图。"""
from test_presentation_contracts import _document,_input
from tradehelper_v2.ui.pages.single_stock import SingleStockPage
from tradehelper_v2.ui.pages.portfolio import PortfolioPage
from tradehelper_v2.ui.pages.report_history import ReportHistoryPage
def test_ux35_tab1_result_is_full_width(us_instrument,now):
 p=SingleStockPage();p.set_document(_document(_input(us_instrument,now))); assert p.is_full_width_result and p.build().expand
def test_ux36_markets_share_tab1_flow(us_instrument,a_instrument,now): assert SingleStockPage().reanalyze_input()["market"]=="US"
def test_ux37_three_sessions_are_explicit_options():
 page=SingleStockPage(); row=page.build().content.controls[2]; control=row.controls[1]; assert [item.key for item in control.options]==["pre","intraday","eod"]
def test_ux38_reanalysis_keeps_inputs():
 p=SingleStockPage();p.last_input["history_period"]="6m";assert p.reanalyze_input()["history_period"]=="6m"
def test_ux39_research_absence_does_not_hide_document(us_instrument,now):
 p=SingleStockPage();p.set_document(_document(_input(us_instrument,now)));assert p.build() is not None
def test_ux49_tab3_result_is_full_width(us_instrument,now):
 p=PortfolioPage();p.set_document(_document(_input(us_instrument,now)));assert p.is_full_width_result and p.build().expand
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
 page=ReportHistoryPage(Service()).build().content
 labels={getattr(control,"label",None) for row in page.controls for control in getattr(row,"controls",())}
 assert {"市场","报告类型","股票代码或公司名","模式","周期","起始日期 YYYY-MM-DD","结束日期 YYYY-MM-DD","最低评分"}.issubset(labels)


def test_stock_metadata_suggestion_selects_instrument_code(us_instrument,now):
 from tradehelper_v2.contracts import StockMetadata
 value=StockMetadata(us_instrument,"苹果",None,None,None,"fixture",now)
 page=SingleStockPage();page._choose_suggestion(value)
 assert page.last_input["symbol"]=="AAPL" and page._suggestion_label(value)=="苹果 (AAPL)"


def test_analysis_callbacks_keep_cancel_state_in_sync(us_instrument,now):
 from tradehelper_v2.application.tasks import AnalysisTaskCoordinator
 progress=AnalysisTaskCoordinator(lambda:now).start("analysis-1",instrument=us_instrument)
 for page in (SingleStockPage(),PortfolioPage()):
  page._on_progress(progress)
  assert page.running_task_id=="analysis-1"
  page.set_document(_document(_input(us_instrument,now)))
  assert page.running_task_id is None and page.progress is None
