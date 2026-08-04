"""Build deterministic, source-closed reports from frozen V2 contracts."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from contracts import (
    MetricDefinition,
    PlanAction,
    PortfolioPresentationInput,
    ReportBlock,
    ReportBlockKind,
    ReportDocument,
    ReportKind,
    ReportSection,
    ReportSeverity,
    ReportTable,
    ReportTableRow,
    RiskProfile,
    SingleStockPresentationInput,
    presentation_source_refs,
    stable_hash,
)
from contracts.presentation import PRESENTATION_POLICY_REF
from .formatting import format_datetime, format_money, format_percent, format_value
from .reasons import explain


_GLOSSARY = (
    MetricDefinition("brier", "Brier", "概率预测和真实结果的平方误差。", "越低越好", "至少 30 条成熟样本", "score"),
    MetricDefinition("log_loss", "Log Loss", "对非常自信但错误的预测惩罚更重。", "越低越好", "至少 30 条成熟样本", "score"),
    MetricDefinition("ece", "ECE", "模型说 70% 时，实际发生率是否接近 70%。", "越低越好", "至少 30 条成熟样本", "score"),
    MetricDefinition("interval_hit", "80% 区间命中", "实际收益落在 P10-P90 预测区间的比例。", "样本充分后接近 80%", "至少 30 条成熟样本", "%"),
    MetricDefinition("oof", "样本外验证（OOF）", "模拟当时未知未来的验证，未通过不代表行情缺失。", "通过后才可用于模型比较", "至少 30 条成熟样本", None),
)


def _value(value):
    return getattr(value, "value", value)


def _refs(*artifacts):
    return presentation_source_refs(*artifacts) if artifacts else (PRESENTATION_POLICY_REF,)


def _text(value, refs=(PRESENTATION_POLICY_REF,)):
    return ReportBlock(ReportBlockKind.TEXT, str(value), tuple(refs))


def _callout(value, refs):
    return ReportBlock(ReportBlockKind.CALLOUT, str(value), tuple(refs))


def _table(value, refs):
    return ReportBlock(ReportBlockKind.TABLE, value, tuple(refs))


def _table_refs(value):
    refs = {ref for row in value.rows for ref in row.source_artifact_refs}
    return tuple(sorted(refs)) or (PRESENTATION_POLICY_REF,)


def _section(key, title, purpose, *blocks, severity=None):
    return ReportSection(key, title, purpose, severity, tuple(blocks))


def _row(row_id, cells, refs, severity=None):
    return ReportTableRow(str(row_id), tuple(str(cell) for cell in cells), severity, tuple(refs))


def _currency(value, market):
    if value is None:
        return "暂无可靠数据"
    return format_money(value, "CNY" if _value(market) == "A" else "USD")


def _datetime_text(value, market=None, *, seconds=False):
    return format_datetime(value, market, seconds=seconds) if isinstance(value, datetime) or value is None else str(value)


def _feature(value, name):
    return next((item for item in value.feature_snapshot.values if item.name == name), None)


def _feature_number(value, name):
    item = _feature(value, name)
    return None if item is None or item.value is None or isinstance(item.value, bool) else item.value


def _price_text(value, market):
    return _currency(_analysis_price(value), market)


def _analysis_price(value):
    price = _feature_number(value, "current.price")
    if price is not None:
        return price
    quote = getattr(value, "quote_snapshot", None)
    if quote is not None and getattr(quote, "price", None) is not None:
        return quote.price
    forecasts = tuple(getattr(value, "forecasts", ()))
    return forecasts[0].reference_price if forecasts else None


def _price_source_text(value):
    feature = _feature(value, "current.price")
    if feature is None or feature.value is None:
        feature = next((item for item in value.feature_snapshot.values if item.name.startswith("closed.") and item.sources), None)
    sources = tuple(getattr(feature, "sources", ()))
    if sources:
        return "、".join(sources)
    quote = getattr(value, "quote_snapshot", None)
    return getattr(quote, "source", None) or "冻结日K/实时快照"


def _reason_text(codes):
    values = tuple(codes or ())
    return "；".join(explain(code) for code in values) if values else "暂无原因代码"


_FEATURE_NAMES = {
    "current.price": "当前价", "closed.ma_20": "MA20", "closed.ma_60": "MA60",
    "closed.ma_120": "MA120", "closed.rsi_14": "RSI14", "closed.volume_ratio_20": "20日量比",
    "current.volume_vs_daily_20": "盘中量能/20日均量", "current.retreat_from_session_high": "距当日高点回撤",
}

_RESEARCH_STATUS_NAMES = {
    "confirmed": "已确认", "refuted": "系统反驳", "pending": "待验证",
    "invalid_data": "数据不足",
}
_CANDIDATE_ELIGIBILITY_NAMES = {
    "eligible_for_oof": "可进入样本外验证",
    "observation_only": "仅观察",
    "implementation_required": "需要实现后验证",
    "rejected": "不纳入候选",
}
_RESEARCH_OUTCOME_NAMES = {
    "pending": "尚未到期", "matured": "已到期", "unverifiable": "不可验证",
    "not_applicable": "不适用", "superseded": "已被替代",
}
_RESEARCH_RUN_STATUS_NAMES = {
    "pending": "外部 LLM 已提交，正在后台研究，完成后自动更新本报告",
    "completed": "外部 LLM 调用完成",
    "partial": "外部 LLM 部分分片未完成，已保留可验证结果",
    "unavailable": "外部 LLM 未配置或当前不可用；确定性交易主报告不受影响",
    "failed": "外部 LLM 研究失败；确定性交易主报告不受影响",
}
_RESEARCH_FAILURE_REASON_NAMES = {
    "RESEARCH_SCHEMA_INVALID": "模型输出格式或引用对象不符合研究合同",
    "RESEARCH_INSTRUMENT_UNKNOWN": "模型引用了本分片之外的股票",
    "RESEARCH_RESPONSE_TRUNCATED": "模型输出被截断",
    "RESEARCH_LLM_TIMEOUT": "模型调用超时",
    "RESEARCH_LLM_TRANSPORT_FAILED": "模型服务连接失败",
    "cancelled": "研究任务被取消",
    "empty": "模型返回空响应",
    "truncated": "模型输出被截断",
    "transport_failed": "模型服务连接失败",
    "timed_out": "模型调用超时",
}
_ACTION_NAMES = {
    "buy": "买入", "add": "加仓", "sell": "卖出", "reduce": "减仓",
    "hold": "持有", "watch": "观察", "invalid": "失效",
}
_PROFILE_NAMES = {"conservative": "保守", "aggressive": "激进"}
_LEVEL_NAMES = {
    "A": "A级（可执行）", "B": "B级（小仓验证）",
    "C": "C级（仅观察）", "D": "D级（数据冲突）",
}
_READINESS_NAMES = {
    "triggered": "已触发", "waiting": "等待触发", "observation_only": "仅观察",
    "not_applicable": "不适用",
}
_MODEL_LIFECYCLE_NAMES = {
    "candidate": "候选模型", "challenger": "挑战模型", "shadow": "影子模型",
    "champion": "正式模型", "retired": "已退役",
}
_VALIDATION_STATUS_NAMES = {
    "not_evaluated": "尚未完成历史样本外检验", "insufficient_sample": "样本不足",
    "selection_passed": "初选通过", "confirmation_passed": "确认通过",
    "noninferior_passed": "校准通过，与基线相当",
    "evaluated_not_better": "未优于基线", "calibration_failed": "概率校准未通过",
    "overfit": "疑似过拟合", "drifted": "模型漂移",
}
_DISPOSITION_NAMES = {
    "approved_now": "当前批准", "conditionally_approved": "条件满足后批准",
    "no_order_required": "无需订单", "observe": "仅观察", "rejected": "已驳回",
}
_DIRECTION_NAMES = {"bullish": "上涨", "neutral": "震荡", "bearish": "下跌"}
_SCENARIO_STATE_NAMES = {
    "bullish_continuation": "上涨趋势延续",
    "bullish_pullback": "上涨趋势中的回调",
    "bearish_continuation": "下跌趋势延续",
    "bearish_rebound": "下跌趋势中的反弹",
    "range_bound": "区间震荡",
    "mixed": "方向分歧",
    "blocked": "事实不足，无法形成情景",
}
_STRATEGY_FAMILY_NAMES = {
    "trend_continuation": "趋势延续",
    "breakout_confirmation": "突破确认",
    "pullback_entry": "回调入场",
    "support_rebound": "支撑反弹",
    "range_mean_reversion": "区间回归",
    "protective_exit": "成本保护/止损",
    "profit_lock": "冲高回落锁利",
    "failed_rebound_exit": "跌破后反抽失败退出",
    "observation": "观察条件",
}
_ALLOCATION_STATUS_NAMES = {
    "allocated_now": "当前已分配", "reserved_conditional": "条件触发后预留",
    "shared_exit_reservation": "备选退出共用持仓", "monitor_only": "仅观察",
    "blocked": "组合约束阻止", "no_order": "当前无订单",
}
_REPLACEMENT_STATUS_NAMES = {
    "research_after_exit": "退出后再研究", "watch_only": "仅关注",
    "rejected": "不作为替换候选",
}
_EVIDENCE_GRADE_NAMES = {
    "high": "较充分", "medium": "一般", "low": "偏弱", "insufficient": "样本不足",
    "reliable_positive": "可靠正期望", "positive_uncertain": "正期望待确认",
    "insufficient_sample": "样本不足", "unavailable": "不可用",
    "negative": "负期望", "conflicting": "证据冲突",
}
_QUALITY_STATUS_NAMES = {"ok": "正常", "watch": "需留意", "degraded": "已降级", "blocked": "已阻止"}
_LEDGER_NAMES = {"forecast": "预测账", "strategy": "策略账", "joint": "联合账"}
_OUTCOME_STATUS_NAMES = {
    "pending": "待验证", "matured": "已验证", "unverifiable": "不可验证",
    "conflicting": "证据冲突", "superseded": "已被更新记录替代",
}


def _display(mapping, value):
    raw = str(_value(value))
    return mapping.get(raw, raw)


def _strategy_history_text(outcomes):
    exit_actions = {"sell", "reduce"}
    is_exit = bool(outcomes) and all(item.action in exit_actions for item in outcomes)
    matured = tuple(sorted(
        (
            item for item in outcomes
            if (item.exit_quality is not None if is_exit else item.net_return is not None)
        ),
        key=lambda item: (item.target_session_date, item.strategy_outcome_id),
    ))
    if not matured:
        return "尚未完成该股票的历史退出评估，不能判断退出时机" if is_exit else "尚未完成该股票的历史入场回放，不能声称已有正期望"
    returns = tuple(Decimal(str(item.exit_quality if is_exit else item.net_return)) for item in matured)
    positive = sum(item > 0 for item in returns)
    average = sum(returns, Decimal("0")) / Decimal(len(returns))
    if is_exit:
        avoided = tuple(Decimal(str(item.exit_avoided_loss)) for item in matured if item.exit_avoided_loss is not None)
        opportunity = tuple(Decimal(str(item.exit_opportunity_cost)) for item in matured if item.exit_opportunity_cost is not None)
        text = (
            f"已评估 {len(matured)} 次退出，有效 {positive} 次；"
            f"平均退出质量 {format_percent(average)}"
        )
        if avoided:
            text += f"；平均避免损失 {format_percent(sum(avoided, Decimal('0')) / len(avoided))}"
        if opportunity:
            text += f"；平均踏空成本 {format_percent(sum(opportunity, Decimal('0')) / len(opportunity))}"
        text += "；退出质量为正表示当时退出优于继续持有，不等于账户收益"
        return text + ("；样本仍少，只能作为观察" if len(matured) < 30 else "")
    adverse = tuple(Decimal(str(item.mae)) for item in matured if item.mae is not None)
    text = (
        f"已验证 {len(matured)} 次入场，盈利 {positive} 次；"
        f"每次交易平均净收益 {format_percent(average)}；"
        f"盈利率 {format_percent(positive / len(matured))}"
    )
    if adverse:
        text += f"；持有期最差不利波动 {format_percent(min(adverse))}"
    benchmarks = tuple(item.benchmark_return for item in matured if item.benchmark_return is not None)
    if len(benchmarks) == len(matured):
        average_benchmark = sum((Decimal(str(item)) for item in benchmarks), Decimal("0")) / Decimal(len(benchmarks))
        text += f"；同期每段买入持有平均收益 {format_percent(average_benchmark)}"
    if len(matured) < 30:
        text += "；样本仍少，只能作为观察"
    else:
        text += "；样本窗口可能重叠，累计收益和组合最大回撤请看联合历史回放"
    return text


def _matching_strategy_outcomes(value, plan, profile=None):
    return tuple(
        item for item in value.strategy_outcomes
        if item.instrument == plan.instrument
        and item.strategy_id == plan.strategy_id
        and item.strategy_version == plan.strategy_version
        and item.parameter_hash == plan.parameter_hash
        and item.action == plan.action.value
        and (profile is None or item.profile == profile.value)
        and item.evidence_origin.value == "reconstructed_oof"
        and (
            item.exit_quality is not None
            if plan.action.value in {"sell", "reduce"}
            else item.net_return is not None
        )
    )


def _action(value):
    return _display(_ACTION_NAMES, value)


def _profile(value):
    return _display(_PROFILE_NAMES, value)


def _allocation_funds(item, market):
    if item.action in {PlanAction.SELL, PlanAction.REDUCE}:
        return "不占用现金；卖出回款本轮不复用"
    if item.reserved_cash > 0:
        return f"预留 {_currency(item.reserved_cash, market)}"
    return "不占用现金"


def _allocation_status(item, member):
    plans = (
        member.strategy_bundle.entry_or_add.plans
        + member.strategy_bundle.reduce_or_exit.plans
        + member.strategy_bundle.hold.plans
        + member.strategy_bundle.invalidation.plans
    )
    plan = next((value for value in plans if value.plan_id == item.plan_id), None)
    if plan is not None and str(_value(plan.readiness)) == "triggered":
        suffix = "；备选退出共用持仓" if str(_value(item.status)) == "shared_exit_reservation" else ""
        return f"条件已满足，执行前复核{suffix}"
    return _display(_ALLOCATION_STATUS_NAMES, item.status)


def _plan_for_allocation(item, member):
    return next((
        plan
        for branch in (
            member.strategy_bundle.entry_or_add,
            member.strategy_bundle.reduce_or_exit,
            member.strategy_bundle.hold,
            member.strategy_bundle.invalidation,
        )
        for plan in branch.plans
        if plan.plan_id == item.plan_id
    ), None)


def _allocation_explanation(item, member, market):
    plan = _plan_for_allocation(item, member)
    if plan is None:
        return "上游计划缺失，执行前必须重新分析"
    family = _display(_STRATEGY_FAMILY_NAMES, plan.family)
    current = _price_text(member, market)
    condition = _condition_text(plan)
    if plan.action in {PlanAction.SELL, PlanAction.REDUCE}:
        return f"{family}：分析价 {current} 已满足“{condition}”；下一交易时段先用最新价复核"
    if str(_value(plan.readiness)) == "triggered":
        return f"{family}：分析价 {current} 已满足“{condition}”；执行前重算股数和风险"
    return f"{family}：等待“{condition}”"


def _portfolio_heat_text(profile):
    snapshot = profile.current_risk_snapshot
    status = str(_value(snapshot.heat_status))
    if status == "complete":
        return format_percent(profile.reservation_snapshot.projected_heat_pct)
    if status == "breached":
        return "已有持仓跌破保护线，先退出/减仓"
    return "部分持仓缺少可量化止损，暂不能合计"


def _portfolio_evidence_text(profile):
    snapshot = profile.current_risk_snapshot
    status = str(_value(snapshot.heat_status))
    if status == "breached":
        return "保护线已越过（不是行情样本不足）"
    grade = str(_value(profile.evidence_grade))
    if grade == "insufficient":
        return "风险要素不完整"
    return _display(_EVIDENCE_GRADE_NAMES, profile.evidence_grade)


def _metric_summary(metrics):
    labels = {
        "brier": "概率误差", "log_loss": "过度自信错误惩罚", "ece": "概率校准误差",
        "direction_accuracy": "方向正确率", "interval_hit_rate": "80%区间命中率",
        "mean_net_return": "平均净收益", "win_rate": "胜率", "alpha": "超过买入持有的收益",
        "mean_benchmark_return": "同期买入持有平均收益",
        "sharpe": "风险调整后表现", "max_drawdown": "最大回撤",
    }
    values = []
    for key, value in metrics:
        if value is None:
            continue
        label = labels.get(key, key.replace("_", " "))
        if any(token in key for token in ("return", "rate", "drawdown", "alpha")):
            rendered = format_percent(value)
        else:
            rendered = f"{value:.3f}"
        values.append(f"{label} {rendered}")
    return "；".join(values) if values else "暂无可解释指标"


def _metric_scope_text(scope_key):
    parts = str(scope_key).split(":")
    if "joint" in parts:
        profile = "保守方案" if parts[-1] == "conservative" else "激进方案"
        return f"完整链路（{profile}）"
    horizon = next((item[1:] for item in parts if item.startswith("h") and item[1:].isdigit()), None)
    if horizon is not None:
        return f"未来 {horizon} 个交易日预测"
    return "历史能力汇总"


def _outcome_summary(instrument, ledger, outcomes):
    if not outcomes:
        return None
    statuses = {}
    for item in outcomes:
        key = str(_value(item.status))
        statuses[key] = statuses.get(key, 0) + 1
    status_text = "；".join(
        f"{_display(_OUTCOME_STATUS_NAMES, key)} {count} 条"
        for key, count in sorted(statuses.items())
    )
    if ledger == "forecast":
        scored = [item for item in outcomes if item.direction_correct is not None]
        correct = sum(item.direction_correct is True for item in scored)
        brier = [item.event_brier for item in scored if item.event_brier is not None]
        details = f"方向正确 {correct}/{len(scored)}" if scored else "尚无到期评分"
        if brier:
            details += f"；平均概率误差 {sum(brier) / len(brier):.3f}（越低越好）"
    elif ledger == "strategy":
        entries = [item for item in outcomes if item.action in {"buy", "add"} and item.net_return is not None]
        exits = [item for item in outcomes if item.action in {"sell", "reduce"} and item.exit_quality is not None]
        details = "尚无可复盘策略结果"
        parts = []
        if entries:
            positive = sum(item.net_return > 0 for item in entries)
            parts.append(
                f"入场盈利 {positive}/{len(entries)}；平均交易净收益 "
                f"{format_percent(sum(item.net_return for item in entries) / len(entries))}"
            )
        if exits:
            positive = sum(item.exit_quality > 0 for item in exits)
            parts.append(
                f"有效退出 {positive}/{len(exits)}；平均退出质量 "
                f"{format_percent(sum(item.exit_quality for item in exits) / len(exits))}"
            )
        if parts:
            details = "；".join(parts)
    else:
        matured = [item for item in outcomes if str(_value(item.status)) == "matured"]
        details = "尚无成熟联合回放"
        if matured:
            system_return = sum(item.time_weighted_return for item in matured) / len(matured)
            benchmarks = [item.benchmark_return for item in matured if item.benchmark_return is not None]
            drawdowns = [item.max_drawdown for item in matured]
            sharpes = [item.sharpe for item in matured if item.sharpe is not None]
            details = f"平均组合收益 {format_percent(system_return)}"
            if benchmarks:
                benchmark = sum(benchmarks) / len(benchmarks)
                retention = None if benchmark <= 0 else system_return / benchmark
                details += f"；买入持有 {format_percent(benchmark)}；超过买入持有 {format_percent(system_return - benchmark)}"
                if retention is not None:
                    details += f"；收益保留率 {format_percent(retention)}"
            if drawdowns:
                details += f"；最差回撤 {format_percent(min(drawdowns))}"
            if sharpes:
                details += f"；风险调整后表现 {format_value(sum(sharpes) / len(sharpes))}（越高越好）"
    return _row(
        f"{instrument.stable_key}:{ledger}:summary",
        (instrument.code, _display(_LEDGER_NAMES, ledger), f"共 {len(outcomes)} 条", f"{status_text}；{details}"),
        _refs(*outcomes),
    )


def _operand_text(operand):
    if operand is None:
        return "未知值"
    if _value(operand.kind) == "feature":
        return _FEATURE_NAMES.get(operand.key, operand.key.replace("closed.", "").replace("current.", "当前"))
    value = operand.value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.2f}" if operand.unit == "price" else f"{value:g}"
    return str(value)


def _condition_expression_text(expression):
    operator = _value(expression.operator)
    if operator in {"all", "any"}:
        separator = " 且 " if operator == "all" else " 或 "
        return separator.join(f"({_condition_expression_text(child)})" for child in expression.children)
    if operator == "not":
        return f"不满足（{_condition_expression_text(expression.children[0])}）"
    left = _operand_text(expression.left)
    if operator == "between":
        return f"{left}位于 {_operand_text(expression.lower)} 至 {_operand_text(expression.upper)}"
    if (expression.left is not None and expression.right is not None and
            _value(expression.left.kind) == "constant" and _value(expression.right.kind) == "constant" and
            isinstance(expression.left.value, bool) and isinstance(expression.right.value, bool)):
        matched = expression.left.value == expression.right.value
        return "观察条件已满足" if matched else "观察条件当前未满足"
    labels = {
        "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "equals": "=",
        "crosses_above": "向上穿越", "crosses_below": "向下穿越",
    }
    return f"{left} {labels.get(operator, operator)} {_operand_text(expression.right)}"


def _condition_text(plan):
    level = plan.trigger_level.value if plan.trigger_level else None
    parts = ([f"触发价 {level:.2f}"] if level is not None else []) + [
        _condition_expression_text(plan.trigger_condition)
    ]
    if (plan.confirmation_condition is not None and
            plan.confirmation_condition.condition_id != plan.trigger_condition.condition_id):
        parts.append(f"确认：{_condition_expression_text(plan.confirmation_condition)}")
    missing = "、".join(plan.missing_conditions)
    if missing:
        parts.append(f"待补事实：{missing}")
    return "；".join(parts)


def _plain_condition_text(expression):
    """Render a frozen condition as trading language instead of a raw formula."""
    text = _condition_expression_text(expression)
    replacements = (
        ("当前价位于 ", "价格进入 "),
        ("当前价 >= ", "价格达到或站上 "),
        ("当前价 > ", "价格站上 "),
        ("当前价 <= ", "价格跌到或低于 "),
        ("当前价 < ", "价格跌破 "),
        ("当前价 向上穿越 ", "价格重新站上 "),
        ("当前价 向下穿越 ", "价格向下跌破 "),
        ("macd_hist_pct >= 0", "MACD 柱线转为非负"),
        ("macd_hist_pct > 0", "MACD 柱线转正"),
        ("macd_hist_pct <= 0", "MACD 柱线转为非正"),
        ("macd_hist_pct < 0", "MACD 柱线转负"),
        ("MA20 > MA60", "MA20 高于 MA60"),
        ("MA20 < MA60", "MA20 低于 MA60"),
    )
    for source, target in replacements:
        text = text.replace(source, target)
    return text.replace("(", "（").replace(")", "）")


def _signal_judgment_text(decision, plan):
    action = _action(decision.action)
    if plan is None:
        return f"建议{action}，但交易计划缺失。请重新分析，暂时不要下单。"
    family = _display(_STRATEGY_FAMILY_NAMES, plan.family)
    readiness = str(_value(plan.readiness))
    if decision.executable_now:
        return f"建议{action}。{family}条件已经确认，可按风控批准的数量执行。"
    if readiness == "triggered":
        return f"建议{action}，但现在先不下单。{family}条件已经出现，下一交易时段需用最新价格复核。"
    if readiness == "waiting":
        return f"关注{action}机会。系统正在等待{family}确认，条件满足前保持不动。"
    if readiness == "observation_only" or plan.action is PlanAction.WATCH:
        return f"当前以观察为主。{family}证据还不足，暂不生成订单。"
    return f"建议{action}，但当前尚未通过执行检查。满足下列条件后再重新评估。"


def _signal_steps_text(plan, decision, market):
    if plan is None:
        return "1. 重新运行分析并生成完整计划\n2. 核对最新价格、股数和最大亏损后再决定"
    steps = []
    trigger = _plain_condition_text(plan.trigger_condition)
    if str(_value(plan.readiness)) == "triggered":
        steps.append(f"复核：用下一交易时段最新价格确认“{trigger}”仍然成立")
    else:
        steps.append(f"触发：{trigger}")
    if (
        plan.confirmation_condition is not None
        and plan.confirmation_condition.condition_id != plan.trigger_condition.condition_id
    ):
        steps.append(f"确认：{_plain_condition_text(plan.confirmation_condition)}")
    if plan.missing_conditions:
        steps.append(f"补齐：{'、'.join(plan.missing_conditions)}")
    steps.append("执行：条件成立后，按最新价格重新核定股数和最大亏损")
    if plan.expires_at is not None:
        steps.append(f"期限：{_datetime_text(plan.expires_at, market)} 前有效，过期后重新分析")
    return "\n".join(steps)


def _share_count(value: Decimal) -> str:
    """Render an exact share quantity without assuming a particular Decimal exponent."""
    return str(int(value)) if value == value.to_integral_value() else format(value.normalize(), "f")


def _signal_profile_text(value, allocation_details):
    lines = []
    for profile_name in ("保守", "激进"):
        detail = allocation_details.get((value.instrument.code, profile_name))
        if detail is None:
            lines.append(f"{profile_name}：暂不下单；等待条件满足并重新通过组合风控")
            continue
        allocation, arrangement = detail
        action = _action(allocation.action)
        shares = allocation.final_requested_shares
        if shares <= 0 or action in {"观察", "持有"}:
            lines.append(f"{profile_name}：暂不下单；{arrangement}")
        else:
            timing = "现在" if "当前" in arrangement and "复核" not in arrangement else "条件确认后"
            lines.append(f"{profile_name}：{timing}{action} {_share_count(shares)} 股；{arrangement}")
    return "\n".join(lines)


def _editorial_table(action_table, members_by_code, editorial=None):
    editorial_items = {
        item.instrument: item
        for item in getattr(editorial, "items", ())
    }
    rows = []
    for action_row in action_table.rows:
        stock, identity, action_text, _next_step, _profiles = action_row.cells
        action = action_text.split("；", 1)[0]
        member = members_by_code.get(stock)
        item = editorial_items.get(stock)
        if item is not None and item.action == action:
            headline = item.headline
            reasons = "\n".join(item.reasons)
            risk_note = item.risk_note
            source = editorial.source
        else:
            detail = action_text.split("；", 1)[-1]
            headline = detail if detail.endswith("。") else f"{detail}。"
            if member is None:
                reasons = "系统已冻结当前动作\n具体条件和风险以下方操作卡为准"
            else:
                decision = _primary_decision(member)
                plans = {
                    plan.plan_id: plan
                    for branch in (member.strategy_bundle.entry_or_add, member.strategy_bundle.reduce_or_exit, member.strategy_bundle.hold)
                    for plan in branch.plans
                }
                plan = plans.get(decision.plan_id)
                strategy = "没有可用策略" if plan is None else _display(_STRATEGY_FAMILY_NAMES, plan.family)
                reasons = (
                    f"预测形成“{_display(_SCENARIO_STATE_NAMES, member.scenario.state)}”情景\n"
                    f"系统据此采用“{strategy}”思路\n"
                    f"风控结论为“{_display(_DISPOSITION_NAMES, decision.disposition)}”"
                )
            risk_note = "具体价格、数量、止损和最大亏损以下方冻结操作卡为准。"
            source = "系统自动解读"
        rows.append(_row(
            f"editorial:{stock}",
            (stock, identity, action, headline, reasons, risk_note, source),
            action_row.source_artifact_refs,
        ))
    overview = getattr(editorial, "overview", None) or "先看一句话结论，再按下方冻结条件决定是否行动；解读文字不会改变代码生成的操作计划。"
    return ReportTable(
        "operation_editorial", "操作逻辑解读",
        ("股票", "身份与价格", "动作", "一句话结论", "主要理由", "风险提醒", "解读来源"),
        tuple(rows), "当前没有需要解读的操作。", overview,
    )


def _risk_reward(plan):
    entry = plan.trigger_level.value if plan.trigger_level else None
    stop = plan.stop.level.value if plan.stop and plan.stop.level else None
    take = plan.take_profit.level.value if plan.take_profit and plan.take_profit.level else None
    if entry is None or stop is None or take is None:
        return "不可量化"
    risk = abs(entry - stop)
    return "不可量化" if risk <= 0 else f"{abs(take - entry) / risk:.2f}:1"


def _forecast_plain_status(forecast):
    status = str(_value(forecast.validation_status))
    if forecast.execution_eligible and status == "confirmation_passed":
        return "已通过历史检验，可用于交易判断"
    if forecast.execution_eligible and status == "noninferior_passed":
        return "历史表现与简单基准相当，只能小仓参考"
    if status == "insufficient_sample":
        return "历史样本确实不足"
    if status == "calibration_failed":
        return "行情样本可用，但模型概率不够可靠"
    if status == "evaluated_not_better":
        return "行情样本可用，但候选模型没有证明有效"
    return "当前是历史经验参考，尚未成为正式模型"


def _forecast_reason(forecast):
    if not forecast.drivers:
        return "根据现有历史价格、趋势和波动特征估计"
    labels = []
    for driver in forecast.drivers[:2]:
        label = _FEATURE_NAMES.get(driver.feature_name, driver.feature_name.replace("closed.", ""))
        if label not in labels:
            labels.append(label)
    return "主要参考" + "、".join(labels) if labels else "根据现有历史技术特征估计"


def _forecast_history_text(value, forecast):
    marker = f":h{forecast.horizon}:"
    snapshot = next(
        (
            item for item in value.metric_snapshots
            if _value(item.ledger_kind) == "forecast" and marker in item.scope_key
        ),
        None,
    )
    status = _forecast_plain_status(forecast)
    if snapshot is None:
        return f"{status}；详细历史成绩将在后台检验完成后显示"
    metrics = dict(snapshot.metrics)
    return (
        f"{status}；{snapshot.sample_count} 条未见历史样本中，"
        f"方向正确率 {format_percent(metrics.get('direction_accuracy'))}，"
        f"概率误差 {format_value(metrics.get('brier'))}，"
        f"80%收益范围命中 {format_percent(metrics.get('interval_hit_rate'))}"
    )


def _forecast_compact_text(forecast):
    if forecast.direction is None or forecast.return_distribution is None:
        return f"目标 {forecast.target_session_date or '待定'}：无法形成可靠预测"
    probability = forecast.probabilities.for_direction(forecast.direction) if forecast.probabilities else None
    return (
        f"{_display(_DIRECTION_NAMES, forecast.direction)} {format_percent(probability)}；"
        f"收益中位 {format_percent(forecast.return_distribution.p50)}；"
        f"目标 {forecast.target_session_date or '待定'}"
    )


def _sample_conclusion(sample_count, *, positive=None, subject="结果"):
    if sample_count == 0:
        return f"尚无到期{subject}，本次只能观察"
    if sample_count < 10:
        return f"仅 {sample_count} 条，不能判断稳定性"
    if sample_count < 30:
        return f"{sample_count} 条，可作观察，暂不能作为稳定依据"
    if positive is True:
        return f"{sample_count} 条，当前表现为正，仍需持续监控"
    if positive is False:
        return f"{sample_count} 条，当前表现未达要求，需要继续优化"
    return f"{sample_count} 条，已达到比较门槛"


def _portfolio_forecast_ability_row(value):
    matured = tuple(
        item for item in value.forecast_outcomes
        if str(_value(item.status)) == "matured" and item.direction_correct is not None
    )
    correct = sum(item.direction_correct is True for item in matured)
    brier = tuple(item.event_brier for item in matured if item.event_brier is not None)
    interval = tuple(
        item for item in matured
        if item.actual_return is not None and item.predicted_p10 is not None and item.predicted_p90 is not None
    )
    interval_hits = sum(item.predicted_p10 <= item.actual_return <= item.predicted_p90 for item in interval)
    pending = sum(str(_value(item.status)) == "pending" for item in value.forecast_outcomes)
    accuracy = None if not matured else Decimal(correct) / Decimal(len(matured))
    average_brier = None if not brier else sum(brier) / len(brier)
    interval_rate = None if not interval else Decimal(interval_hits) / Decimal(len(interval))
    positive = None if accuracy is None else accuracy >= Decimal("0.5")
    return _row(
        f"{value.instrument.stable_key}:forecast-ability",
        (
            value.instrument.code,
            len(matured),
            f"{correct}/{len(matured)}（{format_percent(accuracy)}）" if matured else "—",
            f"{average_brier:.3f}（越低越好）" if average_brier is not None else "—",
            format_percent(interval_rate),
            pending,
            _sample_conclusion(len(matured), positive=positive, subject="预测"),
        ),
        _refs(*value.forecast_outcomes) if value.forecast_outcomes else _refs(*value.forecasts),
    )


def _portfolio_strategy_ability_row(value):
    entries = tuple(
        item for item in value.strategy_outcomes
        if str(_value(item.status)) == "matured"
        and item.action in {"buy", "add"}
        and item.net_return is not None
    )
    exits = tuple(
        item for item in value.strategy_outcomes
        if str(_value(item.status)) == "matured"
        and item.action in {"sell", "reduce"}
        and item.exit_quality is not None
    )
    returns = tuple(Decimal(str(item.net_return)) for item in entries)
    qualities = tuple(Decimal(str(item.exit_quality)) for item in exits)
    benchmarks = tuple(
        Decimal(str(item.benchmark_return))
        for item in entries if item.benchmark_return is not None
    )
    wins = sum(item > 0 for item in returns)
    average = None if not returns else sum(returns, Decimal("0")) / len(returns)
    benchmark = (
        sum(benchmarks, Decimal("0")) / len(benchmarks)
        if benchmarks and len(benchmarks) == len(returns) else None
    )
    alpha = None if average is None or benchmark is None else average - benchmark
    adverse = tuple(Decimal(str(item.mae)) for item in entries if item.mae is not None)
    exit_average = None if not qualities else sum(qualities, Decimal("0")) / len(qualities)
    positive = None if average is None and exit_average is None else bool(
        (average is not None and average > 0) or (exit_average is not None and exit_average > 0)
    )
    return _row(
        f"{value.instrument.stable_key}:strategy-ability",
        (
            value.instrument.code,
            len(entries),
            format_percent(Decimal(wins) / Decimal(len(entries)) if entries else None),
            format_percent(average),
            format_percent(benchmark),
            format_percent(alpha),
            format_percent(min(adverse) if adverse else None),
            len(exits),
            format_percent(exit_average),
            _sample_conclusion(len(entries) + len(exits), positive=positive, subject="策略事件"),
        ),
        _refs(*value.strategy_outcomes) if value.strategy_outcomes else _refs(value.strategy_bundle),
    )


def _portfolio_joint_ability_row(value):
    snapshots = tuple(
        item for item in value.metric_snapshots
        if str(_value(item.ledger_kind)) == "joint"
    )
    by_profile = {}
    for item in sorted(snapshots, key=lambda entry: (entry.data_cutoff_at, entry.generated_at)):
        by_profile[str(item.scope_key).split(":")[-1]] = item
    conservative = by_profile.get("conservative")
    aggressive = by_profile.get("aggressive")
    sample_count = max((item.sample_count for item in by_profile.values()), default=0)
    return _row(
        f"{value.instrument.stable_key}:joint-ability",
        (
            value.instrument.code,
            sample_count,
            _metric_summary(conservative.metrics) if conservative else "尚无保守方案完整回放",
            _metric_summary(aggressive.metrics) if aggressive else "尚无激进方案完整回放",
            _sample_conclusion(sample_count, subject="完整链路回放"),
        ),
        _refs(*by_profile.values()) if by_profile else _refs(value.risk_bundle, value.order_intent_bundle),
    )


def _technical_position_text(value, market):
    current = _analysis_price(value)
    parts = []
    for name, label in (("closed.ma_20", "MA20"), ("closed.ma_60", "MA60"), ("closed.ma_120", "MA120")):
        level = _feature_number(value, name)
        if level is None:
            continue
        relative = "上方" if current is not None and current >= level else "下方"
        parts.append(f"{label} {_currency(level, market)}（现价在{relative}）")
    return "；".join(parts) if parts else "均线样本尚不足"


def _decision_priority(decision, protective_ids, readiness_by_plan=None, evidence_by_decision=None):
    readiness = None if readiness_by_plan is None else readiness_by_plan.get(decision.plan_id)
    readiness_priority = {
        "triggered": 0, "waiting": 1, "observation_only": 2, "not_applicable": 3,
    }.get(str(_value(readiness)), 4)
    action_priority = {
        PlanAction.SELL: 0, PlanAction.REDUCE: 1, PlanAction.HOLD: 2,
        PlanAction.ADD: 3, PlanAction.BUY: 3, PlanAction.WATCH: 4,
    }.get(decision.action, 5)
    level_priority = {"A": 0, "B": 1, "C": 2, "D": 3}.get(str(_value(decision.level)), 4)
    evidence = None if evidence_by_decision is None else evidence_by_decision.get(decision.decision_id)
    evidence_priority = {
        "reliable_positive": 0, "positive_uncertain": 1, "insufficient_sample": 2,
        "unavailable": 3, "negative": 4, "conflicting": 5,
    }.get(str(_value(getattr(evidence, "status", decision.evidence_status))), 6)
    expected = getattr(evidence, "expected_net_return", None)
    adverse = getattr(evidence, "max_adverse_excursion", None)
    return (
        not decision.executable_now, readiness_priority,
        decision.decision_id not in protective_ids, action_priority,
        evidence_priority, float("inf") if expected is None else -float(expected),
        float("inf") if adverse is None else abs(float(adverse)),
        level_priority, decision.decision_id,
    )


def _decision_evidence_map(value, plans):
    by_identity = {
        (item.strategy_id, item.strategy_version, item.parameter_hash, item.profile, item.action): item
        for item in value.learning_evidence
    }
    result = {}
    for decision in value.risk_bundle.decisions:
        plan = plans.get(decision.plan_id)
        if plan is None:
            continue
        identity = (
            plan.strategy_id,
            plan.strategy_version,
            plan.parameter_hash,
            decision.profile,
        )
        result[decision.decision_id] = (
            by_identity.get((*identity, plan.action.value))
            or by_identity.get((*identity, None))
        )
    return result


def _primary_decision(value):
    decisions = tuple(value.risk_bundle.decisions)
    conservative = tuple(item for item in decisions if item.profile is RiskProfile.CONSERVATIVE)
    pool = conservative or decisions
    plans = {
        plan.plan_id: plan
        for branch in (value.strategy_bundle.entry_or_add, value.strategy_bundle.reduce_or_exit, value.strategy_bundle.hold)
        for plan in branch.plans
    }
    readiness = {plan.plan_id: plan.readiness for plan in plans.values()}
    evidence = _decision_evidence_map(value, plans)
    return min(pool, key=lambda item: _decision_priority(
        item, set(value.risk_bundle.protective_decision_ids), readiness, evidence,
    ))


def _forecast_table(value):
    rows = []
    for forecast in value.forecasts:
        probs = forecast.probabilities
        distribution = forecast.return_distribution
        direction = _display(_DIRECTION_NAMES, forecast.direction) if forecast.direction else "暂时无法判断"
        probability = probs.for_direction(forecast.direction) if probs and forecast.direction else None
        probability_text = (
            f"上涨 {format_percent(probs.bullish)} / 震荡 {format_percent(probs.neutral)} / 下跌 {format_percent(probs.bearish)}"
            if probs else "没有可用概率"
        )
        range_text = (
            f"中间估计 {format_percent(distribution.p50)}；通常范围 {format_percent(distribution.p10)} 至 {format_percent(distribution.p90)}"
            if distribution else "没有可靠收益范围"
        )
        rows.append(_row(
            forecast.event_key,
            (
                f"未来 {forecast.horizon} 个交易日", forecast.target_session_date or "目标日暂不可确定",
                _currency(forecast.reference_price, value.instrument.market),
                f"{direction}（{format_percent(probability)}）" if probability is not None else direction,
                probability_text, range_text, _forecast_reason(forecast), _forecast_history_text(value, forecast),
                "可以参与新开仓判断" if forecast.execution_eligible else "不参与新开仓执行分级；只作观察",
            ),
            _refs(forecast),
        ))
    return ReportTable(
        "forecast_table", "未来走势预测",
        ("预测范围", "目标交易日", "预测时价格", "最可能走势", "三种走势概率", "预计收益", "简要理由", "过去表现是否可靠", "能否影响新开仓"),
        tuple(rows), "暂无可靠预测。",
        "先看目标交易日和三种走势概率。预计收益是一个范围，不是保证到达的目标价。",
    )


def _plan_table(value):
    decisions = {(item.plan_id, item.profile): item for item in value.risk_bundle.decisions}
    intents = {item.decision_id: item for item in value.order_intent_bundle.intents}
    rows = []
    branches = (
        ("买入/加仓", value.strategy_bundle.entry_or_add),
        ("卖出/减仓", value.strategy_bundle.reduce_or_exit),
        ("持有", value.strategy_bundle.hold),
        ("失效", value.strategy_bundle.invalidation),
    )
    for branch_name, branch in branches:
        if not branch.plans:
            rows.append(_row(
                branch_name, (branch_name, "不适用", _display(_READINESS_NAMES, branch.readiness), "—", "—", "—", "—", "不可量化", "—"),
                _refs(value.strategy_bundle), ReportSeverity.UNAVAILABLE,
            ))
            continue
        for plan in branch.plans:
            profiles = plan.profiles or (None,)
            for profile in profiles:
                risk_profile = RiskProfile(_value(profile)) if profile is not None else None
                decision = decisions.get((plan.plan_id, risk_profile))
                intent = intents.get(decision.decision_id) if decision else None
                stop_level = f"止损价 {plan.stop.level.value:.2f}；" if plan.stop and plan.stop.level else ""
                stop = stop_level + _condition_expression_text(plan.invalidation_condition)
                if plan.take_profit and plan.take_profit.level:
                    target = f"止盈价 {plan.take_profit.level.value:.2f}"
                elif plan.take_profit and plan.take_profit.condition:
                    target = _condition_expression_text(plan.take_profit.condition)
                else:
                    target = "未设置"
                refs = _refs(value.strategy_bundle, value.risk_bundle, value.order_intent_bundle)
                rows.append(_row(
                    f"{plan.plan_id}:{_value(profile) if profile else 'all'}",
                    (
                        branch_name, _action(plan.action), _display(_READINESS_NAMES, plan.readiness), _profile(profile) if profile else "全部",
                        _condition_text(plan), f"{stop}", f"{target}", _risk_reward(plan),
                        "—" if decision is None else f"{_display(_LEVEL_NAMES, decision.level)} / {decision.approved_shares}股 / 最大亏损 {_currency(decision.max_loss_amount, value.instrument.market)}" + (" / 已生成订单意图" if intent else ""),
                    ), refs,
                ))
    return ReportTable(
        "plan_table", "当前与条件交易计划",
        ("分支", "动作", "状态", "方案", "触发/条件", "止损或失效", "止盈", "风险收益比", "风控/订单"),
        tuple(rows), None,
        "保守与激进方案可共享触发价；差异来自确认门槛、风险预算、批准股数或仓位。",
    )


def _branch_plan_text(branch, *, invalidation=False, hold=False):
    if not branch.plans:
        return ""
    values = []
    for plan in branch.plans:
        if invalidation:
            condition = _condition_expression_text(plan.invalidation_condition)
        elif hold and plan.hold_condition is not None:
            condition = _condition_expression_text(plan.hold_condition)
        else:
            condition = _condition_text(plan)
        text = f"{_action(plan.action)}：{condition}"
        if text not in values:
            values.append(text)
    return " / ".join(values)


def _branch_summary(branch, *, held, kind):
    if not branch.plans:
        if kind == "exit":
            return "当前未持有，无需卖出" if not held else "当前没有额外退出条件"
        if kind == "hold":
            return "当前未持有，无需展示持有条件" if not held else "按当前保护条件继续持有"
        return "当前没有可执行的买入/加仓计划"
    actionable = [plan for plan in branch.plans if plan.action is not PlanAction.WATCH]
    if not actionable:
        if kind == "entry":
            return "当前不买入/不加仓；正式预测或交易条件尚未达到执行门槛"
        return "继续观察，尚无可执行条件"
    values = []
    for plan in actionable:
        condition = _condition_text(plan)
        state = "已满足" if str(_value(plan.readiness)) == "triggered" else "等待"
        text = f"{_action(plan.action)}：{state}“{condition}”"
        if text not in values:
            values.append(text)
    return " / ".join(values)


def _profile_decision_text(value, profile):
    decisions = tuple(item for item in value.risk_bundle.decisions if item.profile is profile)
    if not decisions:
        return "无对应方案"
    readiness = {
        plan.plan_id: plan.readiness
        for branch in (value.strategy_bundle.entry_or_add, value.strategy_bundle.reduce_or_exit, value.strategy_bundle.hold)
        for plan in branch.plans
    }
    decision = min(
        decisions,
        key=lambda item: _decision_priority(item, set(value.risk_bundle.protective_decision_ids), readiness),
    )
    if decision.action in {PlanAction.SELL, PlanAction.REDUCE}:
        risk = "退出不新增计划亏损"
    elif (decision.action in {PlanAction.WATCH, PlanAction.HOLD} or
          str(_value(decision.disposition)) in {"observe", "rejected"}):
        risk = "不下单、不新增风险"
    else:
        risk = f"最大亏损 {_currency(decision.max_loss_amount, value.instrument.market)}"
    return f"{_action(decision.action)}；{decision.approved_shares}股；{risk}；{_display(_DISPOSITION_NAMES, decision.disposition)}"


def _decision_state_text(decision, plan):
    if decision.executable_now:
        return "当前可执行"
    readiness = str(_value(plan.readiness)) if plan is not None else None
    if readiness == "triggered":
        return "条件已满足，待下一可交易时段复核"
    if readiness == "waiting":
        return "等待条件触发"
    if readiness == "observation_only":
        return "仅观察，不生成订单"
    return "当前不可执行"


def _portfolio_plan_rows(value):
    decision = _primary_decision(value)
    plan_by_id = {
        plan.plan_id: plan
        for branch in (
            value.strategy_bundle.entry_or_add,
            value.strategy_bundle.reduce_or_exit,
            value.strategy_bundle.hold,
            value.strategy_bundle.invalidation,
        )
        for plan in branch.plans
    }
    selected_plan = plan_by_id.get(decision.plan_id)
    held = str(_value(value.strategy_bundle.position_state)) != "flat"
    if selected_plan is None:
        current = f"{_action(decision.action)}；上游计划缺失，必须重新分析"
    else:
        family = _display(_STRATEGY_FAMILY_NAMES, selected_plan.family)
        validity = (
            f"有效期 {_datetime_text(selected_plan.valid_from, value.instrument.market)} 至 {_datetime_text(selected_plan.expires_at, value.instrument.market)}"
            if selected_plan.valid_from and selected_plan.expires_at else "观察条件随每次分析更新"
        )
        if selected_plan.action is PlanAction.WATCH:
            current = (
                f"观察；{family}；当前没有通过风控的交易条件。"
                f"预测模型未达执行门槛时，预测只用于观察；{validity}"
            )
        else:
            current = (
                f"{_action(decision.action)}；{family}；{_decision_state_text(decision, selected_plan)}；"
                f"分析价 {_price_text(value, value.instrument.market)}；依据“{_condition_text(selected_plan)}”；{validity}"
            )
    conservative = _profile_decision_text(value, RiskProfile.CONSERVATIVE)
    aggressive = _profile_decision_text(value, RiskProfile.AGGRESSIVE)
    common = _refs(value.scenario, value.strategy_bundle, value.risk_bundle, value.order_intent_bundle)
    return (
        _row(
            value.instrument.stable_key,
            (
                value.instrument.code,
                "持仓" if held else "关注",
                current,
                _branch_summary(value.strategy_bundle.entry_or_add, held=held, kind="entry"),
                _branch_summary(value.strategy_bundle.reduce_or_exit, held=held, kind="exit"),
                (
                    _branch_summary(value.strategy_bundle.hold, held=held, kind="hold")
                    if not held else
                    f"{_branch_summary(value.strategy_bundle.hold, held=held, kind='hold')}；失效：{_branch_plan_text(value.strategy_bundle.invalidation, invalidation=True) or '按退出条件复核'}"
                ),
                conservative,
                aggressive,
            ),
            common,
        ),
    )


def _portfolio_strategy_row(value):
    decision = _primary_decision(value)
    plans = {
        plan.plan_id: plan
        for branch in (value.strategy_bundle.entry_or_add, value.strategy_bundle.reduce_or_exit, value.strategy_bundle.hold)
        for plan in branch.plans
    }
    plan = plans.get(decision.plan_id)
    if plan is None:
        strategy_name = "没有可用策略"
        reason = "计划缺失，需要重新分析"
        history = "没有可验证结果"
    else:
        strategy_name = _display(_STRATEGY_FAMILY_NAMES, plan.family)
        reason = f"预测情景：{_display(_SCENARIO_STATE_NAMES, value.scenario.state)}；当前条件：{_condition_text(plan)}"
        outcomes = _matching_strategy_outcomes(value, plan, decision.profile)
        history = _strategy_history_text(outcomes)
    return _row(
        f"portfolio-strategy:{value.instrument.stable_key}",
        (value.instrument.code, strategy_name, _action(decision.action), reason, history),
        _refs(value.scenario, value.strategy_bundle, value.risk_bundle, *value.strategy_outcomes),
    )


def _risk_table(value):
    rows = []
    for decision in value.risk_bundle.decisions:
        rows.append(_row(
            decision.decision_id,
            (
                _profile(decision.profile), _action(decision.action), _display(_LEVEL_NAMES, decision.level),
                _display(_DISPOSITION_NAMES, decision.disposition), "是" if decision.executable_now else "否",
                decision.approved_shares, format_percent(decision.post_trade_position_pct),
                _currency(decision.risk_budget_amount, value.instrument.market),
                _currency(decision.max_loss_amount, value.instrument.market),
                _reason_text(decision.reason_codes),
            ), _refs(value.risk_bundle),
        ))
    for evidence in value.learning_evidence:
        rows.append(_row(
            evidence.evidence_id,
            ("历史证据", evidence.strategy_id, _display(_EVIDENCE_GRADE_NAMES, evidence.status), "否", "否", "—", "—", "—", "—", f"样本 {evidence.sample_count}，其中历史样本外 {evidence.oof_sample_count}"),
            _refs(evidence),
        ))
    return ReportTable(
        "risk_table", "真实账户风控结论",
        ("方案", "动作", "等级", "处置", "当前可执行", "批准股数", "交易后仓位", "风险预算", "最大亏损", "原因"),
        tuple(rows), None, "金额、股数和仓位均来自冻结账户风控，不使用模拟本金。",
    )


def _strategy_choice_table(value):
    plan_by_id = {
        plan.plan_id: plan
        for branch in (value.strategy_bundle.entry_or_add, value.strategy_bundle.reduce_or_exit, value.strategy_bundle.hold)
        for plan in branch.plans
    }
    rows = []
    evidence = _decision_evidence_map(value, plan_by_id)
    for profile in (RiskProfile.CONSERVATIVE, RiskProfile.AGGRESSIVE):
        decisions = tuple(item for item in value.risk_bundle.decisions if item.profile is profile)
        if not decisions:
            continue
        readiness = {plan.plan_id: plan.readiness for plan in plan_by_id.values()}
        decision = min(
            decisions,
            key=lambda item: _decision_priority(item, set(value.risk_bundle.protective_decision_ids), readiness, evidence),
        )
        plan = plan_by_id.get(decision.plan_id)
        if plan is None:
            strategy_name = "没有可用策略"
            reason = "上游计划缺失，需要重新分析"
            history = "没有可验证结果"
        else:
            strategy_name = _display(_STRATEGY_FAMILY_NAMES, plan.family)
            reason = f"当前预测情景为{_display(_SCENARIO_STATE_NAMES, value.scenario.state)}；{_condition_text(plan)}"
            outcomes = _matching_strategy_outcomes(value, plan, profile)
            history = _strategy_history_text(outcomes)
        rows.append(_row(
            f"strategy:{_value(profile)}:{decision.decision_id}",
            (_profile(profile), strategy_name, _action(decision.action), reason, history),
            _refs(value.scenario, value.strategy_bundle, value.risk_bundle, *value.strategy_outcomes),
        ))
    return ReportTable(
        "strategy_choice", "根据预测选择的策略",
        ("方案", "采用的思路", "当前动作", "为什么选择", "该策略过去表现"),
        tuple(rows), "当前没有可用策略。",
        "策略必须先符合当前预测情景，再比较同一股票上的历史收益与回撤；没有历史回放时会明确标注。",
    )


def _operation_table(value):
    plans = {
        plan.plan_id: plan
        for branch in (value.strategy_bundle.entry_or_add, value.strategy_bundle.reduce_or_exit, value.strategy_bundle.hold)
        for plan in branch.plans
    }
    rows = []
    evidence = _decision_evidence_map(value, plans)
    for profile in (RiskProfile.CONSERVATIVE, RiskProfile.AGGRESSIVE):
        decisions = tuple(item for item in value.risk_bundle.decisions if item.profile is profile)
        if not decisions:
            continue
        readiness = {plan.plan_id: plan.readiness for plan in plans.values()}
        decision = min(
            decisions,
            key=lambda item: _decision_priority(item, set(value.risk_bundle.protective_decision_ids), readiness, evidence),
        )
        plan = plans.get(decision.plan_id)
        if plan is None:
            cells = (_profile(profile), "重新分析", "—", "—", "—", "—", "—", "上游计划缺失")
        else:
            stop = (
                _currency(plan.stop.level.value, value.instrument.market)
                if plan.stop and plan.stop.level else _condition_expression_text(plan.invalidation_condition)
            )
            target = (
                _currency(plan.take_profit.level.value, value.instrument.market)
                if plan.take_profit and plan.take_profit.level else
                _condition_expression_text(plan.take_profit.condition)
                if plan.take_profit and plan.take_profit.condition else "按条件退出，暂无固定目标价"
            )
            risk = "不新增风险" if decision.action in {PlanAction.SELL, PlanAction.REDUCE, PlanAction.HOLD, PlanAction.WATCH} else _currency(decision.max_loss_amount, value.instrument.market)
            cells = (
                _profile(profile), _action(decision.action), f"{decision.approved_shares} 股",
                _condition_text(plan), stop, target, risk,
                f"{_datetime_text(plan.valid_from, value.instrument.market)} 至 {_datetime_text(plan.expires_at, value.instrument.market)}",
            )
        rows.append(_row(
            f"operation:{_value(profile)}:{decision.decision_id}", cells,
            _refs(value.strategy_bundle, value.risk_bundle, value.order_intent_bundle),
        ))
    return ReportTable(
        "operation_table", "保守与激进操作计划",
        ("方案", "当前动作", "数量", "达到什么条件执行", "判断错了在哪里退出", "盈利后怎么处理", "最大计划亏损", "计划有效期"),
        tuple(rows), "当前没有操作计划。",
        "保守和激进方案可能使用同一触发价，但确认要求、数量和最大风险不同。",
    )


def _facts_table(value):
    latest_date = value.feature_snapshot.latest_bar_date or "无完成日K"
    listing = value.metadata.listing_date or "未取得"
    rows = [
        _row("metadata", ("股票", f"{value.metadata.name} ({value.instrument.code})", f"{'A股' if _value(value.instrument.market) == 'A' else '美股'}；上市日期 {listing}", value.metadata.source), _refs(value.metadata)),
        _row("price", ("本次分析价格", _price_text(value, value.instrument.market), f"对应完成日K {latest_date}", _price_source_text(value)), _refs(value.quote_snapshot, value.feature_snapshot, *value.forecasts)),
        _row("technical", ("关键价格位置", _technical_position_text(value, value.instrument.market), "用于核对趋势位置", _price_source_text(value)), _refs(value.feature_snapshot)),
        _row("quality", ("行情可用性", _display(_QUALITY_STATUS_NAMES, value.data_quality.status), _reason_text(item.code for item in value.data_quality.issues) if value.data_quality.issues else "没有发现行情异常", "系统质量检查"), _refs(value.data_quality)),
        _row("news", ("新闻", f"取得 {len(value.news_summary)} 条" if value.news_summary else "暂无可靠数据", value.news_summary[0].title if value.news_summary else "本次未取得；不影响纯技术历史模型继续验证", value.news_summary[0].source if value.news_summary else "—"), _refs(*value.news_summary) if value.news_summary else (PRESENTATION_POLICY_REF,)),
    ]
    if value.fundamental_summary:
        fields = "；".join(f"{name} {item.value}{item.unit or ''}" for name, item in value.fundamental_summary.fields.items()) or "没有可用字段"
        rows.append(_row("fundamental", ("基本面", fields, "当前快照，不倒填历史", value.fundamental_summary.provider), _refs(value.fundamental_summary)))
    else:
        rows.append(_row("fundamental:none", ("基本面", "暂无可靠数据（本次未取得）", "不影响纯技术历史模型继续验证", "—"), (PRESENTATION_POLICY_REF,)))
    return ReportTable("facts_table", "基本信息与数据核对", ("项目", "系统实际使用的内容", "怎么理解", "来源"), tuple(rows), None, "先核对股票、交易日、分析价格和来源；辅助数据缺失与预测模型未通过是两件不同的事。")


def _research_table(value):
    hypotheses = {item.hypothesis_id: item for item in value.research_hypotheses}
    outcomes = {item.hypothesis_id: item for item in value.research_outcomes}
    rows = []
    for validation in value.research_validations:
        hypothesis = hypotheses.get(validation.hypothesis_id)
        outcome = outcomes.get(validation.hypothesis_id)
        artifacts = tuple(item for item in (hypothesis, validation, outcome) if item is not None)
        rows.append(_row(
            validation.validation_id,
            (
                hypothesis.title if hypothesis else validation.hypothesis_id,
                _display(_RESEARCH_STATUS_NAMES, validation.status),
                _display(_CANDIDATE_ELIGIBILITY_NAMES, validation.candidate_eligibility),
                "—" if outcome is None else _display(_RESEARCH_OUTCOME_NAMES, outcome.status),
                _reason_text(validation.reason_codes),
            ), _refs(*artifacts),
        ))
    metric_rows = [
        _row(item.snapshot_id, ("研究指标", item.scope_key, "—", "—", str(dict(item.metrics))), _refs(item))
        for item in value.research_metric_snapshots
    ]
    rows.extend(metric_rows)
    return ReportTable(
        "research_table", "研究员观察与系统验证",
        ("观察", "验证状态", "候选资格", "历史结果", "原因与技术详情"),
        tuple(rows), "研究员观察不可用；确定性报告仍然完整。",
        "研究观察不会改写正式动作，也不会直接产生订单。",
    )


def _history_tables(value):
    forecast_rows = []
    recent_forecasts = tuple(sorted(
        value.forecast_outcomes,
        key=lambda item: (item.target_session_date, item.forecast_outcome_id),
        reverse=True,
    ))[:20]
    for item in recent_forecasts:
        result = "待验证" if _value(item.status) == "pending" else "正确" if item.direction_correct else "错误" if item.direction_correct is False else "不可验证"
        reference_price = None
        if item.actual_price is not None and item.actual_return is not None and Decimal("1") + item.actual_return > 0:
            reference_price = item.actual_price / (Decimal("1") + item.actual_return)
        forecast_rows.append(_row(
            item.forecast_outcome_id,
            (
                value.metadata.name + f" ({value.instrument.code})", item.origin_session_date,
                _currency(reference_price, value.instrument.market) if reference_price is not None else "待到期后核验",
                item.target_session_date, f"{_value(item.predicted_direction)} / {format_percent(item.probabilities.for_direction(item.predicted_direction) if item.probabilities and item.predicted_direction else None)}",
                f"{format_percent(item.predicted_p10)} / {format_percent(item.predicted_p50)} / {format_percent(item.predicted_p90)}",
                item.target_session_date if item.actual_price is not None else "—",
                f"{_currency(item.actual_price, value.instrument.market)} / {format_percent(item.actual_return)}" if item.actual_price is not None else "—",
                result, f"{_value(item.evidence_origin)} / {_value(item.evidence_grade)}",
            ), _refs(item),
        ))
    forecasts = ReportTable(
        "verified_forecasts", "最近已验证预测",
        ("股票", "预测时间", "参考价", "目标交易日", "主要预测", "预测收益区间", "实际日期", "实际价格/收益", "结果", "证据说明"),
        tuple(forecast_rows), "暂无已到期预测。", "预测账只评价预测本身是否准确，不会覆盖当前风险动作。",
    )
    strategy_summary = _outcome_summary(value.instrument, "strategy", value.strategy_outcomes)
    strategies = ReportTable(
        "strategy_outcomes", "策略历史表现",
        ("股票", "评估对象", "样本/记录", "通俗结果"),
        () if strategy_summary is None else (strategy_summary,),
        "暂无成熟策略结果。",
        "这里评价交易计划本身；单次信号窗口可能重叠，因此不冒充连续账户收益。",
    )
    joint_rows = tuple(
        _row(item.snapshot_id, (
            value.instrument.code, _metric_scope_text(item.scope_key),
            f"{item.sample_count} 个历史决策", _metric_summary(item.metrics),
        ), _refs(item))
        for item in value.metric_snapshots
        if str(_value(item.ledger_kind)) == "joint"
    )
    joint = ReportTable(
        "joint_outcomes", "完整链路历史表现",
        ("股票", "评估对象", "样本/记录", "通俗结果"),
        joint_rows,
        "完整链路汇总会在后台历史回放完成后的下一次分析中显示。",
        "完整链路同时评价预测、策略、风控、组合分配和成交；不要求机械跑赢基准，也会比较回撤控制。",
    )
    metric_rows = [
        _row(item.snapshot_id, (
            _display(_LEDGER_NAMES, item.ledger_kind), _metric_scope_text(item.scope_key),
            item.sample_count, _metric_summary(item.metrics),
            _datetime_text(item.data_cutoff_at, value.instrument.market),
        ), _refs(item))
        for item in value.metric_snapshots
        if str(_value(item.ledger_kind)) != "joint"
    ]
    metrics = ReportTable(
        "learning_metrics", "历史统计摘要",
        ("评估对象", "预测/方案", "样本数", "结果", "数据截止时间"),
        tuple(metric_rows), "暂无历史统计摘要。",
        "先看样本数，再看方向正确率、收益和回撤；少于 30 条时不据此宣称稳定有效。",
    )
    return forecasts, strategies, joint, metrics


def _single_quick_action(value, operation_table):
    decision = _primary_decision(value)
    plans = {
        plan.plan_id: plan
        for branch in (
            value.strategy_bundle.entry_or_add,
            value.strategy_bundle.reduce_or_exit,
            value.strategy_bundle.hold,
            value.strategy_bundle.invalidation,
        )
        for plan in branch.plans
    }
    selected_plan = plans.get(decision.plan_id)
    judgment = _signal_judgment_text(decision, selected_plan)
    steps = _signal_steps_text(selected_plan, decision, value.instrument.market)
    profile_lines = []
    for row in operation_table.rows:
        profile, action, quantity, _condition, _stop, _target, risk, _validity = row.cells
        if quantity in {"0 股", "—"} or action in {"观察", "持有", "重新分析"}:
            profile_lines.append(f"{profile}：暂不下单；{risk}")
        else:
            profile_lines.append(f"{profile}：{action} {quantity}；最大计划亏损 {risk}")
    table = ReportTable(
        "single_quick_action", "本轮操作结论",
        ("股票", "身份与价格", "当前动作", "下一步条件", "保守与激进"),
        (_row(
            f"single-quick:{value.instrument.stable_key}",
            (
                value.instrument.code,
                f"{value.metadata.name} · {_price_text(value, value.instrument.market)}",
                f"{_action(decision.action)}；{judgment}",
                steps,
                "\n".join(profile_lines),
            ),
            _refs(value.metadata, value.feature_snapshot, value.strategy_bundle, value.risk_bundle),
        ),),
        None,
        "这里只给出一分钟结论；精确价格、数量、止损、止盈和有效期见详细操作报告。",
    )
    return table


class SingleStockReportBuilder:
    renderer_version = "presentation_v2"

    def build(self, value: SingleStockPresentationInput, editorial=None) -> ReportDocument:
        decision = _primary_decision(value)
        action_refs = _refs(value.risk_bundle, value.order_intent_bundle)
        action_text = (
            f"当前动作：{_action(decision.action)} ｜ 执行等级：{_display(_LEVEL_NAMES, decision.level)} ｜ "
            f"{'当前可执行' if decision.executable_now else '当前不可执行，等待冻结条件'}\n"
            f"批准数量：{decision.approved_shares} 股 ｜ 最大计划亏损：{_currency(decision.max_loss_amount, value.instrument.market)}"
        )
        history_tables = _history_tables(value)
        operation = _operation_table(value)
        quick_action = _single_quick_action(value, operation)
        editorial_table = _editorial_table(quick_action, {value.instrument.code: value}, editorial)
        verified_forecasts, strategy_outcomes, joint_outcomes, learning_metrics = history_tables
        sections = (
            _section(
                "action_summary", "操作总结", "先用一分钟确认现在该做什么",
                _callout(action_text, action_refs), _table(quick_action, _table_refs(quick_action)),
            ),
            _section(
                "facts", "基本信息与数据核对", "先确认股票、日期、价格和来源是否正确",
                _table(_facts_table(value), _refs(value.metadata, value.quote_snapshot, value.data_quality, value.feature_snapshot, value.news_summary, value.fundamental_summary)),
            ),
            _section(
                "forecast", "未来走势预测", "用颜色和概率说明目标日期、可能方向和收益范围",
                _table(_forecast_table(value), _refs(*value.forecasts)),
            ),
            _section(
                "operation_report", "详细操作报告", "先读通俗解读，再核对冻结的触发、退出、数量和风险",
                _table(editorial_table, _table_refs(editorial_table)),
                _table(operation, _refs(value.strategy_bundle, value.risk_bundle, value.order_intent_bundle)),
                _table(_plan_table(value), _refs(value.strategy_bundle, value.risk_bundle, value.order_intent_bundle)),
                _table(_risk_table(value), _refs(value.risk_bundle, *value.learning_evidence)),
            ),
            _section(
                "strategy_performance", "策略表现与回测依据", "说明为什么选择该策略，以及过去的收益和回撤是否支持",
                _table(_strategy_choice_table(value), _refs(value.scenario, value.strategy_bundle, value.risk_bundle, *value.strategy_outcomes)),
                _table(strategy_outcomes, _table_refs(strategy_outcomes)),
                _table(joint_outcomes, _table_refs(joint_outcomes)),
            ),
            _section(
                "research", "研究员补充观察", "LLM 研究员只补充机会和质疑，不改写正式操作",
                _table(_research_table(value), _refs(*value.research_hypotheses, *value.research_validations, *value.research_outcomes, *value.research_metric_snapshots) if (value.research_hypotheses or value.research_validations or value.research_outcomes or value.research_metric_snapshots) else (PRESENTATION_POLICY_REF,)),
            ),
            _section(
                "history", "系统历史可信度", "最后分开核对预测是否准确，以及统计样本是否足够",
                _table(verified_forecasts, _table_refs(verified_forecasts)),
                _table(learning_metrics, _table_refs(learning_metrics)),
            ),
        )
        title = f"{value.metadata.name} ({value.instrument.code}) 单股研究报告"
        identity = {"kind": ReportKind.SINGLE_STOCK, "market": value.instrument.market, "instrument": value.instrument, "mode": value.analysis_mode, "as_of": value.as_of, "title": title, "subtitle": value.history_period, "summary": action_text, "sections": sections, "glossary": _GLOSSARY, "refs": value.source_artifact_refs, "schema": 1, "renderer": self.renderer_version}
        return ReportDocument(stable_hash(identity), ReportKind.SINGLE_STOCK, value.instrument.market, value.instrument, value.analysis_mode, value.as_of, title, value.history_period, action_text, sections, _GLOSSARY, value.source_artifact_refs, 1, self.renderer_version, value.built_at)


class PortfolioReportBuilder:
    renderer_version = "presentation_v2"

    def build(self, value: PortfolioPresentationInput, editorial=None) -> ReportDocument:
        valuation = value.frozen_account_valuation
        bundle = value.portfolio_decision_bundle
        account_refs = _refs(value.account_snapshot, valuation)
        portfolio_refs = _refs(bundle)
        summary = (
            f"冻结账户权益 {_currency(valuation.equity, value.market)} ｜ 现金 {_currency(valuation.cash, value.market)} ｜ "
            f"持仓市值 {_currency(valuation.invested_value, value.market)} ｜ 总仓位 {format_percent(valuation.invested_pct)}\n"
            f"估值时点：{_datetime_text(valuation.valuation_at, seconds=True)}"
        )
        priority_rows = []
        profile_rows = []
        replacement_rows = []
        member_by_instrument = {item.instrument: item for item in value.instruments}
        valuation_map = {item.instrument: item for item in valuation.position_values}
        account_position_map = {item.instrument: item for item in value.account_snapshot.positions}
        allocation_details = {}
        for profile in (bundle.conservative, bundle.aggressive):
            by_id = {item.allocation_id: item for item in profile.allocations}
            ordered_ids = profile.holding_priority_allocation_ids + profile.entry_priority_allocation_ids
            shown_instruments = set()
            for allocation_id in ordered_ids:
                item = by_id[allocation_id]
                status = str(_value(item.status))
                if status not in {"allocated_now", "reserved_conditional", "shared_exit_reservation"}:
                    continue
                if item.instrument in shown_instruments:
                    continue
                shown_instruments.add(item.instrument)
                member = member_by_instrument[item.instrument]
                valued = valuation_map.get(item.instrument)
                position_context = (
                    f"{format_percent(valued.position_pct)}仓位；盈亏 {format_percent(valued.unrealized_pnl_pct)}"
                    if valued is not None else "关注股，当前未持有"
                )
                arrangement = _allocation_status(item, member)
                allocation_details[(item.instrument.code, _profile(profile.profile))] = (item, arrangement)
                priority_rows.append(_row(
                        f"{_value(profile.profile)}:{item.allocation_id}",
                    (
                        _profile(profile.profile), item.instrument.code, _price_text(member, value.market),
                        position_context, _action(item.action), item.final_requested_shares,
                        _allocation_explanation(item, member, value.market),
                        arrangement,
                    ),
                    portfolio_refs,
                ))
            reserve = profile.reservation_snapshot
            profile_rows.append(_row(
                _value(profile.profile),
                (
                    _profile(profile.profile), _currency(reserve.frozen_cash, value.market),
                    _currency(reserve.deployable_cash, value.market), _currency(reserve.reserved_entry_cash, value.market),
                    _currency(reserve.remaining_cash, value.market), format_percent(reserve.projected_invested_pct_at_reference_price),
                    _portfolio_heat_text(profile), _portfolio_evidence_text(profile),
                ),
                portfolio_refs,
            ))
            for item in profile.replacement_candidates:
                replacement_rows.append(_row(
                    item.replacement_id,
                    (_profile(profile.profile), item.source_instrument.code, item.target_instrument.code, _display(_REPLACEMENT_STATUS_NAMES, item.status), "需重新分析" if item.reanalysis_required else "—"),
                    portfolio_refs,
                ))
        priority_title = {
            "pre": "开盘后优先处理", "intraday": "盘中优先处理", "eod": "下一交易时段优先处理",
        }.get(str(_value(value.analysis_mode)), "优先处理")
        priorities = ReportTable(
            "priority_actions", priority_title,
            ("方案", "股票", "分析价", "当前持仓", "首要动作", "股数", "为什么", "执行安排"),
            tuple(priority_rows), "当前没有需要执行或预留的组合动作。",
            "先看分析价和触发依据；盘后计划在下一交易时段用最新价复核，任一退出成交后按剩余持仓重算。",
        )
        profiles = ReportTable(
            "portfolio_profiles", "保守与激进组合方案",
            ("方案", "账户现金", "本轮可新增", "已预留买入", "预留后可新增", "预计仓位", "组合风险状态", "风险证据完整度"),
            tuple(profile_rows), "暂无可靠组合分配。",
            "组合风险状态衡量持仓到保护线的计划亏损。若价格已越过保护线，会直接提示先退出，而不是显示一个失真的百分比；这与行情数据是否完整、预测是否通过历史样本外检验是三件不同的事。",
        )
        holding_rows = []
        for position in value.account_snapshot.positions:
            item = valuation_map.get(position.instrument)
            holding_rows.append(_row(
                position.instrument.stable_key,
                (position.instrument.code, position.shares, _currency(position.cost_price, value.market), _currency(item.price if item else None, value.market), _currency(item.unrealized_pnl_amount if item else None, value.market), format_percent(item.unrealized_pnl_pct if item else None), format_percent(item.position_pct if item else None)),
                account_refs,
            ))
        holdings = ReportTable("holdings", "持仓风险表", ("股票", "数量", "成本", "现价", "浮盈亏", "收益率", "集中度"), tuple(holding_rows), "当前账户没有持仓。", "持仓数据来自用户账户快照和同一冻结估值。")
        plan_rows = []
        fact_rows = []
        quality_rows = []
        forecast_rows = []
        strategy_rows = []
        research_rows = []
        forecast_ability_rows = []
        strategy_ability_rows = []
        joint_ability_rows = []
        exit_signal_rows = []
        entry_signal_rows = []
        hold_signal_rows = []
        quick_action_seeds = []
        holding_detail_tables = []
        watch_detail_tables = []
        for member in value.instruments:
            member_refs = member.source_artifact_refs
            plan_row = _portfolio_plan_rows(member)[0]
            plan_rows.append(plan_row)
            valued = valuation_map.get(member.instrument)
            position = account_position_map.get(member.instrument)
            held = valued is not None
            cost_and_pnl = (
                f"成本 {_currency(position.cost_price if position else None, value.market)}；浮盈亏 {_currency(valued.unrealized_pnl_amount, value.market)}（{format_percent(valued.unrealized_pnl_pct)}）"
                if held else "未持有"
            )
            mode_name = {"pre": "盘前参考价", "intraday": "盘中实时价", "eod": "盘后收盘价"}.get(str(_value(value.analysis_mode)), "分析价")
            fact_row = _row(
                member.instrument.stable_key,
                (
                    f"{member.metadata.name} ({member.instrument.code})",
                    "持仓" if held else "关注",
                    member.feature_snapshot.latest_bar_date or "无完成日K",
                    f"{mode_name} {_price_text(member, value.market)}",
                    _price_source_text(member),
                    cost_and_pnl,
                    format_percent(valued.position_pct) if held else "0%",
                    _technical_position_text(member, value.market),
                ),
                _refs(member.metadata, member.feature_snapshot, value.account_snapshot, valuation),
            )
            fact_rows.append(fact_row)
            if member.quote_snapshot is not None:
                quote_source = member.quote_snapshot.source
            elif str(_value(value.analysis_mode)) == "eod":
                quote_source = "盘后已完成日K（无需实时快照）"
            else:
                quote_source = "实时快照不可用"
            capabilities = member.data_quality.capabilities
            history_state = (
                "日K可计算120日均线" if capabilities.ma120 else
                "日K可计算60日指标" if capabilities.medium_technical_60 else
                "历史日K不足60日"
            )
            attention = "；".join(item.message for item in member.data_quality.issues) or "未发现异常"
            quality_rows.append(_row(member.instrument.stable_key, (
                member.metadata.name, member.instrument.code,
                member.feature_snapshot.latest_bar_date or "无完成日K", history_state,
                quote_source, f"{len(member.news_summary)} 条" if member.news_summary else "本次未取得",
                "已取得" if member.fundamental_summary else "本次未取得", attention,
            ), _refs(member.metadata, member.quote_snapshot, member.data_quality, member.feature_snapshot, member.news_summary, member.fundamental_summary)))
            strategy_row = _portfolio_strategy_row(member)
            strategy_rows.append(strategy_row)
            forecast_by_horizon = {item.horizon: item for item in member.forecasts}
            forecasts_for_row = tuple(forecast_by_horizon[item] for item in (1, 3, 5, 10))
            eligible = tuple(item.horizon for item in forecasts_for_row if item.execution_eligible)
            forecast_row = _row(
                member.instrument.stable_key,
                (
                    member.instrument.code,
                    _currency(forecasts_for_row[0].reference_price, value.market),
                    _forecast_compact_text(forecasts_for_row[0]),
                    _forecast_compact_text(forecasts_for_row[1]),
                    _forecast_compact_text(forecasts_for_row[2]),
                    _forecast_compact_text(forecasts_for_row[3]),
                    "可参与执行：" + "、".join(f"{item}日" for item in eligible)
                    if eligible else "当前周期均只作观察",
                ),
                _refs(*forecasts_for_row),
            )
            forecast_rows.append(forecast_row)
            hypothesis_by_id = {item.hypothesis_id: item for item in member.research_hypotheses}
            for validation in member.research_validations:
                hypothesis = hypothesis_by_id.get(validation.hypothesis_id)
                research_rows.append(_row(validation.validation_id, (
                    member.instrument.code,
                    (f"{hypothesis.title}：{hypothesis.thesis}" if hypothesis else "研究观察记录缺失"),
                    _display(_RESEARCH_STATUS_NAMES, validation.status),
                    _display(_CANDIDATE_ELIGIBILITY_NAMES, validation.candidate_eligibility),
                    _reason_text(validation.reason_codes),
                ), _refs(validation)))
            forecast_ability_row = _portfolio_forecast_ability_row(member)
            strategy_ability_row = _portfolio_strategy_ability_row(member)
            joint_ability_row = _portfolio_joint_ability_row(member)
            forecast_ability_rows.append(forecast_ability_row)
            strategy_ability_rows.append(strategy_ability_row)
            joint_ability_rows.append(joint_ability_row)

            decision = _primary_decision(member)
            if decision.action in {PlanAction.SELL, PlanAction.REDUCE}:
                signal_target = exit_signal_rows
                quick_priority = 0
            elif decision.action in {PlanAction.BUY, PlanAction.ADD}:
                signal_target = entry_signal_rows
                quick_priority = 1
            else:
                signal_target = hold_signal_rows
                quick_priority = 2
            plans_by_id = {
                plan.plan_id: plan
                for branch in (
                    member.strategy_bundle.entry_or_add,
                    member.strategy_bundle.reduce_or_exit,
                    member.strategy_bundle.hold,
                    member.strategy_bundle.invalidation,
                )
                for plan in branch.plans
            }
            selected_plan = plans_by_id.get(decision.plan_id)
            if selected_plan is None:
                quick_current = f"{_action(decision.action)}；{_signal_judgment_text(decision, selected_plan)}"
                quick_condition = _signal_steps_text(selected_plan, decision, value.market)
            else:
                quick_current = f"{_action(decision.action)}；{_signal_judgment_text(decision, selected_plan)}"
                quick_condition = _signal_steps_text(selected_plan, decision, value.market)
            profile_summary = _signal_profile_text(member, allocation_details)
            quick_action_seeds.append((
                quick_priority,
                _row(
                    f"quick-seed:{member.instrument.stable_key}",
                    (
                        member.instrument.code,
                        f"{'持仓' if held else '关注'} · {_price_text(member, value.market)}",
                        quick_current,
                        quick_condition,
                        profile_summary,
                    ),
                    tuple(sorted(set((*plan_row.source_artifact_refs, *fact_row.source_artifact_refs)))),
                ),
            ))
            signal_target.append(_row(
                f"signal:{member.instrument.stable_key}",
                (
                    member.instrument.code,
                    "持仓" if held else "关注",
                    _price_text(member, value.market),
                    _signal_judgment_text(decision, selected_plan),
                    _signal_steps_text(selected_plan, decision, value.market),
                    profile_summary,
                ),
                tuple(sorted(set((*plan_row.source_artifact_refs, *fact_row.source_artifact_refs)))),
            ))

            detail_rows = (
                _row(
                    f"{member.instrument.stable_key}:identity",
                    (
                        "股票与价格",
                        f"{fact_row.cells[0]}；{fact_row.cells[1]}；最新完成日K {fact_row.cells[2]}；"
                        f"{fact_row.cells[3]}；来源 {fact_row.cells[4]}",
                    ),
                    fact_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:position",
                    ("持仓情况", f"{fact_row.cells[5]}；组合占比 {fact_row.cells[6]}"),
                    fact_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:technical",
                    ("关键技术位置", fact_row.cells[7]),
                    fact_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:forecast",
                    (
                        "未来走势",
                        f"1日：{forecast_row.cells[2]}；3日：{forecast_row.cells[3]}；"
                        f"5日：{forecast_row.cells[4]}；10日：{forecast_row.cells[5]}；{forecast_row.cells[6]}",
                    ),
                    forecast_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:strategy",
                    (
                        "采用策略",
                        f"{strategy_row.cells[1]}；当前动作 {_action(decision.action)}；"
                        f"{strategy_row.cells[3]}；历史表现：{strategy_row.cells[4]}",
                    ),
                    strategy_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:current",
                    ("当前应对", plan_row.cells[2]),
                    plan_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:entry",
                    ("买入或加仓", plan_row.cells[3]),
                    plan_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:exit",
                    ("卖出或减仓", plan_row.cells[4]),
                    plan_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:hold",
                    ("持有与失效", plan_row.cells[5]),
                    plan_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:profiles",
                    ("风险方案", f"保守：{plan_row.cells[6]}；激进：{plan_row.cells[7]}"),
                    plan_row.source_artifact_refs,
                ),
                _row(
                    f"{member.instrument.stable_key}:history",
                    (
                        "历史可信度",
                        f"预测：{forecast_ability_row.cells[-1]}；策略：{strategy_ability_row.cells[-1]}；"
                        f"完整链路：{joint_ability_row.cells[-1]}",
                    ),
                    tuple(sorted(set((
                        *forecast_ability_row.source_artifact_refs,
                        *strategy_ability_row.source_artifact_refs,
                        *joint_ability_row.source_artifact_refs,
                    )))),
                ),
            )
            detail_key = member.instrument.code.replace(".", "_").replace("-", "_")
            detail_table = ReportTable(
                f"stock_detail_{_value(value.market).lower()}_{detail_key}",
                f"{member.metadata.name} ({member.instrument.code}) 详细解读",
                ("重点", "结论与说明"),
                detail_rows,
                None,
                "先看“当前应对”，再核对未来走势、交易条件和历史可信度；详细内容不会改变前面的组合优先级。",
            )
            (holding_detail_tables if held else watch_detail_tables).append(detail_table)
            research_artifacts = (*member.research_hypotheses, *member.research_outcomes, *member.research_metric_snapshots)
            if research_artifacts:
                research_rows.append(_row(
                    f"{member.instrument.stable_key}:research-summary",
                    (member.instrument.code, f"历史研究汇总：观察 {len(member.research_hypotheses)} 条", "系统追踪", "研究账", f"到期结果 {len(member.research_outcomes)} 条；指标快照 {len(member.research_metric_snapshots)} 份"),
                    _refs(*research_artifacts),
                ))
        plans = ReportTable(
            "portfolio_plans",
            "逐股条件与当前动作",
            ("股票", "身份", "当前结论与依据", "买入/加仓条件", "卖出/减仓条件", "持有与失效", "保守方案", "激进方案"),
            tuple(plan_rows),
            "暂无逐股计划。",
            "每只股票只占一行。未持有股票不展开无意义的卖出和持有计划；先执行当前动作，未来买入条件不会抵消当前卖出或减仓结论。",
        )
        signal_columns = ("股票", "身份", "分析价", "当前判断", "触发步骤", "执行方案")
        exit_signals = ReportTable(
            "portfolio_exit_signals",
            f"先处理：卖出或减仓（{len(exit_signal_rows)}只）",
            signal_columns,
            tuple(exit_signal_rows),
            "当前没有需要优先卖出或减仓的股票。",
            "这一组优先保护已有资金。盘后结论会在下一交易时段用最新价格复核，不会把未来重新买入条件误当作当前买入建议。",
        )
        entry_signals = ReportTable(
            "portfolio_entry_signals",
            f"再寻找：买入或加仓候选（{len(entry_signal_rows)}只）",
            signal_columns,
            tuple(entry_signal_rows),
            "当前没有达到执行门槛的买入或加仓候选。",
            "只有预测、策略和风控共同通过时才进入这一组；未触发的价格条件仍属于等待计划。",
        )
        hold_signals = ReportTable(
            "portfolio_hold_signals",
            f"继续跟踪：持有或观察（{len(hold_signal_rows)}只）",
            signal_columns,
            tuple(hold_signal_rows),
            "当前没有单纯持有或观察的股票。",
            "“观察”不等于看空，而是当前没有足够证据下单；下一步条件说明系统在等待什么。",
        )
        quick_action_rows = []
        for _, row in sorted(quick_action_seeds, key=lambda item: (item[0], item[1].cells[0])):
            profile_values = []
            for profile_name in ("保守", "激进"):
                detail = allocation_details.get((row.cells[0], profile_name))
                if detail is None:
                    continue
                allocation, arrangement = detail
                action = _action(allocation.action)
                shares = allocation.final_requested_shares
                if shares <= 0 or action in {"观察", "持有"}:
                    profile_values.append(f"{profile_name}：暂不下单；{arrangement}")
                else:
                    profile_values.append(
                        f"{profile_name}：{action} {_share_count(shares)} 股；{arrangement}"
                    )
            quick_action_rows.append(_row(
                f"quick:{row.row_id}",
                (
                    row.cells[0],
                    row.cells[1],
                    row.cells[2],
                    row.cells[3],
                    "；".join(profile_values) or row.cells[4],
                ),
                row.source_artifact_refs,
            ))
        quick_action_rows = tuple(quick_action_rows)
        quick_actions = ReportTable(
            "portfolio_quick_actions",
            "本轮最重要的操作",
            ("股票", "身份与价格", "当前动作", "下一步条件", "保守与激进"),
            quick_action_rows[:5],
            "当前没有需要处理的股票。",
            "最多显示 5 项，并按资金保护优先排序：卖出/减仓在前，买入/加仓其次，持有/观察最后。完整清单和理由见后文。",
        )
        editorial_actions = ReportTable(
            "portfolio_editorial_actions",
            "全部股票冻结动作",
            quick_actions.columns,
            quick_action_rows,
            quick_actions.empty_state,
            "仅用于生成详细操作解读，不改变首屏五项重点限制。",
        )
        editorial_table = _editorial_table(
            editorial_actions,
            {member.instrument.code: member for member in value.instruments},
            editorial,
        )
        facts = ReportTable(
            "portfolio_facts", "逐股价格与关键事实",
            ("股票", "身份", "K线日期", "用于分析的价格", "价格来源", "成本与盈亏", "组合仓位", "关键技术位置"),
            tuple(fact_rows), "暂无组合成分。",
            "这里明确列出本报告真正使用的价格和日期，便于直接与券商核对；盘后价格取最新完成日K，盘中才使用实时快照。",
        )
        qualities = ReportTable(
            "portfolio_quality", "数据来源与完整度",
            ("公司", "代码", "最新完成交易日", "历史行情", "本次价格来源", "新闻", "基本面", "需要注意"),
            tuple(quality_rows), "暂无组合成分。",
            "新闻或基本面本次未取得，不等于历史行情缺失；预测模型是否有效会在下一节单独说明。",
        )
        forecasts = ReportTable(
            "portfolio_forecasts", "各股票未来走势预测",
            ("股票", "分析价", "未来1日", "未来3日", "未来5日", "未来10日", "能否影响新开仓"),
            tuple(forecast_rows), "暂无逐股预测。",
            "行情数据完整不等于预测模型有效。每只股票只占一行；方向后的百分比是该方向概率，收益中位是模型预计的中间情形。历史验证不通过时只作观察，持仓止损不受阻断。",
        )
        strategies = ReportTable(
            "portfolio_strategies", "根据预测选择的策略与过去表现",
            ("股票", "采用的思路", "当前动作", "为什么选择", "该策略过去表现"),
            tuple(strategy_rows), "暂无策略选择。",
            "先根据预测确定市场情景，再比较同一股票上适用策略的历史收益和回撤。没有历史回放时不会声称策略有效。",
        )
        replacements = ReportTable("replacements", "关注股与替换机会", ("方案", "现有持仓", "候选股票", "状态", "下一步"), tuple(replacement_rows), "暂无替换候选。", "替换候选必须重新分析，不代表自动卖旧买新。")
        if value.watchlist_snapshot:
            watch_rows = tuple(_row(item.stable_key, (item.code, item.market.value, "关注"), _refs(value.watchlist_snapshot)) for item in value.watchlist_snapshot.instruments)
            watchlist = ReportTable("watchlist", "关注列表快照", ("股票", "市场", "状态"), watch_rows, "关注列表为空。", "关注列表与持仓互斥。")
            watch_refs = _refs(value.watchlist_snapshot, bundle)
        else:
            watchlist = ReportTable("watchlist", "关注列表快照", ("股票", "市场", "状态"), (), "未提供关注列表快照。", "关注列表与持仓互斥。")
            watch_refs = portfolio_refs
        portfolio_hypotheses={item.hypothesis_id:item for item in value.portfolio_research_hypotheses}
        for validation in value.portfolio_research_validations:
            hypothesis=portfolio_hypotheses.get(validation.hypothesis_id)
            research_rows.append(_row(validation.validation_id, (
                "组合", f"{hypothesis.title}：{hypothesis.thesis}" if hypothesis else "组合研究记录缺失",
                _display(_RESEARCH_STATUS_NAMES, validation.status),
                _display(_CANDIDATE_ELIGIBILITY_NAMES, validation.candidate_eligibility),
                _reason_text(validation.reason_codes),
            ), _refs(hypothesis, validation)))
        research_rows.extend(_row(item.snapshot_id, ("组合", f"研究指标：{item.scope_key}", "系统追踪", "研究账", _metric_summary(item.metrics)), _refs(item)) for item in value.portfolio_research_evidence)
        status = str(_value(value.research_status))
        status_text = _RESEARCH_RUN_STATUS_NAMES.get(status, status)
        if value.research_chunk_count:
            if status == "pending":
                status_text += f"；计划调用 {value.research_chunk_count} 个分片"
            else:
                status_text += f"；实际调用 {value.research_chunk_count} 个分片，成功 {value.research_completed_chunk_count} 个"
        if value.research_failure_reasons:
            failures = "、".join(_RESEARCH_FAILURE_REASON_NAMES.get(item, item) for item in value.research_failure_reasons)
            status_text += f"；未完成原因：{failures}"
        if status == "completed" and not (research_rows or value.portfolio_research_hypotheses):
            status_text += "；本次没有提出满足结构化和可验证要求的新观察"
        elif research_rows:
            status_text += f"；{len(research_rows)} 条观察/研究记录进入展示"
        research_rows.insert(0, _row(
            f"research-status:{status}",
            ("本次调用", status_text, "运行状态", "不直接下单", "LLM 只提出假设，代码系统负责事实与风险验证"),
            (PRESENTATION_POLICY_REF,),
        ))
        research = ReportTable(
            "portfolio_research", "研究员观察与系统验证",
            ("范围", "LLM观察", "系统验证", "候选处理", "理由/结果"),
            tuple(research_rows), "研究员尚未返回结果。",
            "研究员可以发现机会或质疑系统，但不能直接下单。调用状态、空结果和失败都会明确展示；只有通过代码事实验证的候选才进入后续历史样本外检验。",
        )
        forecast_history = ReportTable(
            "portfolio_forecast_history", "预测表现摘要",
            ("股票", "已验证", "方向正确", "概率误差", "80%区间命中", "待验证", "结论"),
            tuple(forecast_ability_rows), "暂无预测历史记录。",
            "这里只评价预测是否准确。方向正确率越高越好，概率误差越低越好；少于 30 条时不宣称模型稳定。",
        )
        strategy_history = ReportTable(
            "portfolio_strategy_history", "策略表现摘要",
            ("股票", "入场成交", "入场盈利率", "入场平均净收益", "同期买入持有", "入场平均超额", "最差不利波动", "退出评估", "平均退出质量", "结论"),
            tuple(strategy_ability_rows), "暂无策略历史回放。",
            "入场收益与退出质量分开评价。退出质量为正表示当时卖出/减仓优于继续持有，不是账户收益；牛市中收益接近基准但回撤显著更小也有价值。",
        )
        joint_history = ReportTable(
            "portfolio_joint_history", "预测 + 策略 + 风控完整链路",
            ("股票", "历史决策样本", "保守方案", "激进方案", "结论"),
            tuple(joint_ability_rows), "暂无完整链路历史回放。",
            "完整链路用于判断预测、策略、风控和成交组合后是否仍然有效；原始逐事件记录请在“历史评估”页面查看。",
        )
        forecast_history_artifacts = tuple(
            artifact
            for member in value.instruments
            for artifact in (
                *member.forecast_outcomes,
                *(item for item in member.metric_snapshots if str(_value(item.ledger_kind)) == "forecast"),
            )
        )
        strategy_history_artifacts = tuple(
            artifact
            for member in value.instruments
            for artifact in (
                *member.strategy_outcomes,
                *member.learning_evidence,
                *(item for item in member.metric_snapshots if str(_value(item.ledger_kind)) == "strategy"),
            )
        )
        joint_history_artifacts = tuple((
            *value.portfolio_learning_evidence,
            *(
                artifact
                for member in value.instruments
                for artifact in (
                    *member.joint_outcomes,
                    *(item for item in member.metric_snapshots if str(_value(item.ledger_kind)) == "joint"),
                )
            ),
        ))
        grouped_actions = {"卖出/减仓": [], "买入/加仓": [], "持有/观察": []}
        for row in quick_action_rows:
            action = row.cells[2].split("；", 1)[0]
            group = "卖出/减仓" if action in {"卖出", "减仓"} else "买入/加仓" if action in {"买入", "加仓"} else "持有/观察"
            grouped_actions[group].append(row.cells[0])
        focus_parts = []
        for name, codes in grouped_actions.items():
            if not codes:
                continue
            shown = "、".join(codes[:5])
            suffix = f"等 {len(codes)} 只" if len(codes) > 5 else ""
            focus_parts.append(f"{name}：{shown}{suffix}")
        final_summary = f"{summary}\n本轮操作：" + ("；".join(focus_parts) if focus_parts else "当前没有需要立即执行或预留的组合动作")
        sections = (
            _section(
                "action_summary", "操作总结", "先用一分钟确认本轮最需要处理什么",
                _callout(final_summary, _refs(value.account_snapshot, valuation, bundle)),
                _table(quick_actions, _table_refs(quick_actions)),
            ),
            _section(
                "facts", "基本信息与数据核对", "确认股票、价格、交易日、持仓和数据来源是否与券商一致",
                _table(facts, _table_refs(facts)),
                _table(qualities, _table_refs(qualities)),
                _table(holdings, account_refs),
                _table(watchlist, watch_refs),
                _callout(summary, account_refs),
            ),
            _section(
                "forecast", "各股票未来走势预测", "用颜色和概率说明目标日期、可能方向和预计收益",
                _table(forecasts, tuple(sorted({ref for row in forecasts.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,)),
            ),
            _section(
                "operation_report", "详细操作报告", "先读通俗解读，再按卖出、买入和观察顺序核对冻结操作",
                _table(editorial_table, _table_refs(editorial_table)),
                _table(exit_signals, _table_refs(exit_signals)),
                _table(entry_signals, _table_refs(entry_signals)),
                _table(hold_signals, _table_refs(hold_signals)),
                _table(profiles, portfolio_refs),
                _table(replacements, portfolio_refs),
                *(_table(item, _table_refs(item)) for item in holding_detail_tables),
                *(_table(item, _table_refs(item)) for item in watch_detail_tables),
            ),
            _section(
                "strategy_performance", "策略表现与回测依据", "说明为什么这样操作，并比较策略收益、基准收益和回撤控制",
                _table(strategies, tuple(sorted({ref for row in strategies.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,)),
                _table(strategy_history, _refs(*strategy_history_artifacts) if strategy_history_artifacts else (PRESENTATION_POLICY_REF,)),
                _table(joint_history, _refs(*joint_history_artifacts) if joint_history_artifacts else (PRESENTATION_POLICY_REF,)),
            ),
            _section(
                "research", "研究员补充观察", "LLM 研究员只补充机会和质疑，不改写正式操作",
                _table(research, tuple(sorted({ref for row in research.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,)),
            ),
            _section(
                "history", "系统历史可信度", "最后单独核对预测是否准确、样本是否足够",
                _table(forecast_history, _refs(*forecast_history_artifacts) if forecast_history_artifacts else (PRESENTATION_POLICY_REF,)),
            ),
        )
        title = f"{'A股' if _value(value.market) == 'A' else '美股'}组合交易工作台"
        identity = {"kind": ReportKind.PORTFOLIO, "market": value.market, "instrument": None, "mode": value.analysis_mode, "as_of": value.as_of, "title": title, "subtitle": value.history_period, "summary": summary, "sections": sections, "glossary": _GLOSSARY, "refs": value.source_artifact_refs, "schema": 1, "renderer": self.renderer_version}
        return ReportDocument(stable_hash(identity), ReportKind.PORTFOLIO, value.market, None, value.analysis_mode, value.as_of, title, value.history_period, summary, sections, _GLOSSARY, value.source_artifact_refs, 1, self.renderer_version, value.built_at)
