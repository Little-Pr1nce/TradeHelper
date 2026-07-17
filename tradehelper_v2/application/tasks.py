"""进程内任务进度协调；不把半成品报告持久化。"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from threading import RLock
from time import monotonic
from tradehelper_v2.contracts import AnalysisStage, AnalysisTaskProgress, ContractViolation, TaskStatus

class AnalysisTaskCoordinator:
    def __init__(self, clock):
        self._clock=clock; self._started={}; self._progress=defaultdict(int); self._cancelled=set(); self._totals={}; self._stages={}; self._lock=RLock()
    def start(self, task_id, *, total_units=1, instrument=None, background=False):
        with self._lock:
            if task_id in self._started or total_units <= 0: raise ContractViolation("task already exists or total is invalid")
            self._started[task_id]=monotonic()
            self._totals[task_id]=total_units
            self._stages[task_id]=AnalysisStage.VALIDATE_INPUT
            return self.emit(task_id,AnalysisStage.VALIDATE_INPUT,TaskStatus.QUEUED,0,total_units,instrument,"queued",background=background)
    def emit(self, task_id, stage, status, completed_units, total_units, instrument, message_code, *, retry_at=None, cancellable=True, background=False):
        with self._lock:
            stage=stage if isinstance(stage,AnalysisStage) else AnalysisStage(str(stage))
            if task_id not in self._started or total_units!=self._totals[task_id] or completed_units<self._progress[task_id] or completed_units>total_units: raise ContractViolation("task progress must be monotonic")
            order={value:index for index,value in enumerate(AnalysisStage)}
            if order[stage]<order[self._stages[task_id]]: raise ContractViolation("task stage must be monotonic")
            if task_id in self._cancelled and status not in {TaskStatus.CANCELLED,TaskStatus.CANCELLING}: raise ContractViolation("cancelled task cannot resume")
            self._progress[task_id]=completed_units
            self._stages[task_id]=stage
            return AnalysisTaskProgress(task_id,stage,status,completed_units,total_units,instrument,message_code,monotonic()-self._started[task_id],retry_at,cancellable,background,self._clock())
    def cancel(self, task_id, *, instrument=None):
        with self._lock:
            if task_id not in self._started:
                raise ContractViolation("cannot cancel an unknown task")
            self._cancelled.add(task_id)
            return self.emit(task_id,self._stages[task_id],TaskStatus.CANCELLED,self._progress[task_id],self._totals[task_id],instrument,"TASK_CANCELLED",cancellable=False)

    def can_persist(self, task_id):
        with self._lock:
            return task_id in self._started and task_id not in self._cancelled and self._progress[task_id]==self._totals[task_id] and self._stages[task_id] is AnalysisStage.COMPLETED

    def is_cancelled(self, task_id):
        with self._lock:
            return task_id in self._cancelled
