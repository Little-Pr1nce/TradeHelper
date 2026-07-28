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
    matured = tuple(sorted(
        (item for item in outcomes if item.net_return is not None),
        key=lambda item: (item.target_session_date, item.strategy_outcome_id),
    ))
    if not matured:
        return "尚未完成该股票的历史成交回放，不能声称已有正期望"
    returns = tuple(Decimal(str(item.net_return)) for item in matured)
    positive = sum(item > 0 for item in returns)
    average = sum(returns, Decimal("0")) / Decimal(len(returns))
    adverse = tuple(Decimal(str(item.mae)) for item in matured if item.mae is not None)
    text = (
        f"已验证 {len(matured)} 次，盈利 {positive} 次；"
        f"每次信号平均净收益 {format_percent(average)}；"
        f"胜率 {format_percent(positive / len(matured))}"
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
        and item.net_return is not None
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
        trades = [item for item in outcomes if item.net_return is not None]
        positive = sum(item.net_return > 0 for item in trades)
        details = f"盈利 {positive}/{len(trades)}" if trades else "尚无可复盘成交"
        if trades:
            details += f"；平均净收益 {format_percent(sum(item.net_return for item in trades) / len(trades))}"
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
        (item.strategy_id, item.strategy_version, item.parameter_hash, item.profile): item
        for item in value.learning_evidence
    }
    return {
        decision.decision_id: by_identity.get((
            plans[decision.plan_id].strategy_id,
            plans[decision.plan_id].strategy_version,
            plans[decision.plan_id].parameter_hash,
            decision.profile,
        ))
        for decision in value.risk_bundle.decisions
        if decision.plan_id in plans
    }


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


class SingleStockReportBuilder:
    renderer_version = "presentation_v2"

    def build(self, value: SingleStockPresentationInput) -> ReportDocument:
        decision = _primary_decision(value)
        action_refs = _refs(value.risk_bundle, value.order_intent_bundle)
        action_text = (
            f"当前动作：{_action(decision.action)}；执行等级：{_display(_LEVEL_NAMES, decision.level)}；"
            f"{'当前可执行' if decision.executable_now else '当前不可执行，等待冻结条件'}；"
            f"批准 {decision.approved_shares} 股；最大计划亏损 {_currency(decision.max_loss_amount, value.instrument.market)}。"
        )
        history_tables = _history_tables(value)
        sections = (
            _section("facts", "基本信息与数据核对", "先确认股票、日期、价格和来源是否正确", _table(_facts_table(value), _refs(value.metadata, value.quote_snapshot, value.data_quality, value.feature_snapshot, value.news_summary, value.fundamental_summary))),
            _section("forecast", "未来走势预测", "说明预测哪个目标日、可能方向和收益范围", _table(_forecast_table(value), _refs(*value.forecasts))),
            _section("scenario_evidence", "策略选择与过去表现", "策略必须符合当前预测，并单独展示历史收益", _table(_strategy_choice_table(value), _refs(value.scenario, value.strategy_bundle, value.risk_bundle, *value.strategy_outcomes))),
            _section("plans", "保守与激进操作计划", "说明触发、数量、退出、最大亏损和有效期", _table(_operation_table(value), _refs(value.strategy_bundle, value.risk_bundle, value.order_intent_bundle)), _table(_plan_table(value), _refs(value.strategy_bundle, value.risk_bundle, value.order_intent_bundle))),
            _section("risk", "账户风险明细", "金额、股数和仓位只来自真实账户", _table(_risk_table(value), _refs(value.risk_bundle, *value.learning_evidence))),
            _section("research", "研究员观察", "LLM 只补充观察，系统负责事实验证", _table(_research_table(value), _refs(*value.research_hypotheses, *value.research_validations, *value.research_outcomes, *value.research_metric_snapshots) if (value.research_hypotheses or value.research_validations or value.research_outcomes or value.research_metric_snapshots) else (PRESENTATION_POLICY_REF,))),
            _section("action_desk", "最终结论", "把预测、策略和风险合并成一句可执行结论", _callout(action_text, action_refs), _text(f"分析时点：{_datetime_text(value.as_of, value.instrument.market, seconds=True)}；计划有效期至：{_datetime_text(decision.expires_at, value.instrument.market) if decision.expires_at else '当前没有订单'}。判断错误时按上方退出条件处理。", action_refs)),
            _section("glossary", "阅读说明", "只保留理解报告所需的最低限度说明", _text("先核对价格和日期，再看预测、策略与操作计划。历史预测和策略结果必须分开判断。")),
            _section("history", "历史可信度", "最后分别核对预测是否准确、策略是否赚钱、完整链路是否有效", *(_table(item, tuple(sorted({ref for row in item.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,)) for item in history_tables)),
        )
        title = f"{value.metadata.name} ({value.instrument.code}) 单股研究报告"
        identity = {"kind": ReportKind.SINGLE_STOCK, "market": value.instrument.market, "instrument": value.instrument, "mode": value.analysis_mode, "as_of": value.as_of, "title": title, "subtitle": value.history_period, "summary": action_text, "sections": sections, "glossary": _GLOSSARY, "refs": value.source_artifact_refs, "schema": 1, "renderer": self.renderer_version}
        return ReportDocument(stable_hash(identity), ReportKind.SINGLE_STOCK, value.instrument.market, value.instrument, value.analysis_mode, value.as_of, title, value.history_period, action_text, sections, _GLOSSARY, value.source_artifact_refs, 1, self.renderer_version, value.built_at)


class PortfolioReportBuilder:
    renderer_version = "presentation_v2"

    def build(self, value: PortfolioPresentationInput) -> ReportDocument:
        valuation = value.frozen_account_valuation
        bundle = value.portfolio_decision_bundle
        account_refs = _refs(value.account_snapshot, valuation)
        portfolio_refs = _refs(bundle)
        summary = (
            f"冻结账户权益：{_currency(valuation.equity, value.market)}；现金：{_currency(valuation.cash, value.market)}；"
            f"持仓市值：{_currency(valuation.invested_value, value.market)}；总仓位：{format_percent(valuation.invested_pct)}；"
            f"估值时点：{_datetime_text(valuation.valuation_at, seconds=True)}。"
        )
        priority_rows = []
        profile_rows = []
        replacement_rows = []
        member_by_instrument = {item.instrument: item for item in value.instruments}
        valuation_map = {item.instrument: item for item in valuation.position_values}
        account_position_map = {item.instrument: item for item in value.account_snapshot.positions}
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
                priority_rows.append(_row(
                        f"{_value(profile.profile)}:{item.allocation_id}",
                    (
                        _profile(profile.profile), item.instrument.code, _price_text(member, value.market),
                        position_context, _action(item.action), item.final_requested_shares,
                        _allocation_explanation(item, member, value.market),
                        _allocation_status(item, member),
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
        history_rows = []
        for member in value.instruments:
            member_refs = member.source_artifact_refs
            plan_rows.extend(_portfolio_plan_rows(member))
            valued = valuation_map.get(member.instrument)
            position = account_position_map.get(member.instrument)
            held = valued is not None
            cost_and_pnl = (
                f"成本 {_currency(position.cost_price if position else None, value.market)}；浮盈亏 {_currency(valued.unrealized_pnl_amount, value.market)}（{format_percent(valued.unrealized_pnl_pct)}）"
                if held else "未持有"
            )
            mode_name = {"pre": "盘前参考价", "intraday": "盘中实时价", "eod": "盘后收盘价"}.get(str(_value(value.analysis_mode)), "分析价")
            fact_rows.append(_row(
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
            ))
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
            strategy_rows.append(_portfolio_strategy_row(member))
            for forecast in member.forecasts:
                forecast_rows.append(_row(forecast.event_key, (
                    member.instrument.code, _currency(forecast.reference_price, value.market), f"{forecast.horizon}日",
                    forecast.target_session_date or "—", _display(_DIRECTION_NAMES, forecast.direction) if forecast.direction else "无法预测",
                    format_percent(forecast.return_distribution.p50 if forecast.return_distribution else None),
                    _forecast_history_text(member, forecast),
                    "可以参与新开仓判断" if forecast.execution_eligible else "不参与新开仓执行分级；只作观察",
                ), _refs(forecast)))
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
            for metric in member.metric_snapshots:
                history_rows.append(_row(metric.snapshot_id, (member.instrument.code, _display(_LEDGER_NAMES, metric.ledger_kind), f"{metric.sample_count} 条", _metric_summary(metric.metrics)), _refs(metric)))
            for ledger, outcomes in (
                ("forecast", member.forecast_outcomes),
                ("strategy", member.strategy_outcomes),
                ("joint", member.joint_outcomes),
            ):
                row = _outcome_summary(member.instrument, ledger, outcomes)
                if row is not None:
                    history_rows.append(row)
            if member.learning_evidence:
                oof_samples = sum(item.oof_sample_count for item in member.learning_evidence)
                statuses = "、".join(sorted({_display(_EVIDENCE_GRADE_NAMES, item.status) for item in member.learning_evidence}))
                history_rows.append(_row(
                    f"{member.instrument.stable_key}:plan-evidence",
                    (member.instrument.code, "计划历史证据", f"{oof_samples} 个历史样本外记录", f"状态：{statuses}"),
                    _refs(*member.learning_evidence),
                ))
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
            ("股票", "预测时价格", "预测范围", "目标交易日", "最可能走势", "预计收益中间值", "过去表现是否可靠", "能否影响新开仓"),
            tuple(forecast_rows), "暂无逐股预测。",
            "行情数据完整不等于预测模型有效。模型未通过时会明确写成模型能力不足，而不是数据缺失；持仓止损不受预测模型阻断。",
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
        history_rows.extend(_row(item.snapshot_id, ("组合", _display(_LEDGER_NAMES, item.ledger_kind), f"{item.sample_count} 条", _metric_summary(item.metrics)), _refs(item)) for item in value.portfolio_learning_evidence)
        history = ReportTable("portfolio_history", "历史能力评估", ("股票/组合", "评估对象", "样本/记录", "通俗结果"), tuple(history_rows), "暂无成熟历史证据。", "同类原始事件已汇总；先看样本数，再同时比较收益、买入持有基准、收益保留率、最大回撤和风险调整表现。牛市中不要求策略机械跑赢基准；预测、策略和最终组合表现不能混为一个结论。")
        priority_summary = "；".join(
            f"{row.cells[1]}：{row.cells[4]} {row.cells[5]}股"
            for row in priority_rows[:5]
        ) or "当前没有需要立即执行或预留的组合动作"
        final_summary = f"{summary} 当前优先事项：{priority_summary}。"
        sections = (
            _section("facts", "基本信息与数据核对", "先确认账户、股票、价格、交易日和来源", _table(facts, tuple(sorted({ref for row in facts.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,)), _table(qualities, tuple(sorted({ref for row in qualities.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,)), _callout(summary, account_refs)),
            _section("forecast", "各股票未来走势预测", "说明目标日期、可能方向和预计收益", _table(forecasts, tuple(sorted({ref for row in forecasts.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
            _section("strategy", "策略选择与过去表现", "策略先服从预测情景，再比较收益和回撤", _table(strategies, tuple(sorted({ref for row in strategies.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
            _section("priority_actions", priority_title, "先保护退出，再持有管理，最后新增风险", _table(priorities, portfolio_refs)),
            _section("profiles", "保守与激进组合方案", "说明现金、预留和组合风险", _table(profiles, portfolio_refs)),
            _section("holdings", "持仓与盈亏", "核对数量、成本、现价和集中度", _table(holdings, account_refs)),
            _section("plans", "逐股操作计划", "说明买卖条件、持有条件、失效和两种方案", _table(plans, tuple(sorted({ref for row in plans.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
            _section("watchlist", "关注股与替换机会", "研究候选不代表自动交易", _table(watchlist, watch_refs), _table(replacements, portfolio_refs)),
            _section("research", "研究员观察", "研究独立展示，不改写正式操作", _table(research, tuple(sorted({ref for row in research.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
            _section("portfolio_overview", "最终结论", "汇总当前最需要处理的事情", _callout(final_summary, _refs(value.account_snapshot, valuation, bundle))),
            _section("history", "历史可信度", "最后分别核对预测、策略和完整链路表现", _table(history, tuple(sorted({ref for row in history.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
        )
        title = f"{'A股' if _value(value.market) == 'A' else '美股'}组合交易工作台"
        identity = {"kind": ReportKind.PORTFOLIO, "market": value.market, "instrument": None, "mode": value.analysis_mode, "as_of": value.as_of, "title": title, "subtitle": value.history_period, "summary": summary, "sections": sections, "glossary": _GLOSSARY, "refs": value.source_artifact_refs, "schema": 1, "renderer": self.renderer_version}
        return ReportDocument(stable_hash(identity), ReportKind.PORTFOLIO, value.market, None, value.analysis_mode, value.as_of, title, value.history_period, summary, sections, _GLOSSARY, value.source_artifact_refs, 1, self.renderer_version, value.built_at)
