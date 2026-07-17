"""UX30--UX34：首事件、单调进度、限频和取消。"""
from datetime import timedelta
import pytest
from tradehelper_v2.application.tasks import AnalysisTaskCoordinator
from tradehelper_v2.contracts import AnalysisStage,TaskStatus
def test_ux30_first_event_is_immediate(now):
 c=AnalysisTaskCoordinator(lambda:now); assert c.start("x").elapsed_seconds<.25
def test_ux31_progress_never_regresses(now):
 c=AnalysisTaskCoordinator(lambda:now); c.start("x",total_units=2); c.emit("x",AnalysisStage.FORECAST,TaskStatus.RUNNING,1,2,None,"running")
 with pytest.raises(Exception): c.emit("x",AnalysisStage.RISK,TaskStatus.RUNNING,0,2,None,"running")
def test_ux32_waiting_has_retry_and_instrument(us_instrument,now):
 c=AnalysisTaskCoordinator(lambda:now); c.start("x",instrument=us_instrument); assert c.emit("x",AnalysisStage.REFRESH_MARKET_DATA,TaskStatus.WAITING,0,1,us_instrument,"TASK_RATE_LIMIT_WAITING",retry_at=now+timedelta(minutes=1)).retry_at
def test_ux33_cancelled_task_stays_cancelled(now):
 c=AnalysisTaskCoordinator(lambda:now);c.start("x");c.cancel("x")
 with pytest.raises(Exception): c.emit("x",AnalysisStage.FORECAST,TaskStatus.RUNNING,1,1,None,"running")
def test_ux34_background_task_does_not_block_foreground(now):
 c=AnalysisTaskCoordinator(lambda:now);background=c.start("background",background=True); foreground=c.start("foreground",background=False)
 assert background.background and not foreground.background and foreground.status is TaskStatus.QUEUED
def test_progress_panel_uses_human_stage_and_visual_bar(now):
 from tradehelper_v2.ui.components.progress_panel import progress_panel
 progress=AnalysisTaskCoordinator(lambda:now).start("visual",total_units=10)
 control=progress_panel(progress)
 assert control.controls[0].controls[0].value.startswith("校验输入") and control.controls[1].value==0
