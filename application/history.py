"""报告历史只读取快照，不触发重新分析。"""
from __future__ import annotations
from contracts import ReportFeedback, ReportHistoryQuery, stable_hash

class ReportHistoryService:
    def __init__(self, repository): self._repository=repository
    def list(self, query: ReportHistoryQuery):
        return self._repository.list_report_history(query)
    def get_document(self, report_id: str):
        return self._repository.get_report_document(report_id)
    def archive(self, report_id: str, *, archived: bool=True):
        """软归档只改变快照可见性，绝不删除冻结业务证据。"""
        self._repository.archive_report(report_id,archived=archived)
    def rate(self, report_id, rating, *, note=None, created_at):
        identity={"report":report_id,"rating":rating,"note":note,"created":created_at}
        value=ReportFeedback(stable_hash(identity),report_id,rating,note,created_at)
        self._repository.save_report_feedback(value)
        return value
    def compare(self, documents):
        if not 1<=len(documents)<=3: raise ValueError("compare one to three reports")
        if len({item.report_kind for item in documents})!=1 or len({item.market for item in documents})!=1: raise ValueError("reports must have same kind and market")
        return tuple(documents)
