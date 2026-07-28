"""UX04--UX06、UX50--UX52：报告历史不可变持久化。"""
from test_presentation_contracts import _document,_input
from contracts import ReportFeedback,stable_hash
from data.repository import SQLiteRepository
from contracts.presentation import ReportHistoryQuery
def test_ux04_snapshot_idempotent_and_recovers_strong_type(us_instrument,now,tmp_path):
 path=tmp_path/"r.sqlite";repo=SQLiteRepository(path);doc=_document(_input(us_instrument,now));assert repo.save_report_document(doc).inserted==1 and repo.save_report_document(doc).idempotent==1;repo.close()
 repo=SQLiteRepository(path);assert repo.get_report_document(doc.report_id)==doc and repo.get_report_snapshot(doc.report_id).document_hash==doc.document_hash;repo.close()
def test_ux05_feedback_append_only(us_instrument,now,tmp_path):
 repo=SQLiteRepository(tmp_path/"r.sqlite");doc=_document(_input(us_instrument,now));repo.save_report_document(doc); f=ReportFeedback(stable_hash({"report":doc.report_id,"rating":5,"note":"ok","created":now}),doc.report_id,5,"ok",now);assert repo.save_report_feedback(f).inserted==1;repo.close()
def test_history_row_projection_reads_snapshot_only(us_instrument,now,tmp_path):
 repo=SQLiteRepository(tmp_path/"r.sqlite");doc=_document(_input(us_instrument,now));repo.save_report_document(doc);assert repo.list_report_history_rows(ReportHistoryQuery(market=us_instrument.market))[0]["report_id"]==doc.report_id;repo.close()
def test_compare_limits_more_than_three(us_instrument,now):
 from application.history import ReportHistoryService
 doc=_document(_input(us_instrument,now));
 try: ReportHistoryService(None).compare((doc,doc,doc,doc));assert False
 except ValueError: assert True
