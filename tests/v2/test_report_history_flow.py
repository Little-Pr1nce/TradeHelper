"""UX50--UX52：冻结历史筛选、追加评分与安全比较。"""
from datetime import timedelta
import pytest
from application.history import ReportHistoryService
from contracts import ReportFeedback,ReportHistoryQuery,stable_hash
from data.repository import SQLiteRepository
from test_presentation_contracts import _document,_input

def _feedback(document, rating, when):
    return ReportFeedback(stable_hash({"report":document.report_id,"rating":rating,"note":None,"created":when}),document.report_id,rating,None,when)
def test_ux50_history_filter_and_detail_do_not_reanalyze(us_instrument,now,tmp_path):
    repo=SQLiteRepository(tmp_path/"history.sqlite"); document=_document(_input(us_instrument,now));repo.save_report_document(document)
    service=ReportHistoryService(repo); page=service.list(ReportHistoryQuery(market=us_instrument.market)); assert page.items[0].report_id==document.report_id and service.get_document(document.report_id)==document
    repo.close()
def test_ux51_rating_change_appends_feedback(us_instrument,now,tmp_path):
    repo=SQLiteRepository(tmp_path/"history.sqlite"); doc=_document(_input(us_instrument,now));repo.save_report_document(doc)
    repo.save_report_feedback(_feedback(doc,3,now));repo.save_report_feedback(_feedback(doc,5,now+timedelta(seconds=1)))
    assert [item.rating for item in repo.list_report_feedback(doc.report_id)]==[3,5];repo.close()
def test_ux52_comparison_needs_same_kind_and_market(us_instrument,a_instrument,now):
    service=ReportHistoryService(None); document=_document(_input(us_instrument,now)); other=_document(_input(a_instrument,now))
    with pytest.raises(ValueError): service.compare((document,other))
def test_history_period_date_and_rating_filters_use_snapshot_index(us_instrument,now,tmp_path):
    repo=SQLiteRepository(tmp_path/"history.sqlite");doc=_document(_input(us_instrument,now));repo.save_report_document(doc);repo.save_report_feedback(_feedback(doc,4,now))
    query=ReportHistoryQuery(market=us_instrument.market,history_period="3m",date_from=doc.as_of-timedelta(minutes=1),date_to=doc.as_of+timedelta(minutes=1),minimum_rating=4)
    assert repo.list_report_history(query).total_count==1;repo.close()


def test_history_list_reads_snapshot_summaries_without_revalidating_full_documents(us_instrument,now,tmp_path,monkeypatch):
    repo=SQLiteRepository(tmp_path/"history.sqlite");doc=_document(_input(us_instrument,now));repo.save_report_document(doc)
    monkeypatch.setattr(repo,"get_report_snapshot",lambda _report_id: (_ for _ in ()).throw(AssertionError("full document validation")))
    page=repo.list_report_history(ReportHistoryQuery())
    assert page.items[0].report_id==doc.report_id and page.items[0].instrument==us_instrument
    repo.close()
