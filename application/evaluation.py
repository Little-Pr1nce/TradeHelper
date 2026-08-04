"""Read-only historical evaluation projections over frozen V2 outcomes."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from contracts import (
    ChartKind,
    ChartSpec,
    ContractViolation,
    DecisionMode,
    ForecastOutcome,
    ForecastResult,
    HistoricalEvaluationQuery,
    HistoricalEvaluationView,
    HypothesisOutcome,
    JointOutcome,
    LedgerViewKind,
    LearningMetricSnapshot,
    Market,
    MetricDefinition,
    OutcomeStatus,
    ReportHistoryQuery,
    ReportKind,
    ReportTable,
    ReportTableRow,
    ResearchMetricSnapshot,
    StrategyOutcome,
    presentation_source_refs,
    stable_hash,
)
from contracts.presentation import PRESENTATION_POLICY_REF
from presentation.charts import empty_chart
from presentation.formatting import format_datetime, format_percent


_GLOSSARY = (
    MetricDefinition("brier", "Brier", "概率预测和真实结果的平方误差。", "越低越好", "至少 30 条成熟样本", "score"),
    MetricDefinition("log_loss", "Log Loss", "对非常自信但错误的预测惩罚更重。", "越低越好", "至少 30 条成熟样本", "score"),
    MetricDefinition("ece", "ECE", "模型说 70% 时，实际发生频率是否接近 70%。", "越低越好", "至少 30 条成熟样本", "score"),
    MetricDefinition("interval_hit", "80% 区间命中", "实际落入 P10-P90 的比例。", "样本充分后接近 80%", "至少 30 条成熟样本", "%"),
)
MAX_EVALUATION_DETAIL_ROWS = 200
DEFAULT_DETAIL_LOOKBACK_DAYS = 30
_ENTRY_ACTIONS = frozenset({"buy", "add"})
_EXIT_ACTIONS = frozenset({"sell", "reduce"})


@dataclass(frozen=True, slots=True)
class IssuedForecastRecord:
    """A forecast as it was actually shown by Tab1 or Tab3.

    The model forecast itself is mode-independent.  Mode and source belong to
    the frozen report that issued it, so the evaluation projection keeps that
    context separate instead of mutating the forecast ledger.
    """

    forecast: ForecastResult
    outcome: ForecastOutcome | None
    analysis_mode: DecisionMode
    report_kind: ReportKind
    report_id: str
    issued_at: datetime

    @property
    def instrument(self):
        return self.forecast.instrument

    @property
    def horizon(self):
        return self.forecast.horizon

    @property
    def target_session_date(self):
        return self.forecast.target_session_date

    @property
    def status(self):
        return self.outcome.status if self.outcome is not None else OutcomeStatus.PENDING

    @property
    def source_ref(self):
        return f"external:issued_forecast_{stable_hash((self.report_id, self.forecast.event_key, self.analysis_mode.value))}"


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
    if isinstance(item, IssuedForecastRecord):
        return _value(item.status)
    return _value(getattr(item, "status", None))


def _ledger(item):
    if isinstance(item, IssuedForecastRecord):
        return LedgerViewKind.FORECAST
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
    if isinstance(item, IssuedForecastRecord):
        return item.source_ref
    try:
        return presentation_source_refs(item)[0]
    except ContractViolation:
        return f"external:historical_{stable_hash(item)}"


def _item_date(item):
    if isinstance(item, IssuedForecastRecord):
        return item.issued_at
    value = getattr(item, "evaluated_at", None) or getattr(item, "generated_at", None)
    if value is None:
        raw = getattr(item, "origin_session_date", None)
        return datetime.combine(raw, datetime.min.time(), tzinfo=timezone.utc) if isinstance(raw, date) else None
    return value


def _detail_window(values, query, built_at):
    """Keep summaries broad while making the default audit list readable."""
    items = tuple(values)
    if query.date_from is not None or query.date_to is not None:
        return items
    cutoff = built_at - timedelta(days=DEFAULT_DETAIL_LOOKBACK_DAYS)
    return tuple(item for item in items if (_item_date(item) or built_at) >= cutoff)


def _matches(query, item, *, record_modes=None, record_sources=None):
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
    if query.analysis_mode is not None:
        mode = (
            item.analysis_mode
            if isinstance(item, IssuedForecastRecord)
            else (record_modes or {}).get(_ref(item))
        )
        if mode is not query.analysis_mode:
            return False
    if query.report_kind is not None:
        source = (
            item.report_kind
            if isinstance(item, IssuedForecastRecord)
            else (record_sources or {}).get(_ref(item))
        )
        origin = _value(getattr(item, "evidence_origin", None))
        if (
            isinstance(item, IssuedForecastRecord)
            or origin == "issued_online"
        ) and source is not query.report_kind:
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
        scope_parts = item.scope_key.split(":")
        dimensions = {
            "horizon": token[1:]
            for token in scope_parts
            if token.startswith("h") and token[1:].isdigit()
        }
    else:
        return False
    if query.ledger_kind is not None and ledger is not query.ledger_kind:
        return False
    if market is not query.market:
        return False
    if query.instrument is not None:
        scope_key = getattr(item, "scope_key", "")
        if scope_key != query.instrument.stable_key and not scope_key.startswith(query.instrument.stable_key + ":"):
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
    if query.analysis_mode is not None and dimensions.get("analysis_mode") != query.analysis_mode.value:
        return False
    if query.report_kind is not None and dimensions.get("report_kind") != query.report_kind.value:
        return False
    return not ((query.date_from and cutoff < query.date_from) or (query.date_to and cutoff > query.date_to))


def _row(row_id, cells, item):
    return ReportTableRow(str(row_id), tuple(str(cell) for cell in cells), None, (_ref(item),))


_MODE_NAMES = {
    DecisionMode.PRE: "盘前",
    DecisionMode.INTRADAY: "盘中",
    DecisionMode.EOD: "盘后",
}
_REPORT_KIND_NAMES = {
    ReportKind.SINGLE_STOCK: "单股分析",
    ReportKind.PORTFOLIO: "我的持仓",
}
_DIRECTION_NAMES = {"bullish": "上涨", "neutral": "震荡", "bearish": "下跌"}


def _direction_name(value):
    return _DIRECTION_NAMES.get(_value(value), "—")


def _issued_forecast_row(item: IssuedForecastRecord):
    result = item.forecast
    outcome = item.outcome
    probability = (
        result.probabilities.for_direction(result.direction)
        if result.probabilities is not None and result.direction is not None
        else None
    )
    prediction = (
        f"{_direction_name(result.direction)} {probability:.0%}"
        if probability is not None
        else "本次未形成可用预测"
    )
    if outcome is None or outcome.status is OutcomeStatus.PENDING:
        actual = "等待目标日"
        conclusion = "待验证"
    elif outcome.status is OutcomeStatus.MATURED:
        actual = f"{_direction_name(outcome.actual_direction)} {format_percent(outcome.actual_return)}"
        conclusion = "正确" if outcome.direction_correct else "错误"
    else:
        actual = "无法验证"
        conclusion = "不可验证"
    cells = (
        format_datetime(item.issued_at, item.instrument.market),
        _MODE_NAMES[item.analysis_mode],
        _REPORT_KIND_NAMES[item.report_kind],
        item.instrument.code,
        result.target_session_date or "—",
        f"{result.horizon}日",
        prediction,
        actual,
        conclusion,
    )
    return _row(item.source_ref, cells, item)


def _mode_forecast_summary(issued):
    rows = []
    for mode in DecisionMode:
        values = tuple(item for item in issued if item.analysis_mode is mode)
        matured = tuple(
            item.outcome for item in values
            if item.outcome is not None and item.outcome.status is OutcomeStatus.MATURED
        )
        correct = sum(item.direction_correct is True for item in matured)
        interval = [item.interval_hit for item in matured if item.interval_hit is not None]
        conclusion = (
            "尚无已到期预测" if not matured else
            "样本积累中" if len(matured) < 10 else
            "可作观察" if len(matured) < 30 else
            "达到比较门槛"
        )
        refs = tuple(sorted(item.source_ref for item in values)) or (PRESENTATION_POLICY_REF,)
        rows.append(ReportTableRow(
            f"mode:{mode.value}",
            (
                _MODE_NAMES[mode], str(len(matured)),
                format_percent(correct / len(matured) if matured else None),
                format_percent(sum(interval) / len(interval) if interval else None),
                str(sum(_status(item) == "pending" for item in values)), conclusion,
            ),
            None, refs,
        ))
    return ReportTable(
        "mode_forecast_summary", "盘前、盘中、盘后能力对比",
        ("分析模式", "已验证", "方向正确率", "80%区间命中", "等待验证", "当前结论"),
        tuple(rows), None,
        "三种模式分别统计，互不覆盖；同一模式、同一目标日和周期只保留最后一次有效预测。",
    )


def _event_row(item):
    if isinstance(item, ForecastOutcome):
        result = "正确" if item.direction_correct else "错误"
        content = (
            item.instrument.code, _ledger(item).value, item.origin_session_date,
            item.target_session_date, _value(item.predicted_direction),
            _value(item.actual_direction), format_percent(item.actual_return),
            result, _value(item.evidence_grade),
        )
    elif isinstance(item, StrategyOutcome):
        if item.action in _EXIT_ACTIONS:
            result_value = (
                "退出质量 " + format_percent(item.exit_quality)
                if item.exit_quality is not None else "退出质量不可用"
            )
        else:
            result_value = "入场收益 " + format_percent(item.net_return)
        content = (
            item.instrument.code, _ledger(item).value, item.generated_at.date(),
            item.target_session_date, item.action, item.fill_outcome,
            result_value, _value(item.status),
            _value(item.execution_evidence_grade),
        )
    elif isinstance(item, JointOutcome):
        content = (
            "组合", _ledger(item).value, item.generated_at.date(),
            item.replay_window[1] if item.replay_window else item.generated_at.date(),
            _value(item.outcome_kind), item.profile or "—",
            format_percent(item.time_weighted_return), _value(item.status),
            _value(item.evidence_grade),
        )
    else:
        content = (
            item.instrument.code, _ledger(item).value, item.origin_session_date,
            item.target_session_date or "—", item.expected_direction or "—",
            item.actual_direction or "—", format_percent(item.actual_return),
            _value(item.status), item.evidence_grade,
        )
    return _row(_ref(item), content, item)


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
    joint_items = tuple(item for item in outcomes if isinstance(item, JointOutcome))
    if not joint_items:
        unavailable = (
            "单次策略事件可能重叠，且退出质量不是账户收益，不能直接复利。"
            "只有完整账户联合回放才能生成连续净值。"
        )
        return (
            empty_chart(ChartKind.CUMULATIVE_PERFORMANCE, "完整链路 OOF 累计表现", interpretation=unavailable, empty_state=unavailable),
            empty_chart(ChartKind.DRAWDOWN, "完整链路 OOF 回撤", interpretation=unavailable, empty_state=unavailable),
        )
    grouped = defaultdict(list)
    for item in joint_items:
        if item.replay_window is not None:
            origin = (
                "连续OOF" if item.evidence_origin.value == "reconstructed_oof"
                else "实际建议" if item.evidence_origin.value == "issued_online"
                else "影子观察"
            )
            grouped[(f"{origin}·{item.profile or '组合'}", item.replay_window)].append(item)
    by_profile = defaultdict(list)
    for (profile, window), values in grouped.items():
        returns = [float(item.time_weighted_return) for item in values]
        benchmarks = [
            float(item.benchmark_return)
            for item in values if item.benchmark_return is not None
        ]
        by_profile[profile].append((
            window,
            sum(returns) / len(returns),
            sum(benchmarks) / len(benchmarks) if benchmarks else 0.0,
        ))
    cumulative_series = []
    drawdown_series = []
    all_labels = []
    selected_count = 0
    for profile, values in sorted(by_profile.items()):
        ordered = sorted(values, key=lambda item: item[0])
        selected = []
        last_end = None
        for item in ordered:
            if last_end is None or item[0][0] > last_end:
                selected.append(item)
                last_end = item[0][1]
        equity = benchmark_equity = peak = 1.0
        cumulative = []
        benchmark_points = []
        drawdown = []
        for window, result, benchmark in selected:
            equity *= 1 + result
            benchmark_equity *= 1 + benchmark
            peak = max(peak, equity)
            key = f"{window[1]}"
            if key not in all_labels:
                all_labels.append(key)
            cumulative.append((key, equity - 1))
            benchmark_points.append((key, benchmark_equity - 1))
            drawdown.append((key, equity / peak - 1))
        if cumulative:
            display = profile.replace("conservative", "保守方案").replace("aggressive", "激进方案")
            cumulative_series.extend(((display, tuple(cumulative)), (f"{display}基准", tuple(benchmark_points))))
            drawdown_series.append((display, tuple(drawdown)))
            selected_count += len(selected)
    if not cumulative_series:
        return (
            empty_chart(ChartKind.CUMULATIVE_PERFORMANCE, "完整链路 OOF 累计表现", interpretation="没有可形成连续窗口的 OOF 结果。", empty_state=warning),
            empty_chart(ChartKind.DRAWDOWN, "完整链路 OOF 回撤", interpretation="没有可形成连续窗口的 OOF 结果。", empty_state=warning),
        )
    baseline = tuple((key, 0.0) for key in sorted(all_labels))
    sample_range = (min(all_labels), max(all_labels))
    cumulative_series = tuple(cumulative_series)
    drawdown_series = tuple(drawdown_series)
    cum = ChartSpec(
        stable_hash(("cumulative", cumulative_series)), ChartKind.CUMULATIVE_PERFORMANCE,
        "完整链路 OOF 累计表现", "非重叠历史决策窗口结束日", "累计收益",
        cumulative_series, baseline, selected_count, sample_range,
        "同一窗口的候选回放先取平均，再只连接互不重叠的窗口；策略事件不会与组合净值混合复利。",
    )
    dd = ChartSpec(
        stable_hash(("drawdown", drawdown_series)), ChartKind.DRAWDOWN,
        "完整链路 OOF 回撤", "非重叠历史决策窗口结束日", "回撤",
        drawdown_series, baseline, selected_count, sample_range,
        "数值越接近 0 越好；负值表示相对该方案历史净值高点的下降。",
    )
    return cum, dd


class HistoricalEvaluationService:
    def build(
        self, query: HistoricalEvaluationQuery, *, outcomes=(), metrics=(),
        issued_forecasts=(), record_modes=None, record_sources=None, built_at: datetime,
    ):
        all_items = tuple(outcomes)
        allowed_outcomes = (ForecastOutcome, StrategyOutcome, JointOutcome, HypothesisOutcome)
        if any(not isinstance(item, allowed_outcomes) for item in all_items):
            raise ContractViolation("historical evaluation accepts only frozen outcome contracts")
        if any(not isinstance(item, (LearningMetricSnapshot, ResearchMetricSnapshot)) for item in metrics):
            raise ContractViolation("historical evaluation accepts only frozen metric snapshots")
        if any(not isinstance(item, IssuedForecastRecord) for item in issued_forecasts):
            raise ContractViolation("issued forecast projection accepts only frozen report links")
        issued_candidates = tuple(
            item for item in issued_forecasts
            if query.report_kind is None or item.report_kind is query.report_kind
        )
        def latest_by_mode(values):
            latest = {}
            for item in values:
                key = (
                    item.instrument, item.analysis_mode,
                    item.target_session_date, item.horizon,
                )
                previous = latest.get(key)
                if previous is None or (item.issued_at, item.report_id) > (previous.issued_at, previous.report_id):
                    latest[key] = item
            return tuple(sorted(
                latest.values(),
                key=lambda item: (item.issued_at, item.report_id), reverse=True,
            ))

        issued = latest_by_mode(issued_candidates)
        portfolio_issued_latest = latest_by_mode(
            item for item in issued_forecasts
            if item.report_kind is ReportKind.PORTFOLIO
        )
        selected_issued = tuple(
            item for item in issued
            if _matches(query, item, record_modes=record_modes, record_sources=record_sources)
        )
        selected = tuple(
            item for item in all_items
            if _matches(query, item, record_modes=record_modes, record_sources=record_sources)
        )
        selected_metrics = tuple(item for item in metrics if _metric_matches(query, item))
        matured = tuple(item for item in selected if _status(item) == OutcomeStatus.MATURED.value)
        for item in matured:
            origin = getattr(item, "origin_session_date", None)
            target = getattr(item, "target_session_date", None)
            if origin is not None and target is not None and target <= origin:
                raise ContractViolation("historical outcome target date precedes prediction")
        issued_forecast_scope = (
            query.ledger_kind is LedgerViewKind.FORECAST
            and (query.analysis_mode is not None or query.report_kind is not None)
        )
        mode_comparison_issued = tuple(
            item for item in issued
            if item.instrument.market is query.market
            and (query.instrument is None or item.instrument == query.instrument)
            and (query.horizon is None or item.horizon == query.horizon)
            and (query.date_from is None or item.issued_at >= query.date_from)
            and (query.date_to is None or item.issued_at <= query.date_to)
        )
        refs = tuple(sorted({
            PRESENTATION_POLICY_REF,
            *(_ref(item) for item in (*selected, *selected_metrics, *selected_issued, *mode_comparison_issued, *portfolio_issued_latest)),
            *(
                _ref(item.outcome)
                for item in (*selected_issued, *mode_comparison_issued, *portfolio_issued_latest)
                if item.outcome is not None
            ),
        }))
        ledger_rows = []
        for ledger in LedgerViewKind:
            values = (
                selected_issued
                if ledger is LedgerViewKind.FORECAST and issued_forecast_scope
                else tuple(item for item in selected if _ledger(item) is ledger)
            )
            ledger_rows.append(ReportTableRow(ledger.value, (ledger.value, str(sum(_status(item) == "matured" for item in values)), str(len(values)), "独立账本，不与其他命中率合并"), None, tuple(sorted({_ref(item) for item in values})) or (PRESENTATION_POLICY_REF,)))
        ledgers = ReportTable("ledger_summary", "账本独立性", ("账本", "成熟样本", "查询样本", "说明"), tuple(ledger_rows), None, "预测账、策略账、联合账和 LLM 研究账分别展示。")
        forecast_items = tuple(item for item in matured if isinstance(item, ForecastOutcome))
        if issued_forecast_scope:
            forecast_items = tuple(dict.fromkeys(
                item.outcome for item in selected_issued
                if item.outcome is not None and item.outcome.status is OutcomeStatus.MATURED
            ))
        effective_matured_count = len(forecast_items) if issued_forecast_scope else len(matured)
        warning = maturity_message(effective_matured_count)
        if effective_matured_count == 1:
            warning += " 当前仅有 1 条已验证样本，不能据此判断模型稳定性。"
        strategy_items = tuple(item for item in matured if isinstance(item, StrategyOutcome))
        joint_items = tuple(item for item in matured if isinstance(item, JointOutcome))
        research_items = tuple(item for item in matured if isinstance(item, HypothesisOutcome))
        detail_items = tuple(sorted(
            _detail_window(matured, query, built_at),
            key=lambda item: (_item_date(item) or datetime.min.replace(tzinfo=timezone.utc), _ref(item)),
            reverse=True,
        ))[:MAX_EVALUATION_DETAIL_ROWS]
        event_rows = tuple(_event_row(item) for item in detail_items)
        events = ReportTable(
            "outcome_events", "最近逐事件审计",
            ("股票", "账本", "发行/起始日", "目标/结束日", "预测/动作", "实际/成交", "收益", "结果", "证据"),
            event_rows, warning,
            f"仅展示最近 {MAX_EVALUATION_DETAIL_ROWS} 条冻结 outcome；筛选股票或周期可进一步缩小范围。",
        )
        regime_groups = defaultdict(list)
        for item in matured:
            regime_groups[getattr(item, "market_regime_key", None) or "未分层"].append(item)
        regime_rows = []
        for regime, values in sorted(regime_groups.items()):
            forecast_values = [item for item in values if isinstance(item, ForecastOutcome) and item.direction_correct is not None]
            entry_returns = [
                float(item.net_return) for item in values
                if isinstance(item, StrategyOutcome)
                and item.action in _ENTRY_ACTIONS and item.net_return is not None
            ]
            exit_quality = [
                float(item.exit_quality) for item in values
                if isinstance(item, StrategyOutcome)
                and item.action in _EXIT_ACTIONS and item.exit_quality is not None
            ]
            regime_rows.append(ReportTableRow(
                f"regime:{regime}",
                (
                    regime, str(len(values)),
                    format_percent(sum(item.direction_correct for item in forecast_values) / len(forecast_values) if forecast_values else None),
                    format_percent(sum(entry_returns) / len(entry_returns) if entry_returns else None),
                    format_percent(sum(exit_quality) / len(exit_quality) if exit_quality else None),
                ),
                None, tuple(sorted({_ref(item) for item in values})),
            ))
        regimes = ReportTable(
            "regime_slices", "市场状态分层",
            ("市场状态", "样本数", "预测正确率", "入场平均净收益", "平均退出质量"),
            tuple(regime_rows), "暂无市场状态分层。",
            "入场收益和退出质量分别聚合；退出质量为正表示当时退出优于继续持有。",
        )
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
        forecast_groups = defaultdict(list)
        for item in forecast_items:
            forecast_groups[item.instrument].append(item)
        forecast_summary_rows = []
        for instrument_key, values in sorted(forecast_groups.items(), key=lambda pair: pair[0].stable_key):
            correct = sum(item.direction_correct is True for item in values)
            brier_values = [item.event_brier for item in values if item.event_brier is not None]
            log_loss_values = [item.event_log_loss for item in values if item.event_log_loss is not None]
            interval_values = [item.interval_hit for item in values if item.interval_hit is not None]
            count = len(values)
            conclusion = (
                "样本积累中，暂不判断稳定性" if count < 10 else
                "可作观察，尚未达到稳定比较门槛" if count < 30 else
                "已达到比较门槛；继续观察分层与漂移"
            )
            forecast_summary_rows.append(ReportTableRow(
                f"forecast-summary:{instrument_key.stable_key}",
                (
                    instrument_key.code,
                    str(count),
                    format_percent(correct / count if count else None),
                    f"{sum(brier_values) / len(brier_values):.3f}" if brier_values else "—",
                    f"{sum(log_loss_values) / len(log_loss_values):.3f}" if log_loss_values else "—",
                    format_percent(sum(interval_values) / len(interval_values) if interval_values else None),
                    conclusion,
                ),
                None,
                tuple(sorted({_ref(item) for item in values})),
            ))
        forecast_summary = ReportTable(
            "forecast_performance_summary", "预测表现汇总",
            ("股票", "已验证预测", "方向正确率", "Brier", "Log Loss", "80%区间命中", "结论"),
            tuple(forecast_summary_rows), "暂无已验证预测。",
            "预测表现只回答预测是否准确。Brier 和 Log Loss 越低越好；样本少于 30 条时不宣称稳定。",
        )
        strategy_groups = defaultdict(list)
        for item in strategy_items:
            strategy_groups[item.instrument].append(item)
        strategy_summary_rows = []
        strategy_exit_rows = []
        for instrument_key, values in sorted(strategy_groups.items(), key=lambda pair: pair[0].stable_key):
            entries = tuple(
                item for item in values
                if item.action in _ENTRY_ACTIONS and item.net_return is not None
            )
            exits = tuple(
                item for item in values
                if item.action in _EXIT_ACTIONS and item.exit_quality is not None
            )
            returns = [float(item.net_return) for item in entries]
            paired_benchmarks = [
                float(item.benchmark_return) for item in entries
                if item.benchmark_return is not None
            ]
            adverse = [float(item.mae) for item in entries if item.mae is not None]
            average = sum(returns) / len(returns) if returns else None
            benchmark = (
                sum(paired_benchmarks) / len(paired_benchmarks)
                if len(paired_benchmarks) == len(entries) and paired_benchmarks else None
            )
            count = len(returns)
            conclusion = (
                "尚无可复盘成交" if not count else
                "样本积累中，暂不判断稳定性" if count < 10 else
                "平均净收益为正，仍需扩大样本" if average is not None and average > 0 else
                "当前平均净收益未转正，需要继续优化"
            )
            strategy_summary_rows.append(ReportTableRow(
                f"strategy-summary:{instrument_key.stable_key}",
                (
                    instrument_key.code,
                    str(count),
                    format_percent(sum(item > 0 for item in returns) / count if count else None),
                    format_percent(average),
                    format_percent(benchmark),
                    format_percent(average - benchmark if average is not None and benchmark is not None else None),
                    format_percent(min(adverse) if adverse else None),
                    conclusion,
                ),
                None,
                tuple(sorted({_ref(item) for item in values})),
            ))
            quality = [float(item.exit_quality) for item in exits]
            avoided = [float(item.exit_avoided_loss) for item in exits if item.exit_avoided_loss is not None]
            opportunity = [float(item.exit_opportunity_cost) for item in exits if item.exit_opportunity_cost is not None]
            exit_count = len(quality)
            average_quality = sum(quality) / exit_count if quality else None
            exit_conclusion = (
                "尚无可评估退出" if not exit_count else
                "样本积累中，暂不判断稳定性" if exit_count < 10 else
                "退出时机平均有效，仍需扩大样本" if average_quality is not None and average_quality > 0 else
                "退出时机平均未创造价值，需要继续优化"
            )
            strategy_exit_rows.append(ReportTableRow(
                f"strategy-exit:{instrument_key.stable_key}",
                (
                    instrument_key.code, str(exit_count),
                    format_percent(sum(item > 0 for item in quality) / exit_count if exit_count else None),
                    format_percent(average_quality),
                    format_percent(sum(avoided) / len(avoided) if avoided else None),
                    format_percent(sum(opportunity) / len(opportunity) if opportunity else None),
                    exit_conclusion,
                ),
                None, tuple(sorted({_ref(item) for item in exits})) or (PRESENTATION_POLICY_REF,),
            ))
        strategy_summary = ReportTable(
            "strategy_performance_summary", "入场策略表现",
            ("股票", "已复盘入场", "盈利率", "平均交易净收益", "同期买入持有", "平均超额", "最差不利波动", "结论"),
            tuple(strategy_summary_rows), "暂无成熟入场交易。",
            "这里只统计买入/加仓后的真实持有期收益。牛市中收益接近基准但不利波动明显更小，也属于有价值的策略表现。",
        )
        strategy_exit_summary = ReportTable(
            "strategy_exit_quality_summary", "退出时机表现",
            ("股票", "已评估退出", "有效退出率", "平均退出质量", "平均避免损失", "平均踏空成本", "结论"),
            tuple(strategy_exit_rows), "暂无成熟卖出或减仓记录。",
            "退出质量 = 避免的后续损失 - 卖出后的踏空成本 - 交易摩擦；为正表示当时退出优于继续持有，它不是账户收益率。",
        )
        joint_groups = defaultdict(list)
        for item in joint_items:
            joint_groups[(item.evidence_origin.value, item.outcome_kind.value, item.profile or "组合")].append(item)
        joint_summary_rows = []
        for (origin, outcome_kind, profile), values in sorted(joint_groups.items()):
            windows = {item.replay_window for item in values if item.replay_window is not None}
            returns = [float(item.time_weighted_return) for item in values]
            benchmarks = [float(item.benchmark_return) for item in values if item.benchmark_return is not None]
            alphas = [float(item.alpha) for item in values if item.alpha is not None]
            drawdowns = [float(item.max_drawdown) for item in values]
            sharpes = [float(item.sharpe) for item in values if item.sharpe is not None]
            joint_summary_rows.append(ReportTableRow(
                f"joint-summary:{profile}",
                (
                    "连续历史OOF" if origin == "reconstructed_oof" else "实际建议回放" if origin == "issued_online" else "影子观察",
                    "保守" if profile == "conservative" else "激进" if profile == "aggressive" else profile,
                    str(len(windows)),
                    str(len(values)),
                    format_percent(sum(returns) / len(returns) if returns else None),
                    format_percent(sum(benchmarks) / len(benchmarks) if benchmarks else None),
                    format_percent(sum(alphas) / len(alphas) if alphas else None),
                    format_percent(min(drawdowns) if drawdowns else None),
                    f"{sum(sharpes) / len(sharpes):.2f}" if sharpes else "—",
                ),
                None,
                tuple(sorted({_ref(item) for item in values})),
            ))
        joint_summary = ReportTable(
            "joint_performance_summary", "完整链路表现",
            ("统计口径", "方案", "独立决策窗口", "候选回放次数", "平均系统收益", "平均买入持有", "平均超额", "最差回撤", "平均Sharpe"),
            tuple(joint_summary_rows), "暂无成熟完整链路回放。",
            "同一历史窗口可能包含多个候选回放，因此同时列出独立窗口和候选次数。不能只看超额收益，还要比较最大回撤和 Sharpe。",
        )
        issued_forecast_events = ReportTable(
            "issued_forecast_details",
            "实际发行预测 · 筛选区间" if query.date_from is not None or query.date_to is not None else "实际发行预测 · 最近30天",
            ("生成时间", "分析模式", "来源", "股票", "目标交易日", "周期", "最终预测", "实际结果", "验证结论"),
            tuple(
                _issued_forecast_row(item)
                for item in _detail_window(selected_issued, query, built_at)[:MAX_EVALUATION_DETAIL_ROWS]
            ),
            "所选区间暂无实际发行预测。" if query.date_from is not None or query.date_to is not None else "最近30天暂无实际发行预测。",
            "汇总默认使用全部历史，明细默认只展示最近30天；选择日期后改为所选区间。Tab1和Tab3共享预测账，同模式、同目标日和周期只评估最后一次有效版本。",
        )
        mode_forecast_summary = _mode_forecast_summary(mode_comparison_issued)

        strategy_source_rows = []
        for origin in ("reconstructed_oof", "issued_online", "shadow_online"):
            values = tuple(item for item in strategy_items if item.evidence_origin.value == origin)
            entries = tuple(item for item in values if item.action in _ENTRY_ACTIONS and item.net_return is not None)
            exits = tuple(item for item in values if item.action in _EXIT_ACTIONS and item.exit_quality is not None)
            returns = [float(item.net_return) for item in entries]
            quality = [float(item.exit_quality) for item in exits]
            label = {"reconstructed_oof": "连续历史OOF回测", "issued_online": "实际建议回放", "shadow_online": "影子观察"}[origin]
            strategy_source_rows.append(ReportTableRow(
                f"strategy-source:{origin}",
                (
                    label, str(len(entries)),
                    format_percent(sum(returns) / len(returns) if returns else None),
                    str(len(exits)),
                    format_percent(sum(quality) / len(quality) if quality else None),
                    "完整历史能力" if origin == "reconstructed_oof" else "仅使用真实运行时产生的建议" if origin == "issued_online" else "不参与执行",
                ),
                None,
                tuple(sorted({_ref(item) for item in values})) or (PRESENTATION_POLICY_REF,),
            ))
        strategy_source_summary = ReportTable(
            "strategy_source_summary", "连续历史能力与实际使用结果",
            ("统计口径", "入场成交", "入场平均净收益", "退出评估", "平均退出质量", "说明"),
            tuple(strategy_source_rows), None,
            "日期只筛选已完成验证的记录，不代表区间内连续持仓。连续OOF不依赖用户每天运行；实际建议回放不补造空窗期决策。",
        )

        issued_matured = tuple(
            item.outcome for item in selected_issued
            if item.outcome is not None and item.outcome.status is OutcomeStatus.MATURED
        )
        issued_correct = sum(item.direction_correct is True for item in issued_matured)
        single_entries = tuple(item for item in strategy_items if item.action in _ENTRY_ACTIONS and item.net_return is not None)
        single_exits = tuple(item for item in strategy_items if item.action in _EXIT_ACTIONS and item.exit_quality is not None)
        single_returns = [float(item.net_return) for item in single_entries]
        single_exit_quality = [float(item.exit_quality) for item in single_exits]
        portfolio_issued_records = tuple(
            item for item in portfolio_issued_latest
            if _matches(query, item, record_modes=record_modes, record_sources=record_sources)
        )
        portfolio_issued = tuple(
            item.outcome for item in selected_issued
            if item.report_kind is ReportKind.PORTFOLIO
            and item.outcome is not None and item.outcome.status is OutcomeStatus.MATURED
        )
        if query.report_kind is None:
            portfolio_issued = tuple(
                item.outcome for item in portfolio_issued_records
                if item.outcome is not None and item.outcome.status is OutcomeStatus.MATURED
            )
        portfolio_correct = sum(item.direction_correct is True for item in portfolio_issued)
        joint_returns = [float(item.time_weighted_return) for item in joint_items]
        joint_drawdowns = [float(item.max_drawdown) for item in joint_items]
        capability_rows = (
            ReportTableRow(
                "capability:single_forecast",
                ("单股预测", str(len(issued_matured)), format_percent(issued_correct / len(issued_matured) if issued_matured else None), "预测方向是否正确", maturity_message(len(issued_matured))),
                None, tuple(sorted(item.source_ref for item in selected_issued)) or (PRESENTATION_POLICY_REF,),
            ),
            ReportTableRow(
                "capability:single_strategy",
                (
                    "单股策略", str(len(single_entries) + len(single_exits)),
                    f"入场 {format_percent(sum(single_returns) / len(single_returns) if single_returns else None)} / 退出 {format_percent(sum(single_exit_quality) / len(single_exit_quality) if single_exit_quality else None)}",
                    "分别评价入场是否赚钱、退出是否及时",
                    maturity_message(min(len(single_entries), len(single_exits)) if single_entries and single_exits else max(len(single_entries), len(single_exits))),
                ),
                None, tuple(sorted({_ref(item) for item in strategy_items})) or (PRESENTATION_POLICY_REF,),
            ),
            ReportTableRow(
                "capability:portfolio_forecast",
                ("组合预测", str(len(portfolio_issued)), format_percent(portfolio_correct / len(portfolio_issued) if portfolio_issued else None), "当前为组合内成分股方向；强弱排序账仍需单独积累", maturity_message(len(portfolio_issued))),
                None, tuple(sorted(item.source_ref for item in portfolio_issued_records)) or (PRESENTATION_POLICY_REF,),
            ),
            ReportTableRow(
                "capability:portfolio_strategy",
                ("组合策略", str(len(joint_returns)), format_percent(sum(joint_returns) / len(joint_returns) if joint_returns else None), format_percent(min(joint_drawdowns) if joint_drawdowns else None) + " 最差回撤", maturity_message(len(joint_returns))),
                None, tuple(sorted({_ref(item) for item in joint_items})) or (PRESENTATION_POLICY_REF,),
            ),
        )
        capability_overview = ReportTable(
            "capability_overview", "系统能力总览",
            ("能力", "成熟样本", "核心结果", "评估内容", "当前结论"), capability_rows, None,
            "预测和策略、单股和组合分别评价。缺少专用证据时明确说明，不用其他指标代替。",
        )
        forecast_detail_items = tuple(sorted(
            _detail_window(forecast_items, query, built_at),
            key=lambda item: (_item_date(item) or datetime.min.replace(tzinfo=timezone.utc), _ref(item)),
            reverse=True,
        ))[:MAX_EVALUATION_DETAIL_ROWS]
        strategy_detail_items = tuple(sorted(
            _detail_window((*strategy_items, *joint_items), query, built_at),
            key=lambda item: (_item_date(item) or datetime.min.replace(tzinfo=timezone.utc), _ref(item)),
            reverse=True,
        ))[:MAX_EVALUATION_DETAIL_ROWS]
        forecast_event_rows = tuple(_event_row(item) for item in forecast_detail_items)
        strategy_event_rows = tuple(_event_row(item) for item in strategy_detail_items)
        forecast_events = ReportTable(
            "forecast_event_details", "预测明细 · 筛选区间" if query.date_from is not None or query.date_to is not None else "预测明细 · 最近30天",
            events.columns, forecast_event_rows, "暂无预测明细。",
            "每一行直白展示何时预测、预测哪个目标日、实际结果和方向是否正确；汇总仍使用完整统计范围。",
        )
        strategy_events = ReportTable(
            "strategy_event_details", "策略与完整链路明细 · 筛选区间" if query.date_from is not None or query.date_to is not None else "策略与完整链路明细 · 最近30天",
            events.columns, strategy_event_rows, "暂无策略明细。",
            "每一行保留成交、收益和证据等级；默认只展示最近30天，汇总仍使用完整统计范围。",
        )
        timeline = _timeline(forecast_items, warning)
        cumulative, drawdown = _performance((*strategy_items, *joint_items), warning)
        tables = (
            ledgers, events, calibration_table, regimes, metric_table,
            forecast_summary, strategy_summary, strategy_exit_summary, joint_summary,
            forecast_events, strategy_events, capability_overview,
            mode_forecast_summary, issued_forecast_events, strategy_source_summary,
        )
        displayed_matured = len(issued_matured) if issued_forecast_scope else len(matured)
        display_warning = maturity_message(displayed_matured)
        if displayed_matured == 1:
            display_warning += " 当前仅有 1 条已验证样本，不能据此判断稳定性。"
        warnings = (display_warning,) if displayed_matured < 30 else ()
        return HistoricalEvaluationView(
            query,
            (("issued_matured_count", len(issued_matured)), ("matured_count", displayed_matured), ("pending_count", sum(_status(item) == "pending" for item in (*selected, *selected_issued))), ("message", display_warning)),
            tuple(sorted(headline)),
            (calibration_chart, timeline, cumulative, drawdown),
            tables, _GLOSSARY, warnings, refs, built_at,
        )


class RepositoryHistoricalEvaluationService:
    def __init__(self, repository, *, clock=lambda: datetime.now(timezone.utc)):
        self.repository=repository
        self.clock=clock
        self.builder=HistoricalEvaluationService()
        self._record_cache = {}

    def invalidate(self, market=None):
        """Discard read projections after the user requests an explicit refresh."""
        if market is None:
            self._record_cache.clear()
        else:
            self._record_cache.pop(market, None)

    def _reports(self, market):
        items = []
        page = 1
        while True:
            result = self.repository.list_report_history(ReportHistoryQuery(
                market=market, include_archived=True, page=page, page_size=1000,
            ))
            items.extend(result.items)
            if not result.has_next:
                return tuple(items)
            page += 1

    def _issued_forecasts(self, market, outcomes, reports):
        outcomes_by_event = {}
        for item in outcomes:
            if not isinstance(item, ForecastOutcome):
                continue
            previous = outcomes_by_event.get(item.forecast_event_key)
            if previous is None or (
                item.status is not OutcomeStatus.SUPERSEDED,
                item.evaluated_at,
                item.forecast_outcome_id,
            ) > (
                previous.status is not OutcomeStatus.SUPERSEDED,
                previous.evaluated_at,
                previous.forecast_outcome_id,
            ):
                outcomes_by_event[item.forecast_event_key] = item
        records = []
        forecasts_by_ref = {
            presentation_source_refs(item)[0]: item
            for item in self.repository.list_market_forecast_results(market)
        }
        for report in reports:
            for ref in report.source_artifact_refs:
                if not ref.startswith("forecast:"):
                    continue
                forecast = forecasts_by_ref.get(ref)
                if forecast is None:
                    continue
                records.append(IssuedForecastRecord(
                    forecast, outcomes_by_event.get(forecast.event_key), report.analysis_mode,
                    report.report_kind, report.report_id, report.created_at,
                ))
        return tuple(records)

    def _record_context(self, outcomes, reports):
        report_by_scenario = {}
        for report in reports:
            for ref in report.source_artifact_refs:
                if not ref.startswith("scenario:"):
                    continue
                scenario_id = ref.split(":", 1)[1]
                previous = report_by_scenario.get(scenario_id)
                if previous is None or (report.created_at, report.report_id) > (previous.created_at, previous.report_id):
                    report_by_scenario[scenario_id] = report
        modes = {}
        sources = {}
        for item in outcomes:
            if isinstance(item, StrategyOutcome):
                scenario = self.repository.get_trading_scenario(item.scenario_id)
                if scenario is not None:
                    modes[_ref(item)] = scenario.mode
                report = report_by_scenario.get(item.scenario_id)
                if report is not None:
                    sources[_ref(item)] = report.report_kind
            elif isinstance(item, JointOutcome):
                batch = self.repository.get_portfolio_input_batch(item.batch_id) if item.batch_id else None
                if batch is None and item.portfolio_bundle_id:
                    bundle = self.repository.get_portfolio_decision_bundle(item.portfolio_bundle_id)
                    batch = self.repository.get_portfolio_input_batch(bundle.batch_id) if bundle is not None else None
                if batch is not None:
                    modes[_ref(item)] = batch.mode
                    sources[_ref(item)] = ReportKind.PORTFOLIO
        return modes, sources

    def load(self, query: HistoricalEvaluationQuery):
        cached = self._record_cache.get(query.market)
        if cached is None:
            outcomes,metrics=self.repository.list_historical_evaluation_records(query.market)
            reports = self._reports(query.market)
            issued = self._issued_forecasts(query.market, outcomes, reports)
            modes, sources = self._record_context(outcomes, reports)
            cached = (outcomes, metrics, issued, modes, sources)
            self._record_cache[query.market] = cached
        outcomes, metrics, issued, modes, sources = cached
        return self.builder.build(
            query, outcomes=outcomes, metrics=metrics, issued_forecasts=issued,
            record_modes=modes, record_sources=sources, built_at=self.clock(),
        )
