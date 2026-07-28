"""Read-only historical evaluation projections over frozen V2 outcomes."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from contracts import (
    ChartKind,
    ChartSpec,
    ContractViolation,
    ForecastOutcome,
    HistoricalEvaluationQuery,
    HistoricalEvaluationView,
    HypothesisOutcome,
    JointOutcome,
    LedgerViewKind,
    LearningMetricSnapshot,
    Market,
    MetricDefinition,
    OutcomeStatus,
    ReportTable,
    ReportTableRow,
    ResearchMetricSnapshot,
    StrategyOutcome,
    presentation_source_refs,
    stable_hash,
)
from contracts.presentation import PRESENTATION_POLICY_REF
from presentation.charts import empty_chart
from presentation.formatting import format_percent


_GLOSSARY = (
    MetricDefinition("brier", "Brier", "概率预测和真实结果的平方误差。", "越低越好", "至少 30 条成熟样本", "score"),
    MetricDefinition("log_loss", "Log Loss", "对非常自信但错误的预测惩罚更重。", "越低越好", "至少 30 条成熟样本", "score"),
    MetricDefinition("ece", "ECE", "模型说 70% 时，实际发生频率是否接近 70%。", "越低越好", "至少 30 条成熟样本", "score"),
    MetricDefinition("interval_hit", "80% 区间命中", "实际落入 P10-P90 的比例。", "样本充分后接近 80%", "至少 30 条成熟样本", "%"),
)


def maturity_message(count):
    if count == 0:
        return "暂无已到期记录。"
    if count < 10:
        return "样本积累中，不评价可靠性。"
    if count < 30:
        return "可作观察，不允许模型优劣定论。"
    return "样本达到比较门槛，仍需结合分层、区间和回撤。"


def _value(value):
    return getattr(value, "value", value)


def _status(item):
    return _value(getattr(item, "status", None))


def _ledger(item):
    if isinstance(item, ForecastOutcome):
        return LedgerViewKind.FORECAST
    if isinstance(item, StrategyOutcome):
        return LedgerViewKind.STRATEGY
    if isinstance(item, JointOutcome):
        return LedgerViewKind.JOINT
    if isinstance(item, HypothesisOutcome):
        return LedgerViewKind.RESEARCH
    raw = getattr(item, "ledger_kind", getattr(item, "ledger", None))
    return None if raw is None else LedgerViewKind(_value(raw))


def _ref(item):
    try:
        return presentation_source_refs(item)[0]
    except ContractViolation:
        return f"external:historical_{stable_hash(item)}"


def _item_date(item):
    value = getattr(item, "evaluated_at", None) or getattr(item, "generated_at", None)
    if value is None:
        raw = getattr(item, "origin_session_date", None)
        return datetime.combine(raw, datetime.min.time(), tzinfo=timezone.utc) if isinstance(raw, date) else None
    return value


def _matches(query, item):
    ledger = _ledger(item)
    if query.ledger_kind is not None and ledger is not query.ledger_kind:
        return False
    instrument = getattr(item, "instrument", None)
    market = instrument.market if instrument is not None else getattr(item, "market", None)
    if market is not query.market:
        return False
    if query.instrument is not None and instrument != query.instrument:
        return False
    horizon = getattr(item, "horizon", getattr(item, "evaluation_horizon", None))
    if query.horizon is not None and horizon != query.horizon:
        return False
    if query.model_version is not None and getattr(item, "model_version", None) != query.model_version:
        return False
    if query.strategy_id is not None and getattr(item, "strategy_id", None) != query.strategy_id:
        return False
    if query.market_regime_key is not None and getattr(item, "market_regime_key", None) != query.market_regime_key:
        return False
    if query.evidence_origin is not None and _value(getattr(item, "evidence_origin", None)) != query.evidence_origin:
        return False
    observed = _item_date(item)
    if query.date_from is not None and (observed is None or observed < query.date_from):
        return False
    if query.date_to is not None and (observed is None or observed > query.date_to):
        return False
    if not query.include_unverifiable and _status(item) in {"unverifiable", "not_applicable", "superseded"}:
        return False
    return True


def _metric_matches(query, item):
    if isinstance(item, ResearchMetricSnapshot):
        ledger = LedgerViewKind.RESEARCH
        market = item.market
        cutoff = item.cutoff_at
        dimensions = dict(item.dimensions)
    elif isinstance(item, LearningMetricSnapshot):
        try:
            ledger = LedgerViewKind(_value(item.ledger_kind))
        except ValueError:
            return False
        market = next(
            (
                candidate for candidate in Market
                if item.scope_key == candidate.value or item.scope_key.startswith(candidate.value + ":")
            ),
            None,
        )
        cutoff = item.data_cutoff_at
        dimensions = {}
    else:
        return False
    if query.ledger_kind is not None and ledger is not query.ledger_kind:
        return False
    if market is not query.market:
        return False
    if query.instrument is not None and getattr(item, "scope_key", None) != query.instrument.stable_key:
        return False
    if query.horizon is not None and str(dimensions.get("horizon")) != str(query.horizon):
        return False
    if query.model_version is not None and dimensions.get("model_version") != query.model_version:
        return False
    if query.strategy_id is not None and dimensions.get("strategy_id") != query.strategy_id:
        return False
    if query.market_regime_key is not None and dimensions.get("market_regime_key") != query.market_regime_key:
        return False
    if query.evidence_origin is not None and dimensions.get("evidence_origin") != query.evidence_origin:
        return False
    return not ((query.date_from and cutoff < query.date_from) or (query.date_to and cutoff > query.date_to))


def _row(row_id, cells, item):
    return ReportTableRow(str(row_id), tuple(str(cell) for cell in cells), None, (_ref(item),))


def _calibration(forecasts, warning):
    bins = defaultdict(list)
    for item in forecasts:
        if item.probabilities is None or item.predicted_direction is None or item.direction_correct is None:
            continue
        confidence = item.probabilities.for_direction(item.predicted_direction)
        lower = min(int(confidence * 10), 9) / 10
        bins[lower].append(1.0 if item.direction_correct else 0.0)
    if not bins:
        return empty_chart(ChartKind.CALIBRATION, "概率校准", interpretation="横轴为预测置信度，纵轴为实际发生频率；对角线是理想校准基线。", empty_state=warning)
    points = tuple((f"{lower:.1f}-{lower + .1:.1f}", sum(values) / len(values)) for lower, values in sorted(bins.items()))
    baseline = tuple((label, lower + .05) for label, lower in ((f"{x:.1f}-{x + .1:.1f}", x) for x in sorted(bins)))
    labels = "；".join(f"{lower:.1f}-{lower + .1:.1f} n={len(values)}" for lower, values in sorted(bins.items()))
    series = (("实际发生率", points),)
    identity = {"kind": ChartKind.CALIBRATION, "series": series, "baseline": baseline, "samples": sum(map(len, bins.values()))}
    return ChartSpec(stable_hash(identity), ChartKind.CALIBRATION, "概率校准", "预测置信度分箱", "实际发生频率", series, baseline, sum(map(len, bins.values())), (points[0][0], points[-1][0]), f"对角线代表理想校准；分箱样本：{labels}")


def _timeline(forecasts, warning):
    usable = tuple(item for item in forecasts if item.actual_return is not None and item.predicted_p50 is not None)
    if not usable:
        return empty_chart(ChartKind.FORECAST_TIMELINE, "预测结果时间线", interpretation="P50 是预测中位数，P10-P90 是预测区间，实际收益用于验证。", empty_state=warning)
    ordered = tuple(sorted(usable, key=lambda item: (item.target_session_date, item.forecast_outcome_id)))
    points = lambda name: tuple((f"{item.target_session_date}:{item.forecast_outcome_id[:8]}", float(getattr(item, name))) for item in ordered)
    series = (("P10", points("predicted_p10")), ("P50", points("predicted_p50")), ("P90", points("predicted_p90")), ("实际收益", points("actual_return")))
    baseline = tuple((key, 0.0) for key, _ in series[0][1])
    identity = {"kind": ChartKind.FORECAST_TIMELINE, "series": series, "baseline": baseline}
    return ChartSpec(stable_hash(identity), ChartKind.FORECAST_TIMELINE, "预测结果时间线", "目标交易日 / outcome", "收益率", series, baseline, len(ordered), (series[0][1][0][0], series[0][1][-1][0]), "P50 为主预测，P10-P90 为区间边界，实际收益是对照点；标签含 outcome ID。")


def _performance(outcomes, warning):
    values = []
    for item in outcomes:
        result = item.net_return if isinstance(item, StrategyOutcome) else item.time_weighted_return if isinstance(item, JointOutcome) else None
        benchmark = getattr(item, "benchmark_return", None)
        if result is not None:
            values.append((getattr(item, "target_session_date", None) or getattr(item, "generated_at", None).date(), float(result), None if benchmark is None else float(benchmark), _ref(item)))
    if not values:
        return (
            empty_chart(ChartKind.CUMULATIVE_PERFORMANCE, "策略/联合 OOF 累计表现", interpretation="只显示有效 OOF 成交或联合结果。", empty_state=warning),
            empty_chart(ChartKind.DRAWDOWN, "策略/联合 OOF 回撤", interpretation="回撤衡量从历史净值高点的下降。", empty_state=warning),
        )
    values.sort(key=lambda item: (item[0], item[3]))
    equity = benchmark_equity = peak = 1.0
    cumulative = []
    benchmark_points = []
    drawdown = []
    for when, result, benchmark, ref in values:
        equity *= 1 + result
        benchmark_equity *= 1 + (benchmark or 0.0)
        peak = max(peak, equity)
        key = f"{when}:{ref.split(':')[-1][:8]}"
        cumulative.append((key, equity - 1))
        benchmark_points.append((key, benchmark_equity - 1))
        drawdown.append((key, equity / peak - 1))
    cumulative_series = (("系统", tuple(cumulative)), ("基准", tuple(benchmark_points)))
    baseline = tuple((key, 0.0) for key, _ in cumulative)
    cum = ChartSpec(stable_hash(("cumulative", cumulative_series)), ChartKind.CUMULATIVE_PERFORMANCE, "策略/联合 OOF 累计表现", "到期/成交日期", "累计收益", cumulative_series, baseline, len(values), (cumulative[0][0], cumulative[-1][0]), "仅使用查询范围内成熟且可验证的 OOF 结果。")
    dd_series = (("回撤", tuple(drawdown)),)
    dd = ChartSpec(stable_hash(("drawdown", dd_series)), ChartKind.DRAWDOWN, "策略/联合 OOF 回撤", "到期/成交日期", "回撤", dd_series, baseline, len(values), (drawdown[0][0], drawdown[-1][0]), "数值越接近 0 越好；负值表示相对历史净值高点的下降。")
    return cum, dd


class HistoricalEvaluationService:
    def build(self, query: HistoricalEvaluationQuery, *, outcomes=(), metrics=(), built_at: datetime):
        all_items = tuple(outcomes)
        allowed_outcomes = (ForecastOutcome, StrategyOutcome, JointOutcome, HypothesisOutcome)
        if any(not isinstance(item, allowed_outcomes) for item in all_items):
            raise ContractViolation("historical evaluation accepts only frozen outcome contracts")
        if any(not isinstance(item, (LearningMetricSnapshot, ResearchMetricSnapshot)) for item in metrics):
            raise ContractViolation("historical evaluation accepts only frozen metric snapshots")
        selected = tuple(item for item in all_items if _matches(query, item))
        selected_metrics = tuple(item for item in metrics if _metric_matches(query, item))
        matured = tuple(item for item in selected if _status(item) == OutcomeStatus.MATURED.value)
        for item in matured:
            origin = getattr(item, "origin_session_date", None)
            target = getattr(item, "target_session_date", None)
            if origin is not None and target is not None and target <= origin:
                raise ContractViolation("historical outcome target date precedes prediction")
        warning = maturity_message(len(matured))
        if len(matured) == 1:
            warning += " 当前仅有 1 条已验证样本，不能据此判断模型稳定性。"
        refs = tuple(sorted({PRESENTATION_POLICY_REF, *(_ref(item) for item in (*selected, *selected_metrics))}))
        ledger_rows = []
        for ledger in LedgerViewKind:
            values = tuple(item for item in selected if _ledger(item) is ledger)
            ledger_rows.append(ReportTableRow(ledger.value, (ledger.value, str(sum(_status(item) == "matured" for item in values)), str(len(values)), "独立账本，不与其他命中率合并"), None, tuple(sorted({_ref(item) for item in values})) or (PRESENTATION_POLICY_REF,)))
        ledgers = ReportTable("ledger_summary", "账本独立性", ("账本", "成熟样本", "查询样本", "说明"), tuple(ledger_rows), None, "预测账、策略账、联合账和 LLM 研究账分别展示。")
        forecast_items = tuple(item for item in matured if isinstance(item, ForecastOutcome))
        strategy_items = tuple(item for item in matured if isinstance(item, StrategyOutcome))
        joint_items = tuple(item for item in matured if isinstance(item, JointOutcome))
        research_items = tuple(item for item in matured if isinstance(item, HypothesisOutcome))
        event_rows = []
        for item in matured:
            if isinstance(item, ForecastOutcome):
                result = "正确" if item.direction_correct else "错误"
                content = (item.instrument.code, _ledger(item).value, item.origin_session_date, item.target_session_date, _value(item.predicted_direction), _value(item.actual_direction), format_percent(item.actual_return), result, _value(item.evidence_grade))
            elif isinstance(item, StrategyOutcome):
                content = (item.instrument.code, _ledger(item).value, item.generated_at.date(), item.target_session_date, item.action, item.fill_outcome, format_percent(item.net_return), _value(item.status), _value(item.execution_evidence_grade))
            elif isinstance(item, JointOutcome):
                content = ("组合", _ledger(item).value, item.generated_at.date(), item.replay_window[1] if item.replay_window else item.generated_at.date(), _value(item.outcome_kind), "—", format_percent(item.time_weighted_return), _value(item.status), _value(item.evidence_grade))
            else:
                content = (item.instrument.code, _ledger(item).value, item.origin_session_date, item.target_session_date or "—", item.expected_direction or "—", item.actual_direction or "—", format_percent(item.actual_return), _value(item.status), item.evidence_grade)
            event_rows.append(_row(_ref(item), content, item))
        events = ReportTable("outcome_events", "逐事件审计", ("股票", "账本", "发行/起始日", "目标/结束日", "预测/动作", "实际/成交", "收益", "结果", "证据"), tuple(event_rows), warning, "每行均来自一个冻结 outcome，并保留独立来源引用。")
        regime_groups = defaultdict(list)
        for item in matured:
            regime_groups[getattr(item, "market_regime_key", None) or "未分层"].append(item)
        regime_rows = []
        for regime, values in sorted(regime_groups.items()):
            forecast_values = [item for item in values if isinstance(item, ForecastOutcome) and item.direction_correct is not None]
            returns = [float(item.net_return) for item in values if isinstance(item, StrategyOutcome) and item.net_return is not None]
            regime_rows.append(ReportTableRow(f"regime:{regime}", (regime, str(len(values)), format_percent(sum(item.direction_correct for item in forecast_values) / len(forecast_values) if forecast_values else None), format_percent(sum(returns) / len(returns) if returns else None)), None, tuple(sorted({_ref(item) for item in values}))))
        regimes = ReportTable("regime_slices", "市场状态分层", ("市场状态", "样本数", "预测正确率", "策略平均收益"), tuple(regime_rows), "暂无市场状态分层。", "同一市场状态下聚合显示，不把每个事件伪装成一个分层。")
        calibration_rows = []
        calibration_chart = _calibration(forecast_items, warning)
        if calibration_chart.series:
            actual = dict(calibration_chart.series[0][1])
            ideal = dict(calibration_chart.baseline)
            for label in actual:
                calibration_rows.append(ReportTableRow(label, (label, str(sum(1 for item in forecast_items if item.probabilities and item.predicted_direction and min(int(item.probabilities.for_direction(item.predicted_direction) * 10), 9) / 10 == float(label[:3]))), format_percent(ideal[label]), format_percent(actual[label])), None, tuple(sorted({_ref(item) for item in forecast_items}))))
        calibration_table = ReportTable("calibration_bins", "概率校准分箱", ("置信度分箱", "样本数", "理想发生率", "实际发生率"), tuple(calibration_rows), warning, "实际发生率高于理想线表示模型偏保守，低于理想线表示模型偏自信。")
        metric_rows = []
        headline = []
        for item in selected_metrics:
            for key, number in item.metrics:
                metric_rows.append(_row(f"{item.snapshot_id}:{key}", (_ledger(item).value if _ledger(item) else "research", item.scope_key, key, "—" if number is None else number, getattr(item, "sample_count", "—")), item))
                headline.append((key, number))
        metric_table = ReportTable("metric_snapshots", "冻结指标快照", ("账本", "范围", "指标", "数值", "样本数"), tuple(metric_rows), "暂无匹配指标快照。", "指标已经按当前查询维度过滤。")
        timeline = _timeline(forecast_items, warning)
        cumulative, drawdown = _performance((*strategy_items, *joint_items), warning)
        tables = (ledgers, events, calibration_table, regimes, metric_table)
        warnings = (warning,) if len(matured) < 30 else ()
        return HistoricalEvaluationView(
            query,
            (("matured_count", len(matured)), ("pending_count", sum(_status(item) == "pending" for item in selected)), ("message", warning)),
            tuple(sorted(headline)),
            (calibration_chart, timeline, cumulative, drawdown),
            tables, _GLOSSARY, warnings, refs, built_at,
        )


class RepositoryHistoricalEvaluationService:
    def __init__(self, repository, *, clock=lambda: datetime.now(timezone.utc)):
        self.repository=repository
        self.clock=clock
        self.builder=HistoricalEvaluationService()

    def load(self, query: HistoricalEvaluationQuery):
        outcomes,metrics=self.repository.list_historical_evaluation_records(query.market)
        return self.builder.build(query,outcomes=outcomes,metrics=metrics,built_at=self.clock())
