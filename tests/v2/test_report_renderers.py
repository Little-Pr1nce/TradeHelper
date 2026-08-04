"""UX53--UX55：同一文档的确定性导出。"""
from test_presentation_contracts import _document,_input
from presentation.renderers import render_html,render_markdown,render_pdf
def test_ux53_html_is_self_contained(us_instrument,now):
    html=render_html(_document(_input(us_instrument,now)))
    assert "cdn" not in html.lower() and "基本信息与数据核对" in html and "操作总结" in html
    assert "美东时间" in html and now.isoformat() not in html
    assert 'class="contents"' in html and 'class="section-number"' in html
    assert 'class="forecast-grid"' in html and 'class="forecast-card"' in html
    assert 'class="operation-card' in html and 'class="probability-bars"' in html
def test_ux54_pdf_is_readable_bytes(us_instrument,now): assert render_pdf(_document(_input(us_instrument,now))).startswith(b"%PDF")
def test_ux55_export_failure_keeps_report(us_instrument,now,tmp_path):
    from application.exports import export_report
    from contracts import ExportFormat,ExportStatus
    from data.repository import SQLiteRepository
    doc=_document(_input(us_instrument,now));repo=SQLiteRepository(tmp_path/"reports.sqlite");repo.save_report_document(doc)
    artifact=export_report(repo,doc,directory=tmp_path/"reports.sqlite"/"blocked",format=ExportFormat.HTML)
    assert artifact.status is ExportStatus.FAILED and repo.get_report_document(doc.report_id)==doc;repo.close()


def test_completed_export_is_revealed_in_macos_file_manager(monkeypatch, tmp_path):
    from application import exports
    calls = []
    monkeypatch.setattr(exports.sys, "platform", "darwin")
    monkeypatch.setattr(exports.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)))
    target = tmp_path / "report.html"
    target.write_text("ok", encoding="utf-8")
    assert exports.reveal_in_file_manager(target)
    assert calls[0][0] == ("open", "-R", str(target.resolve()))
