"""UX53--UX55：同一文档的确定性导出。"""
from test_presentation_contracts import _document,_input
from tradehelper_v2.presentation.renderers import render_html,render_markdown,render_pdf
def test_ux53_html_is_self_contained(us_instrument,now):
    html=render_html(_document(_input(us_instrument,now))); assert "cdn" not in html.lower() and "一分钟操作台" in html
def test_ux54_pdf_is_readable_bytes(us_instrument,now): assert render_pdf(_document(_input(us_instrument,now))).startswith(b"%PDF")
def test_ux55_export_failure_keeps_report(us_instrument,now,tmp_path):
    from tradehelper_v2.application.exports import export_report
    from tradehelper_v2.contracts import ExportFormat,ExportStatus
    from tradehelper_v2.data.repository import SQLiteRepository
    doc=_document(_input(us_instrument,now));repo=SQLiteRepository(tmp_path/"reports.sqlite");repo.save_report_document(doc)
    artifact=export_report(repo,doc,directory=tmp_path/"reports.sqlite"/"blocked",format=ExportFormat.HTML)
    assert artifact.status is ExportStatus.FAILED and repo.get_report_document(doc.report_id)==doc;repo.close()
