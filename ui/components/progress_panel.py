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
_MESSAGES = {
    "validating": "正在检查输入和账户信息",
    "resolve_subject": "正在识别股票代码与市场",
    "refresh_metadata": "正在更新公司资料",
    "refresh_market_data": "正在更新行情、新闻和基本面",
    "build_features": "正在计算技术与市场特征",
    "forecast": "正在生成独立概率预测",
    "scenario": "正在把预测转换为交易情景",
    "strategy": "正在生成条件交易计划",
    "risk": "正在核对仓位、止损和最大亏损",
    "execution_preview": "正在生成可执行订单预览",
    "portfolio_allocation": "正在比较组合优先级和风险容量",
    "research": "正在验证研究员观察",
    "learning_update": "正在更新历史验证账本",
    "build_report": "正在整理报告",
    "persist_report": "正在保存冻结报告",
    "completed": "分析完成",
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
        ft.Text(f"{_MESSAGES.get(progress.message_code, stage)}{subject}{retry}", size=12, color=ft.Colors.BLUE_GREY_700),
    ], spacing=5)
