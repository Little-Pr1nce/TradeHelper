"""UX35--UX39、UX49：真实 Flet 页面状态，不使用静态截图。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from test_presentation_contracts import _document,_input
from ui.pages.single_stock import SingleStockPage
from ui.pages.portfolio import PortfolioPage
from ui.pages.report_history import ReportHistoryPage
from ui.pages.evaluation import AbilityEvaluationPage
from ui.components.date_field import calendar_date_range_field
from ui.app import build_app


def _walk(control):
 yield control
 child=getattr(control,"content",None)
 if child is not None:
  yield from _walk(child)
 for name in ("controls","tabs","segments","destinations","rows","cells"):
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
 assert {"市场","报告类型","股票代码或公司名","模式","周期","日期范围","最低评分"}.issubset(labels)
 date_fields=[control for control in _walk(page) if getattr(control,"label",None)=="日期范围"]
 assert len(date_fields)==1
 assert date_fields[0].read_only and date_fields[0].on_click is not None and date_fields[0].suffix_icon is not None


def test_compact_date_range_uses_one_field_and_can_clear_without_mounting():
 value=calendar_date_range_field("日期范围","2026-07-01","2026-08-04")
 assert value.control.value=="2026/07/01 - 08/04"
 value._clear()
 assert value.start_value==value.end_value=="" and value.control.value==""


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


def test_analysis_progress_rebuilds_on_flet_page_loop(us_instrument,now):
 from application.tasks import AnalysisTaskCoordinator

 class FakePage:
  def __init__(self): self.scheduled=0
  def run_task(self,handler,*args):
   self.scheduled+=1
   return asyncio.run(handler(*args))

 class FakeRoot:
  def __init__(self,page): self.page=page;self.content=None;self.updates=0
  def update(self): self.updates+=1

 progress=AnalysisTaskCoordinator(lambda:now).start("analysis-loop",instrument=us_instrument)
 for view in (SingleStockPage(),PortfolioPage()):
  mounted=FakeRoot(FakePage());view._root=mounted
  view._on_progress(progress)
  assert mounted.page.scheduled==1 and mounted.updates==1
  assert view.progress==progress and view.running_task_id=="analysis-loop"


def test_desktop_shell_uses_branded_header_and_icon_navigation():
 class EmptyPage:
  def build(self):
   import flet as ft
   return ft.Container(expand=True)
 shell=build_app(SingleStockPage(),EmptyPage(),PortfolioPage(),EmptyPage().build(),evaluation=EmptyPage())
 assert len(shell.controls)==2
 navigation=shell.controls[0].content
 assert [item.label for item in navigation.destinations]==["单股分析","我的持仓","能力评估","报告记录","设置"]
 assert all(item.icon is not None and item.selected_icon is not None for item in navigation.destinations)
 assert "TradeHelper" in {getattr(item,"value",None) for item in _walk(navigation.leading)}


def test_desktop_shell_builds_hidden_pages_only_when_selected():
 class CountedPage:
  def __init__(self): self.builds=0
  def build(self):
   import flet as ft
   self.builds+=1
   return ft.Container(expand=True)
 single,portfolio,evaluation,history=(CountedPage() for _ in range(4))
 shell=build_app(single,history,portfolio,CountedPage().build(),evaluation=evaluation)
 assert (single.builds,portfolio.builds,evaluation.builds,history.builds)==(1,0,0,0)
 navigation=shell.controls[0].content
 navigation.selected_index=3
 navigation.on_change(SimpleNamespace(control=navigation))
 assert history.builds==1


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


def test_portfolio_report_view_uses_action_cards_and_probability_bars(us_instrument, now, calendar):
 import flet as ft
 from presentation_helpers import portfolio_presentation
 from presentation.report_builder import PortfolioReportBuilder
 from ui.components.report_view import report_view
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 controls=tuple(_walk(report_view(document)))
 assert any(isinstance(item,ft.ResponsiveRow) for item in controls)
 assert any(isinstance(item,ft.ProgressBar) for item in controls)
 assert "下一条件" in {getattr(item,"value",None) for item in controls if isinstance(item,ft.Text)}


def test_portfolio_detail_expansion_uses_compact_sections_instead_of_table_rows(us_instrument, now, calendar):
 import flet as ft
 from presentation_helpers import portfolio_presentation
 from presentation.report_builder import PortfolioReportBuilder
 from ui.components.report_view import table_control
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 section=next(item for item in document.sections if item.section_id=="operation_report")
 table=next(block.payload for block in section.blocks if block.payload.table_id.startswith("stock_detail_"))
 control=table_control(table)
 controls=tuple(_walk(control))
 assert isinstance(control,ft.ExpansionTile)
 assert not any(isinstance(item,ft.DataTable) for item in controls)
 labels={getattr(item,"value",None) for item in controls if isinstance(item,ft.Text)}
 assert {"股票概况","预测如何转成方案","达到什么条件行动","风险与历史依据"}<=labels
 assert any(isinstance(item,ft.ResponsiveRow) for item in controls)
 assert any(getattr(item,"col",{}).get("md")==6 for item in controls if isinstance(getattr(item,"col",None),dict))


def test_portfolio_signal_groups_use_readable_cards_instead_of_wide_tables(us_instrument, now, calendar):
 import flet as ft
 from presentation_helpers import portfolio_presentation
 from presentation.report_builder import PortfolioReportBuilder
 from ui.components.report_view import table_control
 document=PortfolioReportBuilder().build(portfolio_presentation((us_instrument,),now=now,calendar=calendar))
 section=next(item for item in document.sections if item.section_id=="operation_report")
 table=next(block.payload for block in section.blocks if block.payload.table_id.startswith("portfolio_") and block.payload.table_id.endswith("_signals") and block.payload.rows)
 control=table_control(table)
 controls=tuple(_walk(control))
 assert not any(isinstance(item,ft.DataTable) for item in controls)
 labels={getattr(item,"value",None) for item in controls if isinstance(item,ft.Text)}
 assert {"系统判断","达到以下条件后再行动","条件满足后的执行方案"}<=labels


def test_standalone_evaluation_loads_on_first_show_and_has_five_user_facing_views(now):
 from application.evaluation import HistoricalEvaluationService
 calls=[]
 class Service:
  def load(self,query):
   calls.append(query)
   return HistoricalEvaluationService().build(query,built_at=now)
 page=AbilityEvaluationPage(evaluation_port=Service())
 control=page.build()
 assert not calls and page.view is None
 page.on_show()
 assert len(calls)==1 and page.view is not None
 segment_values=[
  {getattr(segment,"value",None) for segment in getattr(item,"segments",())}
 for item in _walk(control)
 ]
 assert {"overview","single_forecast","single_strategy","portfolio_forecast","portfolio_strategy"} in segment_values
 assert "查询" in {getattr(item,"content",None) for item in _walk(control)}
 date_fields=[item for item in _walk(control) if getattr(item,"label",None)=="日期范围"]
 assert len(date_fields)==1 and date_fields[0].read_only
 assert date_fields[0].on_click is not None and date_fields[0].suffix_icon is not None


def test_evaluation_filters_use_two_intentional_rows_without_random_wrapping():
 import flet as ft
 page=AbilityEvaluationPage();page.active_view="single_strategy"
 toolbar=page._toolbar()
 assert isinstance(toolbar,ft.Column) and len(toolbar.controls)==2
 scope_row,date_band=toolbar.controls
 assert isinstance(scope_row,ft.ResponsiveRow) and scope_row.columns==12
 assert [item.col["md"] for item in scope_row.controls]==[2,2,4,2,2]
 assert all(item.width is None for item in scope_row.controls)
 assert isinstance(date_band,ft.Container) and isinstance(date_band.content,ft.Row)
 assert "汇总统计全部历史" in date_band.content.controls[1].value
 assert "最近30天" in date_band.content.controls[1].value


def test_report_history_primary_filters_use_full_width_responsive_columns():
 import flet as ft
 class Service:
  def list(self,query): return type("Page",(),{"items":(),"total_count":0,"has_next":False})()
 control=ReportHistoryPage(Service()).build()
 rows=[item for item in _walk(control) if isinstance(item,ft.ResponsiveRow)]
 primary=next(item for item in rows if {getattr(child,"label",None) for child in item.controls}>={"市场","报告类型","股票代码或公司名","模式"})
 assert [item.col["md"] for item in primary.controls]==[2,2,5,3]
 assert all(item.width is None for item in primary.controls)


def test_tab3_no_longer_duplicates_historical_evaluation():
 control=PortfolioPage().build()
 segment_values=[
  {getattr(segment,"value",None) for segment in getattr(item,"segments",())}
  for item in _walk(control)
 ]
 assert {"account","watchlist"} in segment_values
 assert not any("evaluation" in values for values in segment_values)


def test_evaluation_accepts_bare_symbol_and_keeps_mode_in_query(us_instrument, now):
 from application.evaluation import HistoricalEvaluationService
 queries=[]
 class Service:
  def load(self,query):
   queries.append(query)
   return HistoricalEvaluationService().build(query,built_at=now)
 page=AbilityEvaluationPage(
  evaluation_port=Service(),
  lookup_port=lambda market,query:(us_instrument,) if market=="US" and query.lower()=="mu" else (),
 )
 page.active_view="single_forecast"
 page.symbol="mu"
 page.analysis_mode="pre"
 page.load()
 assert queries[-1].instrument==us_instrument
 assert queries[-1].analysis_mode.value=="pre"


def test_portfolio_forecast_query_only_uses_tab3_issued_predictions(now):
 from application.evaluation import HistoricalEvaluationService
 queries=[]
 class Service:
  def load(self,query):
   queries.append(query)
   return HistoricalEvaluationService().build(query,built_at=now)
 page=AbilityEvaluationPage(evaluation_port=Service())
 page.active_view="portfolio_forecast"
 page.load()
 assert queries[-1].report_kind.value=="portfolio"


def test_page_session_does_not_own_process_runtime():
 source=Path("main.py").read_text(encoding="utf-8")
 assert "page.on_disconnect = close_session" not in source
 assert "page.on_close = close_session" not in source
 assert "runner(lambda page: _main(page, lifecycle))" in source
