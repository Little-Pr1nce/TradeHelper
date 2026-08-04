"""报告可读性与固定结构，不通过日志断言。"""
from presentation.report_builder import SingleStockReportBuilder
from test_presentation_contracts import _input

def test_single_stock_report_has_seven_ordered_sections(us_instrument,now):
    doc=SingleStockReportBuilder().build(_input(us_instrument,now))
    assert [item.section_id for item in doc.sections]==[
        "action_summary","facts","forecast","operation_report",
        "strategy_performance","research","history",
    ]
def test_report_uses_plain_chinese_missing_semantics(us_instrument,now):
    doc=SingleStockReportBuilder().build(_input(us_instrument,now)); assert "暂无可靠数据" in " ".join(str(block.payload) for section in doc.sections for block in section.blocks)
