"""上游原因码的可读翻译；未知码始终保留技术详情。"""
from __future__ import annotations

_REASONS={
    "REPORT_MODEL_SAMPLE_INSUFFICIENT":"样本仍在积累，暂不能判断模型稳定性。",
    "REPORT_TAKE_PROFIT_UNAVAILABLE":"没有冻结的止盈目标，因此风险收益比不可量化。",
    "REPORT_RESEARCH_UNAVAILABLE":"研究员服务不可用；这不会影响确定性交易结论。",
    "TASK_RATE_LIMIT_WAITING":"数据源限频，系统将在预计重试时间后继续。",
    "RISK_T1_BLOCKED":"市场规则暂时限制卖出，保护意图仍被保留。",
    "RISK_CONCENTRATION_WARNING":"该操作会提高单股集中度。",
    "RISK_CONDITIONALLY_APPROVED":"风险可控，但必须等条件触发后才能执行。",
    "RISK_FRICTION_RESERVE_INCLUDED":"最大亏损已计入费用和滑点预留。",
    "RISK_GAP_LOSS_CAN_EXCEED_PLAN":"跳空时实际亏损可能超过计划金额。",
    "RISK_HARD_CONSTRAINT_IMMUTABLE":"账户、止损和市场规则等硬约束不能被模型绕过。",
    "RISK_MARKET_RECHECK_REQUIRED":"执行前必须用最新行情重新检查条件。",
    "RISK_PORTFOLIO_ALLOCATION_PENDING":"单股已通过初审，最终股数等待组合资金分配。",
    "RISK_SMALL_SAMPLE":"历史样本不足，只允许小仓验证。",
    "RISK_PLAN_OBSERVATION_ONLY":"该计划仅供观察，不生成可执行订单。",
}
def explain(code: str) -> str:
    return _REASONS.get(code,f"技术原因：{code}")
