"""唯一生产 composition root：所有 V2 引擎共享一个 repository 和生命周期。"""
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import dataclass, fields
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tradehelper_v2.config.settings import V2Settings
from tradehelper_v2.contracts.enums import Market
from tradehelper_v2.contracts.runtime import RuntimeHealth
from tradehelper_v2.data.calendar import ExchangeTradingCalendar
from tradehelper_v2.data.composition import build_data_refresh_service
from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.application.exports import export_report
from tradehelper_v2.application.history import ReportHistoryService
from tradehelper_v2.application.portfolio import PortfolioEditor
from tradehelper_v2.application.lookup import InstrumentLookupService
from tradehelper_v2.application.finbert import FinbertEnricher
from tradehelper_v2.application.analysis import AnalysisApplication, RuntimeAnalysisPipeline
from tradehelper_v2.application.background import BackgroundResearchService, BackgroundLearningService
from tradehelper_v2.features.snapshot import FeatureBuilder
from tradehelper_v2.forecast.registry import ForecastRegistry
from tradehelper_v2.forecast.engine import ForecastEngine
from tradehelper_v2.forecast.trainer import ForecastTrainer
from tradehelper_v2.scenario.planner import ScenarioPlanner
from tradehelper_v2.strategies.engine import StrategyEngine
from tradehelper_v2.risk.officer import RiskOfficer
from tradehelper_v2.execution.orders import OrderIntentFactory
from tradehelper_v2.portfolio.engine import PortfolioDecisionEngine
from tradehelper_v2.learning.engine import LearningEngine
from tradehelper_v2.research.parser import StrictHypothesisParser
from tradehelper_v2.research.validator import DeterministicHypothesisValidator
from tradehelper_v2.research.bridge import CandidateBridge
from tradehelper_v2.research.engine import ResearchEngine
from tradehelper_v2.runtime.paths import model_path

@dataclass(slots=True)
class RuntimeContainer:
    settings: V2Settings
    repository: SQLiteRepository
    calendar: ExchangeTradingCalendar
    data_refresh: object
    background_executor: ThreadPoolExecutor
    lookup: InstrumentLookupService
    finbert: FinbertEnricher
    history: ReportHistoryService
    portfolio_editor: PortfolioEditor
    analysis: object | None = None
    single_stock: object | None = None
    portfolio: object | None = None
    background_research: object | None = None
    background_learning: object | None = None
    feature_builder: object | None = None
    forecast_registry: object | None = None
    forecast_engine: object | None = None
    forecast_trainer: object | None = None
    scenario_planner: object | None = None
    strategy_engine: object | None = None
    risk_officer: object | None = None
    order_intent_factory: object | None = None
    portfolio_engine: object | None = None
    learning_engine: object | None = None
    research_engine: object | None = None
    learning_executor: ThreadPoolExecutor | None = None
    provider_executor: ThreadPoolExecutor | None = None
    migration_status: str = "not_required"
    closed: bool = False

    def health(self) -> RuntimeHealth:
        try:
            self.repository._connection.execute("SELECT 1").fetchone(); database_status="ready"
        except Exception: database_status="unavailable"
        try:
            self.calendar._calendar(Market.US); self.calendar._calendar(Market.A); calendar_status="available"
        except Exception: calendar_status="unavailable"
        provider_names=tuple(sorted(item.name for item in fields(self.data_refresh.providers) if getattr(self.data_refresh.providers,item.name) is not None))
        capabilities=(*provider_names,"sqlite","feature","forecast","scenario","strategy","risk","execution","portfolio","learning")
        if self.research_engine is not None and self.settings.llm_api_key and self.settings.llm_base_url and self.settings.llm_model:
            capabilities=(*capabilities,"research")
        return RuntimeHealth("2.0.0",17,"ready",database_status,calendar_status,"available" if self.finbert.available else "unavailable",capabilities,self.migration_status,datetime.now(timezone.utc))

    def close(self) -> None:
        if self.closed: return
        self.closed=True
        for executor in (self.background_executor, self.learning_executor, self.provider_executor):
            if executor is not None: executor.shutdown(wait=True, cancel_futures=True)
        self.repository.close()

def build_runtime_container(settings: V2Settings, *, repository: SQLiteRepository | None = None) -> RuntimeContainer:
    repo=repository or SQLiteRepository(settings.database_path)
    calendar=ExchangeTradingCalendar()
    data=build_data_refresh_service(settings, repo)
    finbert=FinbertEnricher(model_path(settings.finbert_model_path))
    def search_provider(market, query):
        try:
            instrument=__import__("tradehelper_v2.contracts",fromlist=["InstrumentId"]).InstrumentId.from_code(query,market)
        except Exception:
            return ()
        result=data.refresh_metadata(instrument,datetime.now(timezone.utc))
        return (result.value,) if getattr(result,"value",None) is not None else ()
    lookup=InstrumentLookupService(repo,search_provider)
    background=ThreadPoolExecutor(max_workers=4,thread_name_prefix="tradehelper-v2")
    registry=ForecastRegistry(); registry.restore(repo.list_forecast_champions()); feature_builder=FeatureBuilder(calendar)
    container=RuntimeContainer(settings,repo,calendar,data,background,lookup,finbert,ReportHistoryService(repo),PortfolioEditor(repo,lambda:datetime.now(timezone.utc)),
        feature_builder=feature_builder, forecast_registry=registry, forecast_engine=ForecastEngine(calendar,registry), forecast_trainer=ForecastTrainer(),
        scenario_planner=ScenarioPlanner(), strategy_engine=StrategyEngine(), risk_officer=RiskOfficer(), order_intent_factory=OrderIntentFactory(calendar),
        portfolio_engine=PortfolioDecisionEngine(), learning_engine=LearningEngine(), learning_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="tradehelper-learning"),
        provider_executor=ThreadPoolExecutor(max_workers=4,thread_name_prefix="tradehelper-provider"))
    try:
        parser=StrictHypothesisParser(); validator=DeterministicHypothesisValidator(parser.registry); bridge=CandidateBridge(parser.registry)
        container.research_engine=ResearchEngine(parser,validator,bridge)
    except Exception:
        # 研究映射注册失败属于能力降级；确定性主链仍然可用。
        container.research_engine=None
    # 生产环境可替换 pipeline 注入真实各层引擎；默认仍严格拒绝没有事实的执行建议。
    container.analysis=AnalysisApplication(repo, RuntimeAnalysisPipeline(container), container.background_executor)
    container.single_stock=container.analysis
    container.portfolio=container.analysis
    container.background_research=BackgroundResearchService(repo, container.background_executor)
    container.background_learning=BackgroundLearningService(repo,container.learning_engine,container.learning_executor)
    return container
