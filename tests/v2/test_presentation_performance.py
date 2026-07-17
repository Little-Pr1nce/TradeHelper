"""UX58：冻结输入下的本地展示预算。"""
from time import perf_counter
from presentation_helpers import portfolio_presentation
from test_presentation_contracts import _document,_input
from tradehelper_v2.contracts import InstrumentId,Market
from tradehelper_v2.presentation.report_builder import PortfolioReportBuilder
from tradehelper_v2.presentation.renderers import render_html
def test_ux58_document_and_html_fast(us_instrument,now):
 start=perf_counter();doc=_document(_input(us_instrument,now));render_html(doc);assert perf_counter()-start<.5


def test_ux58_fifty_stock_portfolio_document_is_local_and_fast(now):
 instruments=tuple(InstrumentId.from_code(f"T{i:03d}",Market.US,"XNAS") for i in range(50))
 value=portfolio_presentation(instruments,now=now,calendar=None)
 start=perf_counter();PortfolioReportBuilder().build(value)
 assert perf_counter()-start<1.5
