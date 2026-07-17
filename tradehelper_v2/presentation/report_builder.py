"""Build deterministic, source-closed reports from frozen V2 contracts."""
from __future__ import annotations

from decimal import Decimal

from tradehelper_v2.contracts import (
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
from tradehelper_v2.contracts.presentation import PRESENTATION_POLICY_REF
from .formatting import format_money, format_percent, format_value
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


def _display(mapping, value):
    raw = str(_value(value))
    return mapping.get(raw, raw)


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


def _decision_priority(decision, protective_ids):
    action_priority = 0 if decision.action in {PlanAction.SELL, PlanAction.REDUCE} else 1
    level_priority = {"A": 0, "B": 1, "C": 2, "D": 3}.get(str(_value(decision.level)), 4)
    return (decision.decision_id not in protective_ids, not decision.executable_now, action_priority, level_priority, decision.decision_id)


def _primary_decision(value):
    decisions = tuple(value.risk_bundle.decisions)
    conservative = tuple(item for item in decisions if item.profile is RiskProfile.CONSERVATIVE)
    pool = conservative or decisions
    return min(pool, key=lambda item: _decision_priority(item, set(value.risk_bundle.protective_decision_ids)))


def _forecast_table(value):
    rows = []
    for forecast in value.forecasts:
        probs = forecast.probabilities
        distribution = forecast.return_distribution
        margin = "—" if forecast.confidence_margin is None else format_percent(forecast.confidence_margin)
        status = f"{_value(forecast.lifecycle)} / {_value(forecast.validation_status)}"
        if forecast.execution_eligible:
            impact = "参与情景与执行分级"
        else:
            impact = "预测可查看，不参与新开仓执行分级；样本成熟或下一确认窗口重新评估"
        rows.append(_row(
            forecast.event_key,
            (
                value.metadata.name + f" ({value.instrument.code})", forecast.cutoff_at.isoformat(),
                f"{forecast.horizon}日", forecast.target_session_date or "暂无可靠数据",
                _currency(forecast.reference_price, value.instrument.market),
                format_percent(probs.bullish if probs else None),
                format_percent(probs.neutral if probs else None),
                format_percent(probs.bearish if probs else None),
                format_percent(distribution.p10 if distribution else None),
                format_percent(distribution.p50 if distribution else None),
                format_percent(distribution.p90 if distribution else None), margin, status, impact,
            ),
            _refs(forecast),
        ))
    return ReportTable(
        "forecast_table", "独立市场预测",
        ("股票", "预测时间", "周期", "目标交易日", "参考价", "上涨", "震荡", "下跌", "P10", "P50", "P90", "分离度", "模型/OOF", "执行影响"),
        tuple(rows), "暂无可靠预测。",
        "P10/P50/P90 是预测收益分位；分离度只表示决断程度，不代表准确率。",
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
                branch_name, (branch_name, "不适用", _value(branch.readiness), "—", "—", "—", "—", "不可量化", "—"),
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
                        branch_name, _value(plan.action), _value(plan.readiness), _value(profile) if profile else "全部",
                        _condition_text(plan), f"{stop}", f"{target}", _risk_reward(plan),
                        "—" if decision is None else f"{_value(decision.level)} / {decision.approved_shares}股 / 最大亏损 {_currency(decision.max_loss_amount, value.instrument.market)}" + (" / 已生成订单意图" if intent else ""),
                    ), refs,
                ))
    return ReportTable(
        "plan_table", "当前与条件交易计划",
        ("分支", "动作", "状态", "方案", "触发/条件", "止损或失效", "止盈", "风险收益比", "风控/订单"),
        tuple(rows), None,
        "保守与激进方案可共享触发价；差异来自确认门槛、风险预算、批准股数或仓位。",
    )


def _risk_table(value):
    rows = []
    for decision in value.risk_bundle.decisions:
        rows.append(_row(
            decision.decision_id,
            (
                _value(decision.profile), _value(decision.action), _value(decision.level),
                _value(decision.disposition), "是" if decision.executable_now else "否",
                decision.approved_shares, format_percent(decision.post_trade_position_pct),
                _currency(decision.risk_budget_amount, value.instrument.market),
                _currency(decision.max_loss_amount, value.instrument.market),
                _reason_text(decision.reason_codes),
            ), _refs(value.risk_bundle),
        ))
    for evidence in value.learning_evidence:
        rows.append(_row(
            evidence.evidence_id,
            ("历史证据", evidence.strategy_id, _value(evidence.status), "否", "否", "—", "—", "—", "—", f"样本 {evidence.sample_count}，OOF {evidence.oof_sample_count}"),
            _refs(evidence),
        ))
    return ReportTable(
        "risk_table", "真实账户风控结论",
        ("方案", "动作", "等级", "处置", "当前可执行", "批准股数", "交易后仓位", "风险预算", "最大亏损", "原因"),
        tuple(rows), None, "金额、股数和仓位均来自冻结账户风控，不使用模拟本金。",
    )


def _facts_table(value):
    rows = [
        _row("metadata", ("公司", value.metadata.name, value.metadata.source, value.metadata.fetched_at.isoformat()), _refs(value.metadata)),
        _row("quality", ("数据质量", f"{_value(value.data_quality.status)} / {value.data_quality.score:.1f}", "质量引擎", value.data_quality.evaluated_at.isoformat()), _refs(value.data_quality)),
        _row("feature", ("技术特征", f"{len(value.feature_snapshot.values)} 项；版本 {value.feature_snapshot.feature_set_version}", "特征快照", value.feature_snapshot.cutoff_at.isoformat()), _refs(value.feature_snapshot)),
    ]
    if value.quote_snapshot:
        quote = value.quote_snapshot
        rows.append(_row("quote", ("当前价格", _currency(quote.price, value.instrument.market), quote.source, f"{quote.session.value} / {quote.observed_at.isoformat()}"), _refs(quote)))
    for index, news in enumerate(value.news_summary, 1):
        sentiment = news.finbert_label or "未评分"
        rows.append(_row(f"news:{index}", ("新闻", f"{news.title}；情感 {sentiment}", news.source, news.available_at.isoformat()), _refs(news)))
    if not value.news_summary:
        rows.append(_row("news:none", ("新闻", "暂无可靠数据", "—", "—"), (PRESENTATION_POLICY_REF,), ReportSeverity.UNAVAILABLE))
    if value.fundamental_summary:
        fields = "；".join(f"{name}={item.value} {item.unit or ''}" for name, item in value.fundamental_summary.fields.items()) or "字段为空"
        rows.append(_row("fundamental", ("基本面", fields, value.fundamental_summary.provider, value.fundamental_summary.available_at.isoformat()), _refs(value.fundamental_summary)))
    else:
        rows.append(_row("fundamental:none", ("基本面", "暂无可靠数据", "—", "—"), (PRESENTATION_POLICY_REF,), ReportSeverity.UNAVAILABLE))
    return ReportTable("facts_table", "当前事实", ("类别", "内容", "来源", "可见时点"), tuple(rows), None, "缺失项显示缺失，不以 0 代替。")


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
    for item in value.forecast_outcomes:
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
        "verified_forecasts", "已验证预测",
        ("股票", "预测时间", "参考价", "目标交易日", "主要预测", "预测收益区间", "实际日期", "实际价格/收益", "结果", "证据说明"),
        tuple(forecast_rows), "暂无已到期预测。", "预测账只评价预测本身是否准确，不会覆盖当前风险动作。",
    )
    strategy_rows = [
        _row(item.strategy_outcome_id, (item.strategy_id, item.action, item.target_session_date, item.trigger_state, item.fill_outcome, format_percent(item.net_return), format_percent(item.mae), format_percent(item.mfe), _value(item.status)), _refs(item))
        for item in value.strategy_outcomes
    ]
    strategies = ReportTable("strategy_outcomes", "策略结果", ("策略", "动作", "目标日", "触发", "成交", "净收益", "MAE", "MFE", "状态"), tuple(strategy_rows), "暂无成熟策略结果。", "策略账评价交易计划，不替代预测账。")
    joint_rows = [
        _row(item.joint_outcome_id, (_value(item.outcome_kind), item.profile or "组合", format_percent(item.time_weighted_return), format_percent(item.benchmark_return), format_percent(item.alpha), format_percent(item.max_drawdown), format_value(item.sharpe), _value(item.status)), _refs(item))
        for item in value.joint_outcomes
    ]
    joint = ReportTable("joint_outcomes", "联合结果", ("类型", "方案", "收益", "基准", "Alpha", "最大回撤", "Sharpe", "状态"), tuple(joint_rows), "暂无成熟联合结果。", "联合账评价预测、策略、风控和组合后的最终结果。")
    metric_rows = [
        _row(item.snapshot_id, (_value(item.ledger_kind), item.scope_key, item.sample_count, str(dict(item.metrics)), item.data_cutoff_at.isoformat()), _refs(item))
        for item in value.metric_snapshots
    ]
    metrics = ReportTable("learning_metrics", "冻结指标", ("账本", "范围", "样本数", "指标", "截止时点"), tuple(metric_rows), "暂无冻结指标。", "样本少于 30 条时不据此宣称稳定正期望。")
    return forecasts, strategies, joint, metrics


class SingleStockReportBuilder:
    renderer_version = "presentation_v2"

    def build(self, value: SingleStockPresentationInput) -> ReportDocument:
        decision = _primary_decision(value)
        action_refs = _refs(value.risk_bundle, value.order_intent_bundle)
        action_text = (
            f"当前动作：{_value(decision.action)}；执行等级：{_value(decision.level)}；"
            f"{'当前可执行' if decision.executable_now else '当前不可执行，等待冻结条件'}；"
            f"批准 {decision.approved_shares} 股；最大计划亏损 {_currency(decision.max_loss_amount, value.instrument.market)}。"
        )
        scenario_refs = _refs(value.scenario, value.strategy_bundle)
        scenario_table = ReportTable(
            "scenario_table", "预测到策略的证据链", ("环节", "状态", "来源身份"),
            (
                _row("scenario", ("情景", _value(value.scenario.state), value.scenario.scenario_id), _refs(value.scenario)),
                _row("strategy", ("策略", value.strategy_bundle.conflict_state, value.strategy_bundle.bundle_id), _refs(value.strategy_bundle)),
            ), None, "预测先形成情景，策略只在情景允许的家族中生成计划。",
        )
        history_tables = _history_tables(value)
        sections = (
            _section("action_desk", "一分钟操作台", "当前动作、有效期、风险和最重要条件", _callout(action_text, action_refs), _text(f"数据时点：{value.as_of.isoformat()}；会话：{_value(value.scenario.decision_session.session_date) if value.scenario.decision_session else '日历不可用'}；有效期至：{decision.expires_at or '无当前订单'}", action_refs)),
            _section("forecast", "独立市场预测", "预测时间、目标日、概率与预测收益区间", _table(_forecast_table(value), _refs(*value.forecasts))),
            _section("plans", "当前与条件交易计划", "买/加、卖/减、持有和失效四个条件分支", _table(_plan_table(value), _refs(value.strategy_bundle, value.risk_bundle, value.order_intent_bundle))),
            _section("risk", "风险金额与风控结论", "真实账户金额、仓位、股数和历史证据", _table(_risk_table(value), _refs(value.risk_bundle, *value.learning_evidence))),
            _section("facts", "当前事实", "价格、来源、时点、质量、技术、新闻和基本面", _table(_facts_table(value), _refs(value.metadata, value.quote_snapshot, value.data_quality, value.feature_snapshot, value.news_summary, value.fundamental_summary))),
            _section("scenario_evidence", "情景与策略证据", "预测如何映射为情景和策略家族", _table(scenario_table, scenario_refs)),
            _section("research", "研究员观察与系统验证", "LLM 观察独立于正式执行", _table(_research_table(value), _refs(*value.research_hypotheses, *value.research_validations, *value.research_outcomes, *value.research_metric_snapshots) if (value.research_hypotheses or value.research_validations or value.research_outcomes or value.research_metric_snapshots) else (PRESENTATION_POLICY_REF,))),
            _section("history", "历史验证与系统追踪", "预测、策略和联合账分开呈现", *(_table(item, tuple(sorted({ref for row in item.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,)) for item in history_tables)),
            _section("glossary", "术语和阅读方法", "样本门槛、指标含义与阅读方向", _text("先看目标日期和当前动作，再看最大亏损；历史样本少于 10 条只积累，10-29 条只观察，30 条以上才可比较。")),
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
            f"估值时点：{valuation.valuation_at.isoformat()}。"
        )
        priority_rows = []
        profile_rows = []
        replacement_rows = []
        for profile in (bundle.conservative, bundle.aggressive):
            by_id = {item.allocation_id: item for item in profile.allocations}
            ordered_ids = profile.holding_priority_allocation_ids + profile.entry_priority_allocation_ids
            for allocation_id in ordered_ids:
                item = by_id[allocation_id]
                priority_rows.append(_row(
                    f"{_value(profile.profile)}:{item.allocation_id}",
                    (_value(profile.profile), item.instrument.code, _value(item.action), item.level, item.final_requested_shares, _currency(item.reserved_cash, value.market), _value(item.status), _reason_text(item.reason_codes)),
                    portfolio_refs,
                ))
            reserve = profile.reservation_snapshot
            profile_rows.append(_row(
                _value(profile.profile),
                (_value(profile.profile), _currency(reserve.remaining_cash, value.market), format_percent(reserve.projected_invested_pct_at_reference_price), format_percent(reserve.projected_heat_pct), _value(reserve.evidence_grade)),
                portfolio_refs,
            ))
            for item in profile.replacement_candidates:
                replacement_rows.append(_row(
                    item.replacement_id,
                    (_value(profile.profile), item.source_instrument.code, item.target_instrument.code, _value(item.status), "需重新分析" if item.reanalysis_required else "—"),
                    portfolio_refs,
                ))
        priorities = ReportTable("priority_actions", "今日优先处理", ("方案", "股票", "动作", "等级", "股数", "占用/释放金额", "状态", "原因"), tuple(priority_rows), "当前没有需要执行的组合动作。", "保护退出排在持有管理和新增风险之前。")
        profiles = ReportTable("portfolio_profiles", "保守与激进组合方案", ("方案", "剩余现金", "预计仓位", "组合热度", "证据等级"), tuple(profile_rows), "暂无可靠组合分配。", "两种方案使用同一冻结账户估值，分别分配风险预算。")
        valuation_map = {item.instrument: item for item in valuation.position_values}
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
        quality_rows = []
        forecast_rows = []
        research_rows = []
        history_rows = []
        for member in value.instruments:
            member_refs = member.source_artifact_refs
            decision = _primary_decision(member)
            plan_rows.append(_row(member.instrument.stable_key, (member.instrument.code, _value(decision.action), _value(decision.level), "当前可执行" if decision.executable_now else "条件等待", _reason_text(decision.reason_codes)), _refs(member.scenario, member.strategy_bundle, member.risk_bundle, member.order_intent_bundle)))
            quality_rows.append(_row(member.instrument.stable_key, (member.metadata.name, member.instrument.code, _value(member.data_quality.status), member.data_quality.score, member.quote_snapshot.source if member.quote_snapshot else "无实时快照", f"新闻 {len(member.news_summary)} 条", "有" if member.fundamental_summary else "缺失"), _refs(member.metadata, member.quote_snapshot, member.data_quality, member.feature_snapshot, member.news_summary, member.fundamental_summary)))
            for forecast in member.forecasts:
                forecast_rows.append(_row(forecast.event_key, (member.instrument.code, f"{forecast.horizon}日", forecast.target_session_date or "—", _value(forecast.direction) if forecast.direction else "不可用", format_percent(forecast.return_distribution.p50 if forecast.return_distribution else None), _value(forecast.validation_status), "是" if forecast.execution_eligible else "否"), _refs(forecast)))
            for validation in member.research_validations:
                research_rows.append(_row(validation.validation_id, (
                    member.instrument.code,
                    _display(_RESEARCH_STATUS_NAMES, validation.status),
                    _display(_CANDIDATE_ELIGIBILITY_NAMES, validation.candidate_eligibility),
                    _reason_text(validation.reason_codes),
                ), _refs(validation)))
            for metric in member.metric_snapshots:
                history_rows.append(_row(metric.snapshot_id, (member.instrument.code, _value(metric.ledger_kind), metric.sample_count, str(dict(metric.metrics))), _refs(metric)))
            # Consume otherwise section-less frozen research/learning evidence without
            # conflating their metrics into another ledger.
            for artifact in (*member.forecast_outcomes, *member.strategy_outcomes, *member.joint_outcomes, *member.research_hypotheses, *member.research_outcomes, *member.research_metric_snapshots, *member.learning_evidence):
                history_rows.append(_row(stable_hash(artifact), (member.instrument.code, type(artifact).__name__, getattr(artifact, "sample_count", "—"), "独立冻结证据"), _refs(artifact)))
        plans = ReportTable("portfolio_plans", "逐股条件与当前动作", ("股票", "动作", "等级", "状态", "原因"), tuple(plan_rows), "暂无逐股计划。", "详细买/加、卖/减、持有和失效条件保留在同一冻结单股计划中。")
        qualities = ReportTable("portfolio_quality", "逐股数据质量", ("公司", "代码", "质量", "分数", "行情来源", "新闻", "基本面"), tuple(quality_rows), "暂无组合成分。", "单股数据异常只降级该股票。")
        forecasts = ReportTable("portfolio_forecasts", "组合成分股独立预测", ("股票", "周期", "目标日", "方向", "P50", "OOF", "参与执行"), tuple(forecast_rows), "暂无逐股预测。", "每只股票独立显示 1/3/5/10 日预测。")
        replacements = ReportTable("replacements", "关注股与替换机会", ("方案", "现有持仓", "候选股票", "状态", "下一步"), tuple(replacement_rows), "暂无替换候选。", "替换候选必须重新分析，不代表自动卖旧买新。")
        if value.watchlist_snapshot:
            watch_rows = tuple(_row(item.stable_key, (item.code, item.market.value, "关注"), _refs(value.watchlist_snapshot)) for item in value.watchlist_snapshot.instruments)
            watchlist = ReportTable("watchlist", "关注列表快照", ("股票", "市场", "状态"), watch_rows, "关注列表为空。", "关注列表与持仓互斥。")
            watch_refs = _refs(value.watchlist_snapshot, bundle)
        else:
            watchlist = ReportTable("watchlist", "关注列表快照", ("股票", "市场", "状态"), (), "未提供关注列表快照。", "关注列表与持仓互斥。")
            watch_refs = portfolio_refs
        research_rows.extend(_row(item.snapshot_id, ("组合", "研究指标", item.scope_key, str(dict(item.metrics))), _refs(item)) for item in value.portfolio_research_evidence)
        research = ReportTable("portfolio_research", "研究员观察与系统验证", ("股票", "状态", "资格/范围", "原因/指标"), tuple(research_rows), "暂无可靠研究汇总。", "研究结果不改写组合执行。")
        history_rows.extend(_row(item.snapshot_id, ("组合", _value(item.ledger_kind), item.sample_count, str(dict(item.metrics))), _refs(item)) for item in value.portfolio_learning_evidence)
        history = ReportTable("portfolio_history", "历史能力评估", ("股票/组合", "账本", "样本数", "冻结指标"), tuple(history_rows), "暂无成熟历史证据。", "预测、策略、联合和研究证据保持独立。")
        sections = (
            _section("portfolio_overview", "组合概览", "冻结账户权益、现金、仓位、计划风险和币种", _callout(summary, account_refs)),
            _section("priority_actions", "今日优先处理", "先保护退出，再持有管理，最后新增风险", _table(priorities, portfolio_refs)),
            _section("profiles", "保守与激进组合方案", "最终分配、现金、heat 和集中度", _table(profiles, portfolio_refs)),
            _section("holdings", "持仓风险表", "数量、成本、现价、浮盈亏和集中度", _table(holdings, account_refs)),
            _section("plans", "条件触发计划", "每股当前动作及冻结条件计划", _table(plans, tuple(sorted({ref for row in plans.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
            _section("watchlist", "关注股与替换机会", "研究候选，不代表自动交易", _table(watchlist, watch_refs), _table(replacements, portfolio_refs)),
            _section("quality", "逐股数据质量", "每股来源、新鲜度、新闻和基本面状态", _table(qualities, tuple(sorted({ref for row in qualities.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
            _section("forecast", "组合成分股独立预测", "每股 1/3/5/10 日预测和模型状态", _table(forecasts, tuple(sorted({ref for row in forecasts.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
            _section("research", "研究员观察与系统验证", "研究独立展示，不改写组合执行", _table(research, tuple(sorted({ref for row in research.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
            _section("history", "历史能力评估", "预测账、策略账、联合账和研究账独立展示", _table(history, tuple(sorted({ref for row in history.rows for ref in row.source_artifact_refs})) or (PRESENTATION_POLICY_REF,))),
            _section("glossary", "术语和阅读方法", "指标定义、样本门槛和阅读方法", _text("先看保护退出与账户总风险，再看新增风险；样本不足时只积累，不宣称正期望。")),
        )
        title = f"{value.market.value} 组合交易工作台"
        identity = {"kind": ReportKind.PORTFOLIO, "market": value.market, "instrument": None, "mode": value.analysis_mode, "as_of": value.as_of, "title": title, "subtitle": value.history_period, "summary": summary, "sections": sections, "glossary": _GLOSSARY, "refs": value.source_artifact_refs, "schema": 1, "renderer": self.renderer_version}
        return ReportDocument(stable_hash(identity), ReportKind.PORTFOLIO, value.market, None, value.analysis_mode, value.as_of, title, value.history_period, summary, sections, _GLOSSARY, value.source_artifact_refs, 1, self.renderer_version, value.built_at)
