"""报告可读性与固定结构，不通过日志断言。"""
from tradehelper_v2.presentation.report_builder import SingleStockReportBuilder,PortfolioReportBuilder
from test_presentation_contracts import _input

def test_single_stock_report_has_nine_ordered_sections(us_instrument,now):
    doc=SingleStockReportBuilder().build(_input(us_instrument,now))
    assert [item.section_id for item in doc.sections]==["action_desk","forecast","plans","risk","facts","scenario_evidence","research","history","glossary"]
def test_report_uses_plain_chinese_missing_semantics(us_instrument,now):
    doc=SingleStockReportBuilder().build(_input(us_instrument,now)); assert "暂无可靠数据" in " ".join(str(block.payload) for section in doc.sections for block in section.blocks)
