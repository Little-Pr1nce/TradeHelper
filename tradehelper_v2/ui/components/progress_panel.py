from __future__ import annotations
import flet as ft

_STAGES = {
    "validate_input": "校验输入", "resolve_subject": "识别股票", "refresh_metadata": "更新公司资料",
    "refresh_market_data": "更新行情", "build_features": "计算特征", "forecast": "生成预测",
    "scenario": "规划情景", "strategy": "生成策略", "risk": "风控审核",
    "execution_preview": "生成订单预览", "portfolio_allocation": "组合分配",
    "research": "研究观察", "learning_update": "更新历史验证", "build_report": "生成报告",
    "persist_report": "保存报告", "completed": "分析完成",
}

def progress_panel(progress):
    retry="" if progress.retry_at is None else f"；预计重试：{progress.retry_at.isoformat()}"
    subject="" if progress.instrument is None else f"；当前股票：{progress.instrument.code}"
    ratio = progress.completed_units / progress.total_units if progress.total_units else 0
    stage = _STAGES.get(progress.stage.value, progress.stage.value)
    return ft.Column([
        ft.Row([
            ft.Text(f"{stage} · {progress.completed_units}/{progress.total_units}", weight=ft.FontWeight.BOLD),
            ft.Text(f"{ratio:.0%}", color=ft.Colors.BLUE_GREY_700),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.ProgressBar(value=ratio, bar_height=6),
        ft.Text(f"{progress.message_code}{subject}{retry}", size=12, color=ft.Colors.BLUE_GREY_700),
    ], spacing=5)
