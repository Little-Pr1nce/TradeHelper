"""Production orchestration for Tab1 and Tab3.

The application layer coordinates frozen V2 contracts.  It never calculates a
technical indicator itself and never turns missing facts into executable advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Callable, Mapping

from tradehelper_v2.contracts import (
    AnalysisRunResult, AnalysisRunStatus, AnalysisStage, AvailabilitySource,
    DecisionMode, ExecutionPolicy, FeatureEvidenceMode, FeatureInputs,
    FreshnessStatus, InstrumentId, Market, PortfolioCandidate,
    PortfolioInputBatch, PortfolioPolicy, PortfolioRole, PositionAvailability,
    ProviderStatus, RiskPolicy, RiskRequest, SingleStockAnalysisCommand,
    PortfolioAnalysisCommand, ResearchScope, StockMetadata, StrategyInput, TaskStatus,
    ValuationPrice, ValuationPriceKind, stable_hash,
)
from tradehelper_v2.contracts.forecast import ForecastRequest
from tradehelper_v2.contracts.scenario import ScenarioRequest
from tradehelper_v2.data.calendar import TradingCalendarUnavailable
from tradehelper_v2.data.quality import evaluate_data_quality
from tradehelper_v2.execution import OrderIntentFactory
from tradehelper_v2.portfolio.evidence import (
    build_correlation_snapshot, build_holding_risks,
)
from tradehelper_v2.presentation.inputs import portfolio_input, single_stock_input
from tradehelper_v2.presentation.report_builder import PortfolioReportBuilder, SingleStockReportBuilder
from tradehelper_v2.risk import freeze_account_valuation
from tradehelper_v2.risk.market_rules import default_market_rules
from tradehelper_v2.strategies.registry import default_specs
from tradehelper_v2.application.tasks import AnalysisTaskCoordinator
from tradehelper_v2.research.client import LLMResearchRequest, OpenAICompatibleResearchClient, ResearchClientCapabilities
from tradehelper_v2.research.context import ResearchContextBuilder
from tradehelper_v2.research.prompt import PROMPT_VERSION, build_prompt


_HISTORY_DAYS = {"1m": 45, "3m": 140, "6m": 280, "1y": 540}


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

    def _facts(self, instrument, mode, history_period, as_of, callback=None):
        data = self.container.data_refresh
        self._emit(callback, AnalysisStage.REFRESH_METADATA, instrument)
        metadata_result = data.refresh_metadata(instrument, as_of)
        listing_result = data.refresh_listing_date(instrument, as_of)
        metadata = metadata_result.value if metadata_result.status is ProviderStatus.OK else self.container.repository.get_stock_metadata(instrument)
        listing_date = listing_result.value if listing_result.status is ProviderStatus.OK else getattr(metadata, "listing_date", None)
        if metadata is None:
            metadata = StockMetadata(instrument, instrument.code, None, None, listing_date, "instrument_identity", as_of)

        self._emit(callback, AnalysisStage.REFRESH_MARKET_DATA, instrument)
        requested_start = as_of.date() - timedelta(days=_HISTORY_DAYS.get(history_period, 140))
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
        return _Facts(
            instrument, metadata, listing_date, tuple(bars), quote, tuple(news),
            news_result.status, fundamentals, fundamental_result.status, quality,
        )

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
        forecasts = self.container.forecast_engine.forecast(ForecastRequest(origin, facts.bars[-1], as_of, data_quality=origin_quality))
        for forecast in forecasts:
            self.container.repository.save_forecast_result(forecast)

        self._emit(callback, AnalysisStage.SCENARIO, instrument)
        session = self._decision_session(instrument, mode, origin.latest_bar_date, as_of)
        scenario = self.container.scenario_planner.build(
            ScenarioRequest(instrument, mode, as_of, origin, current, facts.quote, (), forecasts, facts.quality, session),
            generated_at=as_of,
        )
        self.container.repository.save_trading_scenario(scenario)

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
                        availability, (), rules, None, RiskPolicy(), as_of),
            generated_at=as_of,
        )
        self.container.repository.save_risk_decision_bundle(risk)
        for decision in risk.decisions:
            self.container.repository.save_execution_decision(decision)

        self._emit(callback, AnalysisStage.EXECUTION_PREVIEW, instrument)
        orders = self.container.order_intent_factory.build_bundle(
            risk, plans, {}, as_of, ExecutionPolicy(), decision_mode=mode,
        )
        for intent in orders.intents:
            self.container.repository.save_order_intent(intent)
        for record in orders.records:
            self.container.repository.save_order_intent_build_record(record)

        presentation = single_stock_input(
            instrument=instrument, analysis_mode=mode, as_of=as_of,
            history_period=history_period, metadata=facts.metadata,
            quote_snapshot=facts.quote, data_quality=facts.quality,
            feature_snapshot=current, forecasts=forecasts, scenario=scenario,
            strategy_bundle=strategy, risk_bundle=risk,
            order_intent_bundle=orders,
            forecast_outcomes=self.container.repository.list_forecast_outcomes(instrument),
            strategy_outcomes=self.container.repository.list_strategy_outcomes(instrument),
            joint_outcomes=self.container.repository.list_joint_outcomes(instrument.market),
            news_summary=facts.news, fundamental_summary=facts.fundamentals,
            built_at=datetime.now(timezone.utc),
        )
        if self.container.background_learning is not None:
            self.container.background_learning.submit(instrument,facts.bars,listing_date=facts.listing_date)
        return _InstrumentAnalysis(presentation, scenario, strategy, risk, plans, facts.bars)

    def single_stock(self, command, *, on_progress=None):
        self._emit(on_progress, AnalysisStage.RESOLVE_SUBJECT, command.instrument)
        account = self.container.repository.get_latest_account_snapshot(command.instrument.market)
        if account is None:
            raise ValueError("真实账户快照缺失，不能生成可执行分析")
        facts = self._facts(command.instrument, command.mode, command.history_period, command.requested_at, on_progress)
        valuation = self._valuation(account, {command.instrument: facts}, command.requested_at)
        analysis = self._build_instrument(facts, command.mode, command.history_period, command.requested_at, account, valuation, on_progress)
        self._emit(on_progress, AnalysisStage.BUILD_REPORT, command.instrument)
        document=SingleStockReportBuilder().build(analysis.presentation)
        with self._research_lock:
            self._research_inputs[document.report_id]=analysis.presentation
        return document

    def schedule_research(self, document):
        settings=self.container.settings
        if (document.report_kind.value!="single_stock" or self.container.background_research is None or
                self.container.research_engine is None or not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model):
            return None
        return self.container.background_research.submit(document,self._research_revision)

    def _research_revision(self, base_document):
        with self._research_lock:
            value=self._research_inputs.pop(base_document.report_id,None)
        if value is None: return None
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
        capabilities=ResearchClientCapabilities(False,True,False,False)
        request=LLMResearchRequest.for_capabilities(capabilities=capabilities,request_id=request_id,
            context_id=context.context_id,prompt_version=PROMPT_VERSION,prompt_hash=prompt_hash,
            json_schema_version=1,provider_name="configured",model_name=self.container.settings.llm_model,
            requested_at=now,instrument_keys=(value.instrument.stable_key,))
        client=OpenAICompatibleResearchClient(endpoint=self.container.settings.llm_base_url,
            api_key=self.container.settings.llm_api_key,prompts={request_id:prompt},capabilities=capabilities)
        result=self.container.research_engine.run(context=context,request=request,client=client,
            market=value.instrument.market,scope_key=value.instrument.stable_key,
            base_version="research_candidate_v1",search_space_hash=stable_hash(default_specs()),
            allowed_instrument_keys=(value.instrument.stable_key,))
        response=result.get("response")
        if response is None: return None
        self.container.repository.save_research_result(context,response,result["hypotheses"],result["validations"],result["links"],candidates=result["candidates"])
        if not result["hypotheses"]: return None
        payload={item.name:getattr(value,item.name) for item in fields(value) if item.name not in {"presentation_id","source_artifact_refs"}}
        payload.update(research_hypotheses=result["hypotheses"],research_validations=result["validations"],built_at=datetime.now(timezone.utc))
        revised=single_stock_input(**payload)
        return SingleStockReportBuilder().build(revised)

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
        valuation = self._valuation(account, facts_by_instrument, command.requested_at)
        analyses = []
        for instrument in universe:
            facts = facts_by_instrument.get(instrument)
            if facts is None:
                continue
            try:
                analyses.append(self._build_instrument(facts, command.mode, command.history_period, command.requested_at, account, valuation, on_progress))
            except Exception as exc:
                failures.append(f"{instrument.code}:{type(exc).__name__}")
        if not analyses:
            raise RuntimeError("PORTFOLIO_ALL_INSTRUMENTS_UNAVAILABLE")

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
            rules = default_market_rules(instrument.market, instrument.exchange, command.requested_at)
            rules_by_instrument[instrument] = rules
            for decision in analysis.risk_bundle.decisions:
                plan = analysis.plans[decision.plan_id]
                identity = {"role": role, "scenario_id": analysis.scenario.scenario_id,
                            "plan_id": plan.plan_id, "decision_id": decision.decision_id,
                            "evidence_id": None, "rule_version": rules.rule_version}
                candidates.append(PortfolioCandidate(stable_hash(identity), role, analysis.scenario, plan, decision, None, rules, command.requested_at))
        portfolio_policy = PortfolioPolicy()
        risk_policy = RiskPolicy()
        bars_by_instrument = {item.presentation.instrument: item.bars for item in analyses}
        correlation = build_correlation_snapshot(
            market=command.market, universe=tuple(item.presentation.instrument for item in analyses),
            bars_by_instrument=bars_by_instrument, policy=portfolio_policy,
            cutoff_at=command.requested_at, source_batch_hash=stable_hash(bars_by_instrument),
            generated_at=command.requested_at,
        )
        risk_bundles = tuple(sorted((item.risk_bundle for item in analyses), key=lambda item: item.risk_bundle_id))
        candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
        holding_risks = build_holding_risks(
            valuation=valuation, account=account, candidates=candidates,
            risk_bundles=risk_bundles, captured_at=command.requested_at,
            generated_at=command.requested_at,
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
            "correlation_snapshot_id": correlation.correlation_snapshot_id, "as_of": command.requested_at,
        }
        batch = PortfolioInputBatch(
            stable_hash(batch_identity), command.market, account.currency, command.mode,
            account, valuation, risk_policy, portfolio_policy, risk_bundles,
            candidates, watch_instruments, holding_risks, correlation,
            command.requested_at, command.requested_at,
        )
        decision = self.container.portfolio_engine.decide(batch, command.requested_at)
        self.container.repository.save_portfolio_result(batch, decision)
        presentation = portfolio_input(
            market=command.market, analysis_mode=command.mode, as_of=command.requested_at,
            history_period=command.history_period, account_snapshot=account,
            frozen_account_valuation=valuation, portfolio_decision_bundle=decision,
            instruments=tuple(item.presentation for item in analyses),
            watchlist_snapshot=watch, built_at=datetime.now(timezone.utc),
        )
        self._emit(on_progress, AnalysisStage.BUILD_REPORT, None)
        return PortfolioReportBuilder().build(presentation)


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
        now = self.clock(); mode = DecisionMode(str(values.get("mode", "eod"))); history = str(values.get("history_period", "3m"))
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
        now = self.clock(); mode = DecisionMode(str(values.get("mode", "eod"))); history = str(values.get("history_period", "3m"))
        account_id = stable_hash(account); watch_id = getattr(watch, "watchlist_id", None)
        identity = {"market": market, "mode": mode, "history": history, "requested_at": now, "account": account_id, "watchlist": watch_id, "force_refresh": bool(values.get("force_refresh", False))}
        return PortfolioAnalysisCommand(stable_hash(identity), market, mode, history, now, account_id, watch_id, bool(values.get("force_refresh", False)))

    def _execute(self, kind, command, on_progress, on_complete, on_error):
        run_id=stable_hash({"command":command.command_id})
        existing=self.repository.get_analysis_run(run_id)
        if existing is not None and existing.status is AnalysisRunStatus.COMPLETED:
            document=self.repository.get_report_document(existing.deterministic_report_id)
            if document is not None:
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
        def progress(stage, instrument, message):
            nonlocal completed,current_stage
            if self._tasks.is_cancelled(command.command_id):
                raise RuntimeError("ANALYSIS_CANCELLED")
            completed = min(completed + 1, total - 1)
            if stage_order[stage] >= stage_order[current_stage]:
                current_stage=stage
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
