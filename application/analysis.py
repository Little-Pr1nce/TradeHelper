"""Production orchestration for Tab1 and Tab3.

The application layer coordinates frozen V2 contracts.  It never calculates a
technical indicator itself and never turns missing facts into executable advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Callable, Mapping
import logging
from time import monotonic

from contracts import (
    AnalysisRunResult, AnalysisRunStatus, AnalysisStage, AvailabilitySource,
    DecisionMode, ExecutionPolicy, FeatureEvidenceMode, FeatureInputs,
    FreshnessStatus, InstrumentId, LearningMetricSnapshot, LedgerKind, Market, PortfolioCandidate,
    PortfolioInputBatch, PortfolioPolicy, PortfolioRole, PositionAvailability,
    ProviderStatus, RiskPolicy, RiskRequest, SingleStockAnalysisCommand,
    PortfolioAnalysisCommand, ResearchRunStatus, ResearchScope, RiskProfile, StockMetadata, StrategyInput, TaskStatus,
    ValuationPrice, ValuationPriceKind, stable_hash,
)
from contracts.forecast import ForecastRequest
from forecast import build_training_sample
from learning.evidence import plan_evidence
from contracts.scenario import ScenarioRequest
from data.calendar import TradingCalendarUnavailable
from data.quality import evaluate_data_quality
from execution import OrderIntentFactory
from portfolio.evidence import (
    build_correlation_snapshot, build_holding_risks,
)
from presentation.inputs import portfolio_input, single_stock_input
from presentation.report_builder import PortfolioReportBuilder, SingleStockReportBuilder
from risk import freeze_account_valuation
from risk.market_rules import default_market_rules
from strategies.registry import default_specs
from application.tasks import AnalysisTaskCoordinator
from application.report_editor import edit_report
from research.client import LLMResearchRequest, OpenAICompatibleResearchClient, capabilities_for_endpoint, output_token_budget
from research.context import ResearchContextBuilder
from research.prompt import PROMPT_VERSION, build_prompt, build_prompt_chunks


_HISTORY_DAYS = {"1m": 45, "3m": 140, "6m": 280, "1y": 540}
_FORECAST_HISTORY_DAYS = 1900
_FORECAST_MAX_ORIGINS = 720
logger = logging.getLogger(__name__)


def _provider_attempt_trace(result) -> str:
    attempts = []
    for attempt in getattr(result, "attempts", ()):
        duration_ms = max(
            0,
            int((attempt.finished_at - attempt.started_at).total_seconds() * 1000),
        )
        error = f"/{attempt.error_code}" if attempt.error_code else ""
        attempts.append(
            f"{attempt.provider}:{attempt.status.value}{error}:{duration_ms}ms"
        )
    return ",".join(attempts) if attempts else "cache_or_no_attempt"


def _quality_issue_trace(report) -> str:
    return ",".join(
        f"{item.code}/{item.severity.value}/{item.message}"
        for item in getattr(report, "issues", ())
    ) or "none"


@dataclass(frozen=True, slots=True)
class _Facts:
    instrument: InstrumentId
    metadata: StockMetadata
    listing_date: date | None
    bars: tuple
    quote: object | None
    news: tuple
    news_status: ProviderStatus
    fundamentals: object | None
    fundamentals_status: ProviderStatus
    quality: object


@dataclass(frozen=True, slots=True)
class _InstrumentAnalysis:
    presentation: object
    scenario: object
    strategy_bundle: object
    risk_bundle: object
    plans: Mapping[str, object]
    bars: tuple


class RuntimeAnalysisPipeline:
    """Connect data -> features -> forecast -> scenario -> strategy -> risk."""

    def __init__(self, container):
        self.container = container
        self._research_inputs = {}
        self._research_lock = Lock()

    @staticmethod
    def _emit(callback, stage, instrument=None, message=None):
        if callback:
            callback(stage, instrument, message or stage.value)

    @staticmethod
    def _decision_cutoff(requested_at, facts_values):
        """Return the common point-in-time cutoff for facts fetched in this run.

        ``requested_at`` freezes the provider request boundary. News/fundamental
        first-visibility and quote observation may occur while that request is in
        flight, so the downstream decision cutoff must include those timestamps.
        """
        candidates = [requested_at]
        for facts in facts_values:
            if facts.quote is not None:
                candidates.append(facts.quote.observed_at)
            candidates.extend(item.available_at for item in facts.news)
            if facts.fundamentals is not None:
                candidates.append(facts.fundamentals.available_at)
                candidates.extend(
                    item.published_at for item in facts.fundamentals.fields.values()
                    if item.published_at is not None
                )
        return max(candidates)

    def _align_facts_quality(self, facts, mode, history_period, requested_at, cutoff_at):
        requested_start = requested_at.date() - timedelta(days=_HISTORY_DAYS.get(history_period, 140))
        quality = evaluate_data_quality(
            facts.bars,
            market=facts.instrument.market,
            mode=mode,
            as_of=cutoff_at,
            quote=facts.quote,
            news_available=bool(facts.news),
            fundamentals_available=facts.fundamentals is not None,
            listing_date=facts.listing_date,
            requested_start=requested_start,
            calendar=self.container.calendar,
        )
        logger.info(
            "Facts aligned instrument=%s mode=%s requested_at=%s cutoff_at=%s "
            "quality=%s score=%.1f issues=%s",
            facts.instrument.stable_key,
            mode.value,
            requested_at.isoformat(),
            cutoff_at.isoformat(),
            quality.status.value,
            quality.score,
            _quality_issue_trace(quality),
        )
        return replace(facts, quality=quality)

    @staticmethod
    def _presentation_built_at(facts, cutoff_at):
        timestamps = [datetime.now(timezone.utc), cutoff_at, facts.metadata.fetched_at]
        if facts.quote is not None:
            timestamps.append(facts.quote.fetched_at)
        timestamps.extend(item.fetched_at for item in facts.news)
        if facts.fundamentals is not None:
            timestamps.append(facts.fundamentals.fetched_at)
        return max(timestamps)

    def _facts(self, instrument, mode, history_period, as_of, callback=None):
        data = self.container.data_refresh
        refresh_started = monotonic()
        logger.info(
            "Fact refresh started instrument=%s market=%s mode=%s history=%s requested_at=%s",
            instrument.stable_key,
            instrument.market.value,
            mode.value,
            history_period,
            as_of.isoformat(),
        )
        self._emit(callback, AnalysisStage.REFRESH_METADATA, instrument)
        metadata_result = data.refresh_metadata(instrument, as_of)
        listing_result = data.refresh_listing_date(instrument, as_of)
        metadata = metadata_result.value if metadata_result.status is ProviderStatus.OK else self.container.repository.get_stock_metadata(instrument)
        listing_date = listing_result.value if listing_result.status is ProviderStatus.OK else getattr(metadata, "listing_date", None)
        if metadata is None:
            metadata = StockMetadata(instrument, instrument.code, None, None, listing_date, "instrument_identity", as_of)

        self._emit(callback, AnalysisStage.REFRESH_MARKET_DATA, instrument)
        requested_start = as_of.date() - timedelta(days=max(
            _HISTORY_DAYS.get(history_period, 140), _FORECAST_HISTORY_DAYS,
        ))
        try:
            requested_end = self.container.calendar.latest_completed_session(instrument.market, as_of)
        except TradingCalendarUnavailable:
            requested_end = as_of.date()
        bars_result = data.refresh_daily_bars(instrument, requested_start, requested_end, listing_date, as_of)
        bars = bars_result.value if bars_result.status is ProviderStatus.OK and bars_result.value else ()
        quote_result = data.refresh_quote(instrument, mode, as_of)
        quote = quote_result.value if quote_result.status is ProviderStatus.OK else None
        news_result = data.refresh_news(instrument, mode, as_of)
        news = news_result.value if news_result.status is ProviderStatus.OK and news_result.value else ()
        if news:
            news = self.container.finbert.enrich(news)
            self.container.repository.upsert_news(news)
        fundamental_result = data.refresh_fundamentals(instrument, as_of)
        fundamentals = fundamental_result.value if fundamental_result.status is ProviderStatus.OK else None
        quality = evaluate_data_quality(
            bars, market=instrument.market, mode=mode, as_of=as_of, quote=quote,
            news_available=bool(news), fundamentals_available=fundamentals is not None,
            listing_date=listing_date, requested_start=requested_start,
            calendar=self.container.calendar,
        )
        bar_range = (
            f"{bars[0].trading_date.isoformat()}..{bars[-1].trading_date.isoformat()}"
            if bars else "none"
        )
        quote_detail = (
            f"price={quote.price} observed_at={quote.observed_at.isoformat()} "
            f"freshness={quote.freshness_status.value}"
            if quote is not None else "price=none"
        )
        fundamental_fields = len(fundamentals.fields) if fundamentals is not None else 0
        logger.info(
            "Provider result instrument=%s fact=metadata status=%s source=%s listing_status=%s "
            "listing_date=%s attempts=%s listing_attempts=%s",
            instrument.stable_key,
            metadata_result.status.value,
            metadata_result.selected_source or "none",
            listing_result.status.value,
            listing_date.isoformat() if listing_date else "unknown",
            _provider_attempt_trace(metadata_result),
            _provider_attempt_trace(listing_result),
        )
        logger.info(
            "Provider result instrument=%s fact=daily_bars status=%s source=%s count=%d "
            "range=%s attempts=%s fallback=%s",
            instrument.stable_key,
            bars_result.status.value,
            bars_result.selected_source or "none",
            len(bars),
            bar_range,
            _provider_attempt_trace(bars_result),
            bars_result.fallback_reason or "none",
        )
        logger.info(
            "Provider result instrument=%s fact=quote mode=%s status=%s source=%s %s "
            "attempts=%s fallback=%s",
            instrument.stable_key,
            mode.value,
            quote_result.status.value,
            quote_result.selected_source or "none",
            quote_detail,
            _provider_attempt_trace(quote_result),
            quote_result.fallback_reason or "none",
        )
        logger.info(
            "Provider result instrument=%s fact=news status=%s source=%s count=%d "
            "attempts=%s fallback=%s",
            instrument.stable_key,
            news_result.status.value,
            news_result.selected_source or "none",
            len(news),
            _provider_attempt_trace(news_result),
            news_result.fallback_reason or "none",
        )
        logger.info(
            "Provider result instrument=%s fact=fundamentals status=%s source=%s fields=%d "
            "quality=%s attempts=%s fallback=%s",
            instrument.stable_key,
            fundamental_result.status.value,
            fundamental_result.selected_source or "none",
            fundamental_fields,
            getattr(getattr(fundamentals, "quality_status", None), "value", "unavailable"),
            _provider_attempt_trace(fundamental_result),
            fundamental_result.fallback_reason or "none",
        )
        logger.info(
            "Facts refreshed instrument=%s mode=%s bars=%d quote=%s news=%s fundamentals=%s "
            "quality=%s score=%.1f action=%s issues=%s duration_seconds=%.3f",
            instrument.stable_key,
            mode.value,
            len(bars),
            quote_result.status.value,
            news_result.status.value,
            fundamental_result.status.value,
            quality.status.value,
            quality.score,
            quality.action.value,
            _quality_issue_trace(quality),
            monotonic() - refresh_started,
        )
        return _Facts(
            instrument, metadata, listing_date, tuple(bars), quote, tuple(news),
            news_result.status, fundamentals, fundamental_result.status, quality,
        )

    def _technical_training_samples(self, facts, *, maximum_origins=_FORECAST_MAX_ORIGINS):
        """Build bounded point-in-time samples for cold-start baseline and OOF."""
        bars = tuple(facts.bars)
        if len(bars) < 31:
            return ()
        by_date = {bar.trading_date: bar for bar in bars}
        origin_indexes = tuple(range(20, len(bars) - 1))[-maximum_origins:]
        samples = []
        for index in origin_indexes:
            reference = bars[index]
            cutoff = datetime.combine(reference.trading_date, time(23, 59, 59), tzinfo=timezone.utc)
            prefix = bars[: index + 1]
            quality = evaluate_data_quality(
                prefix, market=facts.instrument.market, mode=DecisionMode.EOD,
                as_of=cutoff, news_available=False, fundamentals_available=False,
                listing_date=facts.listing_date, calendar=self.container.calendar,
            )
            snapshot = self.container.feature_builder.build(
                FeatureInputs(
                    facts.instrument, DecisionMode.EOD, cutoff, prefix, None, (),
                    ProviderStatus.EMPTY, None, ProviderStatus.EMPTY, quality,
                    FeatureEvidenceMode.RECONSTRUCTED_HISTORY,
                ),
                generated_at=cutoff,
            )
            for horizon in (1, 3, 5, 10):
                try:
                    target_date = self.container.calendar.target_dates(
                        facts.instrument.market, reference.trading_date, (horizon,),
                    )[horizon]
                except TradingCalendarUnavailable:
                    continue
                target = by_date.get(target_date)
                if target is None:
                    continue
                sample = build_training_sample(
                    calendar=self.container.calendar, feature_snapshot=snapshot,
                    reference_bar=reference, target_bar=target, horizon=horizon,
                )
                if sample is not None:
                    samples.append(sample)
        return tuple(samples)

    def _decision_session(self, instrument, mode, latest_bar_date, as_of):
        try:
            if mode is DecisionMode.INTRADAY:
                active = self.container.calendar.session_containing(instrument.market, instrument.exchange, as_of)
                if active is not None:
                    return active
            session_date = self.container.calendar.next_session(instrument.market, instrument.exchange, latest_bar_date)
            return self.container.calendar.session_window(instrument.market, instrument.exchange, session_date)
        except TradingCalendarUnavailable:
            return None

    def _valuation_price(self, instrument, facts, as_of):
        quote = facts.quote
        if quote is not None and quote.freshness_status is FreshnessStatus.FRESH:
            return ValuationPrice(instrument, Decimal(str(quote.price)), quote.observed_at, quote.source, ValuationPriceKind.FRESH_QUOTE, quote.freshness_status)
        if facts.bars:
            bar = facts.bars[-1]
            return ValuationPrice(instrument, Decimal(str(bar.close)), as_of, bar.source, ValuationPriceKind.REFERENCE_CLOSE, FreshnessStatus.NOT_REQUIRED)
        return None

    def _valuation(self, account, facts_by_instrument, as_of):
        prices = {}
        for position in account.positions:
            facts = facts_by_instrument.get(position.instrument)
            price = self._valuation_price(position.instrument, facts, as_of) if facts is not None else None
            if price is None:
                stored = self.container.repository.list_daily_bars(position.instrument, as_of.date() - timedelta(days=30), as_of.date())
                if stored:
                    price = ValuationPrice(position.instrument, Decimal(str(stored[-1].close)), as_of, stored[-1].source, ValuationPriceKind.REFERENCE_CLOSE, FreshnessStatus.NOT_REQUIRED)
            if price is not None:
                prices[position.instrument] = price
        valuation = freeze_account_valuation(account, prices, as_of, generated_at=as_of)
        self.container.repository.save_frozen_account_valuation(valuation)
        return valuation

    def _forecast_metric_snapshots(self, instrument, forecasts, as_of):
        """Project persisted sample-out diagnostics into source-closed report facts."""
        snapshots = []
        for forecast in forecasts:
            evaluations = tuple(
                item for item in self.container.repository.list_forecast_candidate_evaluations(
                    market=instrument.market,
                    scope_key=instrument.stable_key,
                    horizon=forecast.horizon,
                )
                if item["phase"] == "confirmation" and item["created_at"] <= as_of
            )
            if forecast.training_data_hash is not None:
                evaluations = tuple(
                    item for item in evaluations
                    if item["data_hash"] == forecast.training_data_hash
                )
            if not evaluations:
                continue
            latest_at = max(item["created_at"] for item in evaluations)
            latest = tuple(item for item in evaluations if item["created_at"] == latest_at)
            selected = None
            metric_side = "baseline"
            if forecast.execution_eligible:
                registered = self.container.forecast_registry.resolve(
                    market=instrument.market, stock_key=instrument.stable_key,
                    industry_key=None, horizon=forecast.horizon,
                )
                spec_id = registered.version.spec.spec_id if registered is not None else None
                selected = next((item for item in latest if item["spec_id"] == spec_id), None)
                metric_side = "candidate"
            selected = selected or latest[0]
            payload = selected["payload"][metric_side]
            metrics = (
                ("brier", float(payload["brier"])),
                ("direction_accuracy", float(payload["accuracy"])),
                ("ece", float(payload["ece"])),
                ("interval_hit_rate", float(payload["interval_coverage"])),
                ("log_loss", float(payload["log_loss"])),
            )
            scope_key = (
                f"{instrument.stable_key}:h{forecast.horizon}:"
                f"{'formal_model' if forecast.execution_eligible else 'empirical_baseline'}"
            )
            normalized_metrics = tuple(sorted(metrics))
            identity = {
                "ledger": LedgerKind.FORECAST, "scope": scope_key,
                "cutoff": latest_at, "sample_count": int(payload["sample_count"]),
                "metrics": normalized_metrics,
            }
            snapshot = LearningMetricSnapshot(
                stable_hash(identity), LedgerKind.FORECAST, scope_key, latest_at,
                int(payload["sample_count"]), normalized_metrics, as_of,
            )
            self.container.repository.save_learning_metric_snapshot(snapshot)
            snapshots.append(snapshot)
        return tuple(snapshots)

    def _build_instrument(self, facts, mode, history_period, as_of, account, valuation, callback=None):
        instrument = facts.instrument
        self._emit(callback, AnalysisStage.BUILD_FEATURES, instrument)
        origin_quality = evaluate_data_quality(
            facts.bars, market=instrument.market, mode=DecisionMode.EOD, as_of=as_of,
            news_available=bool(facts.news), fundamentals_available=facts.fundamentals is not None,
            listing_date=facts.listing_date, calendar=self.container.calendar,
        )
        origin_inputs = FeatureInputs(
            instrument, DecisionMode.EOD, as_of, facts.bars, None, facts.news,
            facts.news_status, facts.fundamentals, facts.fundamentals_status,
            origin_quality, FeatureEvidenceMode.RECONSTRUCTED_HISTORY,
        )
        current_inputs = FeatureInputs(
            instrument, mode, as_of, facts.bars, facts.quote, facts.news,
            facts.news_status, facts.fundamentals, facts.fundamentals_status,
            facts.quality, FeatureEvidenceMode.RECONSTRUCTED_HISTORY,
        )
        origin = self.container.feature_builder.build(origin_inputs, generated_at=as_of)
        current = origin if mode is DecisionMode.EOD else self.container.feature_builder.build(current_inputs, generated_at=as_of)
        self.container.repository.upsert_feature_snapshot(origin)
        if current.feature_hash != origin.feature_hash:
            self.container.repository.upsert_feature_snapshot(current)
        if not facts.bars or origin.latest_bar_date is None:
            raise RuntimeError(f"{instrument.code}: DAILY_BARS_UNAVAILABLE")

        self._emit(callback, AnalysisStage.FORECAST, instrument)
        training_samples = self._technical_training_samples(facts)
        forecasts = self.container.forecast_engine.forecast(
            ForecastRequest(origin, facts.bars[-1], as_of, data_quality=origin_quality),
            samples=training_samples,
        )
        for forecast in forecasts:
            self.container.repository.save_forecast_result(forecast)
        logger.info(
            "Forecast completed instrument=%s training_samples=%d results=%s",
            instrument.stable_key,
            len(training_samples),
            ";".join(
                f"h{item.horizon}:{item.availability.value}:{item.direction.value if item.direction else 'none'}:"
                f"{item.model_family.value}/{item.validation_status.value}:eligible={item.execution_eligible}:"
                f"samples={item.sample_count}/oof={item.oof_sample_count}"
                for item in forecasts
            ) or "none",
        )

        self._emit(callback, AnalysisStage.SCENARIO, instrument)
        session = self._decision_session(instrument, mode, origin.latest_bar_date, as_of)
        scenario = self.container.scenario_planner.build(
            ScenarioRequest(instrument, mode, as_of, origin, current, facts.quote, (), forecasts, facts.quality, session),
            generated_at=as_of,
        )
        self.container.repository.save_trading_scenario(scenario)
        logger.info(
            "Scenario completed instrument=%s mode=%s status=%s state=%s bias=%s "
            "forecast_support=%s entry_posture=%s exit_posture=%s reasons=%s",
            instrument.stable_key,
            mode.value,
            scenario.status.value,
            scenario.state.value,
            scenario.bias.value,
            scenario.forecast_support.value,
            scenario.entry_posture.value,
            scenario.exit_posture.value,
            ",".join(scenario.reason_codes) or "none",
        )

        self._emit(callback, AnalysisStage.STRATEGY, instrument)
        position = next((item for item in account.positions if item.instrument == instrument), None)
        strategy = self.container.strategy_engine.build(
            StrategyInput(instrument, current, scenario, position, default_specs(), "strategy_policy_v1", as_of),
            generated_at=as_of,
        )
        self.container.repository.save_strategy_bundle(strategy)
        plans = {
            plan.plan_id: plan
            for branch in (strategy.entry_or_add, strategy.reduce_or_exit, strategy.hold, strategy.invalidation)
            for plan in branch.plans
        }
        for plan in plans.values():
            self.container.repository.save_trade_plan(plan)
        logger.info(
            "Strategy completed instrument=%s position_state=%s plans=%d details=%s",
            instrument.stable_key,
            strategy.position_state.value,
            len(plans),
            ";".join(
                f"{plan.action.value}/{plan.readiness.value}/{plan.strategy_id}"
                for plan in plans.values()
            ),
        )

        historical_strategy_outcomes = self.container.repository.list_strategy_outcomes(instrument)
        learning_evidence = []
        for plan in plans.values():
            for raw_profile in plan.profiles:
                profile = RiskProfile(raw_profile.value)
                evidence = plan_evidence(
                    instrument=instrument,
                    strategy_id=plan.strategy_id,
                    strategy_version=plan.strategy_version,
                    parameter_hash=plan.parameter_hash,
                    profile=profile,
                    outcomes=historical_strategy_outcomes,
                    cutoff_at=as_of,
                    generated_at=as_of,
                    action=plan.action.value,
                )
                self.container.repository.save_plan_evidence_snapshot(evidence)
                learning_evidence.append(evidence)

        self._emit(callback, AnalysisStage.RISK, instrument)
        availability = None
        if position is not None:
            availability = PositionAvailability(
                instrument, position.shares,
                position.shares if instrument.market is Market.US else None,
                as_of,
                AvailabilitySource.USER if instrument.market is Market.US else AvailabilitySource.UNAVAILABLE,
                () if instrument.market is Market.US else ("RISK_POSITION_AVAILABILITY_UNKNOWN",),
            )
        rules = default_market_rules(instrument.market, instrument.exchange, as_of)
        risk = self.container.risk_officer.assess(
            RiskRequest(instrument, strategy, scenario, facts.quality, account, valuation,
                        availability, tuple(learning_evidence), rules, None, RiskPolicy(), as_of),
            generated_at=as_of,
        )
        self.container.repository.save_risk_decision_bundle(risk)
        for decision in risk.decisions:
            self.container.repository.save_execution_decision(decision)
        logger.info(
            "Risk completed instrument=%s decisions=%d details=%s",
            instrument.stable_key,
            len(risk.decisions),
            ";".join(
                f"{item.profile.value}/{item.action.value}/{item.level.value}/"
                f"{item.disposition.value}/shares={item.approved_shares}"
                for item in risk.decisions
            ),
        )

        self._emit(callback, AnalysisStage.EXECUTION_PREVIEW, instrument)
        orders = self.container.order_intent_factory.build_bundle(
            risk, plans, {}, as_of, ExecutionPolicy(), decision_mode=mode,
        )
        for intent in orders.intents:
            self.container.repository.save_order_intent(intent)
        for record in orders.records:
            self.container.repository.save_order_intent_build_record(record)
        logger.info(
            "Execution preview completed instrument=%s intents=%d records=%d record_statuses=%s",
            instrument.stable_key,
            len(orders.intents),
            len(orders.records),
            ",".join(item.status.value for item in orders.records) or "none",
        )

        forecast_metrics = self._forecast_metric_snapshots(instrument, forecasts, as_of)
        metric_loader = getattr(
            self.container.repository, "list_latest_learning_metric_snapshots", None,
        )
        persisted_metrics = (
            metric_loader(instrument.stable_key + ":") if metric_loader is not None else ()
        )
        metrics_by_scope = {
            (item.ledger_kind, item.scope_key): item
            for item in (*persisted_metrics, *forecast_metrics)
        }
        presentation = single_stock_input(
            instrument=instrument, analysis_mode=mode, as_of=as_of,
            history_period=history_period, metadata=facts.metadata,
            quote_snapshot=facts.quote, data_quality=facts.quality,
            feature_snapshot=current, forecasts=forecasts, scenario=scenario,
            strategy_bundle=strategy, risk_bundle=risk,
            order_intent_bundle=orders,
            forecast_outcomes=self.container.repository.list_forecast_outcomes(instrument),
            strategy_outcomes=historical_strategy_outcomes,
            # 原始联合回放是市场级审计事实；单股报告只消费按股票冻结的汇总，
            # 避免把同市场其他股票的回放误归因到当前股票。
            joint_outcomes=(),
            learning_evidence=tuple(learning_evidence),
            metric_snapshots=tuple(sorted(
                metrics_by_scope.values(),
                key=lambda item: (item.ledger_kind.value, item.scope_key),
            )),
            news_summary=facts.news, fundamental_summary=facts.fundamentals,
            built_at=self._presentation_built_at(facts, as_of),
        )
        if self.container.background_learning is not None:
            self.container.background_learning.submit(instrument,facts.bars,listing_date=facts.listing_date)
        if getattr(self.container, "background_forecast_training", None) is not None and training_samples:
            self.container.background_forecast_training.submit(instrument, training_samples)
        if getattr(self.container, "background_strategy_replay", None) is not None and training_samples:
            self.container.background_strategy_replay.submit(
                instrument, facts.bars, training_samples, listing_date=facts.listing_date,
            )
        return _InstrumentAnalysis(presentation, scenario, strategy, risk, plans, facts.bars)

    def single_stock(self, command, *, on_progress=None):
        self._emit(on_progress, AnalysisStage.RESOLVE_SUBJECT, command.instrument)
        account = self.container.repository.get_latest_account_snapshot(command.instrument.market)
        if account is None:
            raise ValueError("真实账户快照缺失，不能生成可执行分析")
        facts = self._facts(command.instrument, command.mode, command.history_period, command.requested_at, on_progress)
        analysis_at = self._decision_cutoff(command.requested_at, (facts,))
        facts = self._align_facts_quality(facts, command.mode, command.history_period, command.requested_at, analysis_at)
        valuation = self._valuation(account, {command.instrument: facts}, analysis_at)
        analysis = self._build_instrument(facts, command.mode, command.history_period, analysis_at, account, valuation, on_progress)
        self._emit(on_progress, AnalysisStage.BUILD_REPORT, command.instrument)
        document=SingleStockReportBuilder().build(analysis.presentation)
        with self._research_lock:
            self._research_inputs[document.report_id]=analysis.presentation
        return document

    def schedule_research(self, document):
        settings=self.container.settings
        if (document.report_kind.value not in {"single_stock", "portfolio"} or self.container.background_research is None or
                self.container.research_engine is None or not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model):
            return None
        return self.container.background_research.submit(document,self._research_revision)

    def _research_revision(self, base_document):
        with self._research_lock:
            value=self._research_inputs.pop(base_document.report_id,None)
        if value is None: return None
        if base_document.report_kind.value == "portfolio":
            return self._portfolio_research_revision(base_document, value)
        builder=ResearchContextBuilder(); now=datetime.now(timezone.utc)
        facts=builder.project_upstream_facts(
            feature_snapshots=(value.feature_snapshot,),news_snapshots=value.news_summary,
            fundamental_snapshots=(value.fundamental_summary,) if value.fundamental_summary else (),
            forecasts=value.forecasts,scenarios=(value.scenario,),strategy_bundles=(value.strategy_bundle,),
            risk_bundles=(value.risk_bundle,),learning_snapshots=value.metric_snapshots,
        )
        manifest=builder.build_manifest(scope=ResearchScope.SINGLE_STOCK,market=value.instrument.market,
            cutoff_at=value.as_of,instruments=(value.instrument,),facts=facts,generated_at=now)
        context=builder.build_context(scope=ResearchScope.SINGLE_STOCK,market=value.instrument.market,
            mode=value.analysis_mode.value,cutoff_at=value.as_of,manifest=manifest,
            instrument_roles=((value.instrument,"subject"),),
            forecast_event_keys=tuple(item.event_key for item in value.forecasts),
            scenario_ids=(value.scenario.scenario_id,),strategy_bundle_ids=(value.strategy_bundle.bundle_id,),
            risk_bundle_ids=(value.risk_bundle.risk_bundle_id,),generated_at=now)
        prompt,prompt_hash=build_prompt(context)
        request_id=stable_hash({"context":context.context_id,"prompt":prompt_hash,"model":self.container.settings.llm_model})
        capabilities=capabilities_for_endpoint(self.container.settings.llm_base_url)
        thinking_enabled=getattr(self.container.settings,"llm_enable_thinking",False)
        request=LLMResearchRequest.for_capabilities(capabilities=capabilities,request_id=request_id,
            context_id=context.context_id,prompt_version=PROMPT_VERSION,prompt_hash=prompt_hash,
            json_schema_version=1,provider_name="configured",model_name=self.container.settings.llm_model,
            requested_at=now,instrument_keys=(value.instrument.stable_key,),
            thinking_enabled=thinking_enabled,max_output_tokens=output_token_budget(thinking_enabled))
        client=OpenAICompatibleResearchClient(endpoint=self.container.settings.llm_base_url,
            api_key=self.container.settings.llm_api_key,prompts={request_id:prompt},capabilities=capabilities)
        result=self.container.research_engine.run(context=context,request=request,client=client,
            market=value.instrument.market,scope_key=value.instrument.stable_key,
            base_version="research_candidate_v1",search_space_hash=stable_hash(default_specs()),
            allowed_instrument_keys=(value.instrument.stable_key,))
        response=result.get("response")
        if response is None: return None
        self.container.repository.save_research_result(context,response,result["hypotheses"],result["validations"],result["links"],candidates=result["candidates"])
        payload={item.name:getattr(value,item.name) for item in fields(value) if item.name not in {"presentation_id","source_artifact_refs"}}
        payload.update(research_hypotheses=result["hypotheses"],research_validations=result["validations"],built_at=datetime.now(timezone.utc))
        revised=single_stock_input(**payload)
        builder=SingleStockReportBuilder()
        revised_document=builder.build(revised)
        editorial=edit_report(revised_document,self.container.settings)
        if not result["hypotheses"] and editorial is None: return None
        return builder.build(revised,editorial)

    def _portfolio_research_revision(self, base_document, value):
        builder=ResearchContextBuilder(); now=datetime.now(timezone.utc)
        members=value.instruments
        learning=tuple(item for member in members for item in member.metric_snapshots) + value.portfolio_learning_evidence
        facts=builder.project_upstream_facts(
            feature_snapshots=tuple(item.feature_snapshot for item in members),
            news_snapshots=tuple(news for item in members for news in item.news_summary),
            fundamental_snapshots=tuple(item.fundamental_summary for item in members if item.fundamental_summary),
            forecasts=tuple(forecast for item in members for forecast in item.forecasts),
            scenarios=tuple(item.scenario for item in members),
            strategy_bundles=tuple(item.strategy_bundle for item in members),
            risk_bundles=tuple(item.risk_bundle for item in members),
            portfolio_bundles=(value.portfolio_decision_bundle,),
            learning_snapshots=learning,
        )
        instruments=tuple(item.instrument for item in members)
        held={item.instrument for item in value.account_snapshot.positions}
        roles=tuple((item, "holding" if item in held else "watchlist") for item in instruments)
        manifest=builder.build_manifest(
            scope=ResearchScope.PORTFOLIO,market=value.market,cutoff_at=value.as_of,
            instruments=instruments,facts=facts,generated_at=now,
        )
        context=builder.build_context(
            scope=ResearchScope.PORTFOLIO,market=value.market,mode=value.analysis_mode.value,
            cutoff_at=value.as_of,manifest=manifest,instrument_roles=roles,
            forecast_event_keys=tuple(item.event_key for member in members for item in member.forecasts),
            scenario_ids=tuple(item.scenario.scenario_id for item in members),
            strategy_bundle_ids=tuple(item.strategy_bundle.bundle_id for item in members),
            risk_bundle_ids=tuple(item.risk_bundle.risk_bundle_id for item in members),
            portfolio_bundle_id=value.portfolio_decision_bundle.portfolio_bundle_id,
            learning_snapshot_ids=tuple(item.snapshot_id for item in learning),generated_at=now,
        )
        capabilities=capabilities_for_endpoint(self.container.settings.llm_base_url)
        thinking_enabled=getattr(self.container.settings,"llm_enable_thinking",False)
        invocations=[]
        for keys,prompt,prompt_hash in build_prompt_chunks(context):
            request_id=stable_hash({"context":context.context_id,"prompt":prompt_hash,"model":self.container.settings.llm_model,"instruments":keys})
            request=LLMResearchRequest.for_capabilities(
                capabilities=capabilities,request_id=request_id,context_id=context.context_id,
                prompt_version=PROMPT_VERSION,prompt_hash=prompt_hash,json_schema_version=1,
                provider_name="configured",model_name=self.container.settings.llm_model,
                requested_at=now,instrument_keys=keys,
                thinking_enabled=thinking_enabled,max_output_tokens=output_token_budget(thinking_enabled),
            )
            client=OpenAICompatibleResearchClient(
                endpoint=self.container.settings.llm_base_url,api_key=self.container.settings.llm_api_key,
                prompts={request_id:prompt},capabilities=capabilities,
            )
            invocations.append((keys,request,client))
        result=self.container.research_engine.run_chunks(
            context=context,invocations=tuple(invocations),market=value.market,
            scope_key=f"{value.market.value}:portfolio",base_version="research_candidate_v1",
            search_space_hash=stable_hash(default_specs()),
        )
        hypotheses=result["hypotheses"]; validations=result["validations"]; links=result["links"]
        for response in result["responses"]:
            response_hypotheses=tuple(item for item in hypotheses if item.response_id==response.response_id)
            hypothesis_ids={item.hypothesis_id for item in response_hypotheses}
            response_validations=tuple(item for item in validations if item.hypothesis_id in hypothesis_ids)
            response_links=tuple(item for item in links if item.hypothesis_id in hypothesis_ids)
            candidate_ids={item.candidate_id for item in response_links if item.candidate_id}
            response_candidates=tuple(item for item in result["candidates"] if item.candidate_id in candidate_ids)
            self.container.repository.save_research_result(
                context,response,response_hypotheses,response_validations,response_links,candidates=response_candidates,
            )
        validation_by_hypothesis={item.hypothesis_id:item for item in validations}
        revised_members=[]
        completed_at=datetime.now(timezone.utc)
        for member in members:
            member_hypotheses=tuple(item for item in hypotheses if item.instrument==member.instrument)
            member_validations=tuple(validation_by_hypothesis[item.hypothesis_id] for item in member_hypotheses)
            payload={item.name:getattr(member,item.name) for item in fields(member) if item.name not in {"presentation_id","source_artifact_refs"}}
            payload.update(
                research_hypotheses=tuple((*member.research_hypotheses,*member_hypotheses)),
                research_validations=tuple((*member.research_validations,*member_validations)),
                built_at=max(completed_at,member.built_at),
            )
            revised_members.append(single_stock_input(**payload))
        portfolio_hypotheses=tuple(item for item in hypotheses if item.instrument is None)
        portfolio_validations=tuple(validation_by_hypothesis[item.hypothesis_id] for item in portfolio_hypotheses)
        payload={item.name:getattr(value,item.name) for item in fields(value) if item.name not in {"presentation_id","source_artifact_refs"}}
        payload.update(
            instruments=tuple(revised_members),
            portfolio_research_hypotheses=tuple((*value.portfolio_research_hypotheses,*portfolio_hypotheses)),
            portfolio_research_validations=tuple((*value.portfolio_research_validations,*portfolio_validations)),
            research_status=result["status"],
            research_chunk_count=result["attempted_chunks"],
            research_completed_chunk_count=result["completed_chunks"],
            research_failure_reasons=result["failure_reasons"],
            built_at=max(completed_at,value.built_at),
        )
        revised=portfolio_input(**payload)
        builder=PortfolioReportBuilder()
        revised_document=builder.build(revised)
        editorial=edit_report(revised_document,self.container.settings)
        return builder.build(revised,editorial)

    def portfolio(self, command, *, on_progress=None):
        account = self.container.repository.get_latest_account_snapshot(command.market)
        if account is None:
            raise ValueError("真实账户快照缺失，不能生成组合分析")
        watch = self.container.repository.latest_watchlist_snapshot(command.market)
        universe = tuple(dict.fromkeys((*[item.instrument for item in account.positions], *(watch.instruments if watch else ()))))
        if not universe:
            raise ValueError("组合没有持仓或关注股票")
        facts_by_instrument = {}
        failures = []
        for instrument in universe:
            try:
                facts_by_instrument[instrument] = self._facts(instrument, command.mode, command.history_period, command.requested_at, on_progress)
            except Exception as exc:
                failures.append(f"{instrument.code}:{type(exc).__name__}")
                logger.exception(
                    "Portfolio fact refresh failed instrument=%s mode=%s error_type=%s error=%s",
                    instrument.stable_key, command.mode.value, type(exc).__name__, exc,
                )
        analysis_at = self._decision_cutoff(command.requested_at, facts_by_instrument.values())
        facts_by_instrument = {
            instrument: self._align_facts_quality(
                facts, command.mode, command.history_period, command.requested_at, analysis_at,
            )
            for instrument, facts in facts_by_instrument.items()
        }
        logger.info(
            "Portfolio facts frozen market=%s requested_at=%s analysis_cutoff=%s instruments=%d failures=%d",
            command.market.value,
            command.requested_at.isoformat(),
            analysis_at.isoformat(),
            len(facts_by_instrument),
            len(failures),
        )
        valuation = self._valuation(account, facts_by_instrument, analysis_at)
        analyses = []
        for instrument in universe:
            facts = facts_by_instrument.get(instrument)
            if facts is None:
                continue
            try:
                analyses.append(self._build_instrument(facts, command.mode, command.history_period, analysis_at, account, valuation, on_progress))
            except Exception as exc:
                failures.append(f"{instrument.code}:{type(exc).__name__}")
                logger.exception(
                    "Portfolio instrument build failed instrument=%s mode=%s error_type=%s error=%s",
                    instrument.stable_key, command.mode.value, type(exc).__name__, exc,
                )
        if not analyses:
            detail = ",".join(failures[:8])
            raise RuntimeError("PORTFOLIO_ALL_INSTRUMENTS_UNAVAILABLE" + (f":" + detail if detail else ""))

        self._emit(on_progress, AnalysisStage.PORTFOLIO_ALLOCATION, None)
        position_instruments = {item.instrument for item in account.positions}
        analyzed_instruments = {item.presentation.instrument for item in analyses}
        missing_holdings = tuple(sorted(
            (item.code for item in position_instruments - analyzed_instruments),
        ))
        if missing_holdings:
            raise RuntimeError(
                "PORTFOLIO_HOLDING_ANALYSIS_UNAVAILABLE:" + ",".join(missing_holdings)
            )
        candidates = []
        rules_by_instrument = {}
        for analysis in analyses:
            instrument = analysis.presentation.instrument
            role = PortfolioRole.HOLDING if instrument in position_instruments else PortfolioRole.WATCHLIST
            rules = default_market_rules(instrument.market, instrument.exchange, analysis_at)
            rules_by_instrument[instrument] = rules
            evidence_by_plan_profile = {
                (item.strategy_id, item.strategy_version, item.parameter_hash, item.profile): item
                for item in analysis.presentation.learning_evidence
            }
            for decision in analysis.risk_bundle.decisions:
                plan = analysis.plans[decision.plan_id]
                evidence = evidence_by_plan_profile.get((
                    plan.strategy_id, plan.strategy_version, plan.parameter_hash, decision.profile,
                ))
                identity = {"role": role, "scenario_id": analysis.scenario.scenario_id,
                            "plan_id": plan.plan_id, "decision_id": decision.decision_id,
                            "evidence_id": evidence.evidence_id if evidence else None,
                            "rule_version": rules.rule_version}
                candidates.append(PortfolioCandidate(
                    stable_hash(identity), role, analysis.scenario, plan, decision,
                    evidence, rules, analysis_at,
                ))
        portfolio_policy = PortfolioPolicy()
        risk_policy = RiskPolicy()
        bars_by_instrument = {item.presentation.instrument: item.bars for item in analyses}
        correlation = build_correlation_snapshot(
            market=command.market, universe=tuple(item.presentation.instrument for item in analyses),
            bars_by_instrument=bars_by_instrument, policy=portfolio_policy,
            cutoff_at=analysis_at, source_batch_hash=stable_hash(bars_by_instrument),
            generated_at=analysis_at,
        )
        risk_bundles = tuple(sorted((item.risk_bundle for item in analyses), key=lambda item: item.risk_bundle_id))
        candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
        holding_risks = build_holding_risks(
            valuation=valuation, account=account, candidates=candidates,
            risk_bundles=risk_bundles, captured_at=analysis_at,
            generated_at=analysis_at,
        )
        # A failed watch item is omitted from this immutable decision batch and can
        # be retried later. A failed holding is blocked above because allocating
        # around an unvalued owned position would understate portfolio risk.
        watch_instruments = tuple(
            item for item in universe
            if item not in position_instruments and item in analyzed_instruments
        )
        batch_identity = {
            "market": command.market, "currency": account.currency, "mode": command.mode,
            "account_hash": stable_hash(account), "valuation_id": valuation.valuation_id,
            "risk_policy": risk_policy.parameter_hash, "portfolio_policy": portfolio_policy.parameter_hash,
            "bundle_ids": tuple(item.risk_bundle_id for item in risk_bundles),
            "candidate_ids": tuple(item.candidate_id for item in candidates), "watchlist": watch_instruments,
            "holding_risk_ids": tuple(item.holding_risk_id for item in holding_risks),
            "correlation_snapshot_id": correlation.correlation_snapshot_id, "as_of": analysis_at,
        }
        batch = PortfolioInputBatch(
            stable_hash(batch_identity), command.market, account.currency, command.mode,
            account, valuation, risk_policy, portfolio_policy, risk_bundles,
            candidates, watch_instruments, holding_risks, correlation,
            analysis_at, analysis_at,
        )
        decision = self.container.portfolio_engine.decide(batch, analysis_at)
        self.container.repository.save_portfolio_result(batch, decision)
        presentation = portfolio_input(
            market=command.market, analysis_mode=command.mode, as_of=analysis_at,
            history_period=command.history_period, account_snapshot=account,
            frozen_account_valuation=valuation, portfolio_decision_bundle=decision,
            instruments=tuple(item.presentation for item in analyses),
            watchlist_snapshot=watch,
            research_status=(ResearchRunStatus.PENDING if (
                self.container.background_research is not None and self.container.research_engine is not None and
                self.container.settings.llm_base_url and self.container.settings.llm_api_key and self.container.settings.llm_model
            ) else ResearchRunStatus.UNAVAILABLE),
            research_chunk_count=((len(analyses) + 9) // 10 if (
                self.container.background_research is not None and self.container.research_engine is not None and
                self.container.settings.llm_base_url and self.container.settings.llm_api_key and self.container.settings.llm_model
            ) else 0),
            built_at=max(datetime.now(timezone.utc), analysis_at, *(item.presentation.built_at for item in analyses)),
        )
        self._emit(on_progress, AnalysisStage.BUILD_REPORT, None)
        document=PortfolioReportBuilder().build(presentation)
        with self._research_lock:
            self._research_inputs[document.report_id]=presentation
        return document


@dataclass(slots=True)
class AnalysisApplication:
    repository: object
    pipeline: object | None = None
    executor: object | None = None
    clock: Callable = lambda: datetime.now(timezone.utc)
    _tasks: AnalysisTaskCoordinator = field(init=False, repr=False)
    _futures: dict = field(init=False, repr=False)
    _lock: Lock = field(init=False, repr=False)

    def __post_init__(self):
        self._tasks = AnalysisTaskCoordinator(self.clock)
        self._futures = {}
        self._lock = Lock()

    @staticmethod
    def _notify(callback, *args):
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            # UI observers are outside the deterministic transaction boundary.
            # Their failure must not change or erase an already valid analysis.
            return

    def _require_account(self, market):
        account = self.repository.get_latest_account_snapshot(market)
        if account is None:
            raise ValueError("真实账户快照缺失，不能生成可执行分析")
        return account

    def _single_command(self, values):
        if isinstance(values, SingleStockAnalysisCommand):
            return values
        market = Market(str(values.get("market", "US")).upper())
        instrument = values.get("instrument")
        if instrument is None:
            provisional=InstrumentId.from_code(values["symbol"], market)
            matches=self.repository.search_stock_metadata(market,provisional.code,limit=5)
            instrument=next((item.instrument for item in matches if item.instrument.code==provisional.code),provisional)
        account = self._require_account(market)
        now = self.clock(); mode = DecisionMode(str(values.get("mode", "eod"))); history = str(values.get("history_period", "1y"))
        account_id = stable_hash(account)
        identity = {"instrument": instrument, "mode": mode, "history": history, "requested_at": now, "account": account_id, "force_refresh": bool(values.get("force_refresh", False))}
        return SingleStockAnalysisCommand(stable_hash(identity), instrument, mode, history, now, account_id, bool(values.get("force_refresh", False)))

    def _portfolio_command(self, values):
        if isinstance(values, PortfolioAnalysisCommand):
            return values
        account_value = values.get("account")
        market = account_value.market if account_value is not None else Market(str(values.get("market", "US")).upper())
        account = self._require_account(market)
        watch = values.get("watchlist") or self.repository.latest_watchlist_snapshot(market)
        now = self.clock(); mode = DecisionMode(str(values.get("mode", "eod"))); history = str(values.get("history_period", "1y"))
        account_id = stable_hash(account); watch_id = getattr(watch, "watchlist_id", None)
        identity = {"market": market, "mode": mode, "history": history, "requested_at": now, "account": account_id, "watchlist": watch_id, "force_refresh": bool(values.get("force_refresh", False))}
        return PortfolioAnalysisCommand(stable_hash(identity), market, mode, history, now, account_id, watch_id, bool(values.get("force_refresh", False)))

    def _execute(self, kind, command, on_progress, on_complete, on_error):
        started_at = monotonic()
        run_id=stable_hash({"command":command.command_id})
        instrument = getattr(command, "instrument", None)
        market = instrument.market if instrument is not None else command.market
        logger.info(
            "Analysis started kind=%s command_id=%s market=%s instrument=%s mode=%s history=%s",
            kind,
            command.command_id,
            market.value,
            getattr(instrument, "stable_key", None),
            command.mode.value,
            command.history_period,
        )
        existing=self.repository.get_analysis_run(run_id)
        if existing is not None and existing.status is AnalysisRunStatus.COMPLETED:
            document=self.repository.get_report_document(existing.deterministic_report_id)
            if document is not None:
                logger.info("Analysis cache hit kind=%s command_id=%s report_id=%s", kind, command.command_id, document.report_id)
                self._notify(on_complete,document)
                return document
        if kind == "single_stock":
            total = 13
        else:
            account=self.repository.get_latest_account_snapshot(command.market)
            watch=self.repository.latest_watchlist_snapshot(command.market)
            subjects=len({*(item.instrument for item in (account.positions if account else ())), *(watch.instruments if watch else ())})
            total=max(13,subjects * 9 + 5)
        initial=self._tasks.start(command.command_id, total_units=total, instrument=getattr(command, "instrument", None))
        self._notify(on_progress,initial)
        completed = 0
        current_stage=AnalysisStage.VALIDATE_INPUT
        stage_order={value:index for index,value in enumerate(AnalysisStage)}
        last_progress_key = None
        last_progress_at = monotonic()
        def progress(stage, instrument, message):
            nonlocal completed,current_stage,last_progress_key,last_progress_at
            if self._tasks.is_cancelled(command.command_id):
                raise RuntimeError("ANALYSIS_CANCELLED")
            completed = min(completed + 1, total - 1)
            if stage_order[stage] >= stage_order[current_stage]:
                current_stage=stage
            progress_key = (stage, getattr(instrument, "stable_key", None))
            now_tick = monotonic()
            if progress_key != last_progress_key:
                logger.info(
                    "Analysis progress kind=%s command_id=%s mode=%s stage=%s instrument=%s "
                    "completed=%d total=%d previous_step_seconds=%.3f message=%s",
                    kind,
                    command.command_id,
                    command.mode.value,
                    stage.value,
                    getattr(instrument, "stable_key", None),
                    completed,
                    total,
                    now_tick - last_progress_at,
                    message,
                )
                last_progress_key = progress_key
                last_progress_at = now_tick
            value = self._tasks.emit(command.command_id, current_stage, TaskStatus.RUNNING, completed, total, instrument, message)
            self._notify(on_progress,value)
        try:
            document = getattr(self.pipeline, kind)(command, on_progress=progress)
            value = self._tasks.emit(command.command_id, AnalysisStage.PERSIST_REPORT, TaskStatus.RUNNING, total - 1, total, getattr(command, "instrument", None), "persist_report")
            self._notify(on_progress,value)
            if self._tasks.is_cancelled(command.command_id):
                result=AnalysisRunResult(run_id,command.command_id,AnalysisRunStatus.CANCELLED,None,None,(),(),("ANALYSIS_CANCELLED",),command.requested_at,self.clock())
                self.repository.save_analysis_run(result,report_kind="single_stock" if kind=="single_stock" else "portfolio",market=(command.instrument.market if kind=="single_stock" else command.market).value,instrument_key=command.instrument.stable_key if kind=="single_stock" else None,mode=command.mode.value,history_period=command.history_period)
                return None
            self.repository.save_report_document(document)
            schedule=getattr(self.pipeline,"schedule_research",None)
            background_ids=()
            if schedule is not None:
                task_id=schedule(document)
                background_ids=(task_id,) if task_id else ()
            result = AnalysisRunResult(run_id, command.command_id, AnalysisRunStatus.COMPLETED, document.report_id, None, background_ids, document.source_artifact_refs, (), command.requested_at, self.clock())
            self.repository.save_analysis_run(result, report_kind="single_stock" if kind == "single_stock" else "portfolio", market=(command.instrument.market if kind == "single_stock" else command.market).value, instrument_key=command.instrument.stable_key if kind == "single_stock" else None, mode=command.mode.value, history_period=command.history_period)
            value = self._tasks.emit(command.command_id, AnalysisStage.COMPLETED, TaskStatus.COMPLETED, total, total, getattr(command, "instrument", None), "completed", cancellable=False)
            self._notify(on_progress,value)
            self._notify(on_complete,document)
            if background_ids and getattr(self.pipeline.container, "background_research", None) is not None:
                def research_completed(task_result):
                    report_id=getattr(task_result,"research_report_id",None)
                    if getattr(task_result,"status",None)!="completed" or not report_id:
                        return
                    revised=self.repository.get_report_document(report_id)
                    if revised is not None:
                        self._notify(on_complete,revised)
                self.pipeline.container.background_research.add_done_callback(background_ids[0], research_completed)
            logger.info(
                "Analysis completed kind=%s command_id=%s report_id=%s duration_seconds=%.3f background_tasks=%d",
                kind,
                command.command_id,
                document.report_id,
                monotonic() - started_at,
                len(background_ids),
            )
            return document
        except Exception as exc:
            cancelled=self._tasks.is_cancelled(command.command_id)
            status=AnalysisRunStatus.CANCELLED if cancelled else AnalysisRunStatus.FAILED
            task_status=TaskStatus.CANCELLED if cancelled else TaskStatus.FAILED
            reason="ANALYSIS_CANCELLED" if cancelled else "ANALYSIS_FAILED"
            if not cancelled:
                value=self._tasks.emit(command.command_id,current_stage,task_status,completed,total,getattr(command,"instrument",None),reason,cancellable=False)
                self._notify(on_progress,value)
            result=AnalysisRunResult(run_id,command.command_id,status,None,None,(),(),(reason,),command.requested_at,self.clock())
            self.repository.save_analysis_run(result,report_kind="single_stock" if kind=="single_stock" else "portfolio",market=(command.instrument.market if kind=="single_stock" else command.market).value,instrument_key=command.instrument.stable_key if kind=="single_stock" else None,mode=command.mode.value,history_period=command.history_period)
            if cancelled:
                logger.info("Analysis cancelled kind=%s command_id=%s stage=%s", kind, command.command_id, current_stage.value)
            else:
                logger.exception(
                    "Analysis failed kind=%s command_id=%s market=%s mode=%s stage=%s "
                    "duration_seconds=%.3f error_type=%s error=%s",
                    kind,
                    command.command_id,
                    market.value,
                    command.mode.value,
                    current_stage.value,
                    monotonic() - started_at,
                    type(exc).__name__,
                    exc,
                )
            self._notify(on_error,exc)
            raise

    def _start(self, kind, command, on_progress, on_complete, on_error):
        if self.pipeline is None:
            raise RuntimeError("V2 analysis engines are not configured")
        if self.executor is None:
            return self._execute(kind, command, on_progress, on_complete, on_error)
        future = self.executor.submit(self._execute, kind, command, on_progress, on_complete, on_error)
        with self._lock:
            self._futures[command.command_id] = future
        return command.command_id

    def start_single(self, values, *, on_progress=None, on_complete=None, on_error=None):
        return self._start("single_stock", self._single_command(values), on_progress, on_complete, on_error)

    def start_portfolio(self, values, *, on_progress=None, on_complete=None, on_error=None):
        return self._start("portfolio", self._portfolio_command(values), on_progress, on_complete, on_error)

    def cancel(self, task_id):
        with self._lock:
            future = self._futures.get(task_id)
        if future is None or future.done():
            return False
        self._tasks.cancel(task_id)
        future.cancel()
        return True
