"""
分析服务层 — UI 与业务逻辑之间的解耦层。

负责编排完整的股票分析工作流：
  搜索 → 数据获取（含缓存） → 计算管道 → 图表 → 报告 → 持久化

UI 层仅需调用 AnalysisService.analyze() 并处理返回结果，
无需关心数据从何而来、如何计算、如何存储。

同时支持 CLI（run_backtest.py）复用同一业务逻辑。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Callable

import pandas as pd

from config.settings import Settings
from data.database import Database
from data.stock_fetcher import get_stock_fetcher, fetch_us_extended_quote
from data.models import StockInfo, PriceData, AnalysisReport
from indicators.technical import calc_all_indicators, summarize
from indicators.sentiment import aggregate
from core.pipeline import run_pipeline, AnalysisResult as PipelineResult
from core.pipeline import compute_intraday_snapshot, compute_premarket_snapshot
from strategies import get_execution_strategy
from report.chart import generate_kline_chart
from report.generator import generate_report, generate_intraday_report, generate_premarket_report
from utils.dates import get_backtest_dates
from utils.market import detect_market, search_a_stock, search_a_stock_fallback
from utils.market import search_us_stock_online, search_us_stock_fallback
from utils.session import detect_session

logger = logging.getLogger(__name__)


def _extract_direction(report_content: str, final_score: float = 0.0) -> str:
    """从 Final_Score 的符号直接判断预测方向。"""
    if final_score > 0.05:
        return "bullish"
    elif final_score < -0.05:
        return "bearish"
    return "neutral"


def _requires_realtime_token(market: str, mode: str) -> bool:
    """盘中必须 TickFlow；A 股盘前也需要实时行情；美股盘前价格走 Nasdaq.com。"""
    if mode == "intraday":
        return True
    if mode == "pre" and market == "A":
        return True
    return False


def _fetch_quote_inputs_for_mode(
    code: str,
    market: str,
    mode: str,
    fetcher=None,
) -> tuple[dict | None, dict | None, str]:
    """Fetch quote inputs from the provider allowed for the analysis mode.

    TickFlow is a regular-session source for US stocks. Premarket and
    after-hours inputs must use Nasdaq.com, with yfinance as its fallback.
    """
    if market == "US" and mode != "intraday":
        extended = fetch_us_extended_quote(code)
        if not extended or float(extended.get("price", 0) or 0) <= 0:
            return None, None, detect_session(market)
        quote = dict(extended)
        quote["latest"] = float(extended["price"])
        session = detect_session(market, stock_quote=quote)
        # An EOD report run during regular trading must not silently consume a
        # live regular-session quote through Nasdaq's generic /info endpoint.
        if mode == "eod" and session == "intraday":
            return None, None, session
        tick = {
            "latest": quote["latest"],
            "volume": quote.get("volume", 0),
            "timestamp": quote.get("timestamp", 0),
        }
        return tick, quote, session

    fetcher = fetcher or get_stock_fetcher(market)
    quote = fetcher.fetch_quote(code) if hasattr(fetcher, "fetch_quote") else None
    tick = fetcher.fetch_stock_tick(code) if hasattr(fetcher, "fetch_stock_tick") else None
    return tick, quote, detect_session(market, stock_tick=tick, stock_quote=quote)


def _single_stock_research_item(
    info: StockInfo,
    pipeline_result: PipelineResult,
    current_price: float | None = None,
) -> dict:
    """把 Tab1 单股结果包装成研究员观察模块可消费的结构。"""
    df = pipeline_result.df
    marker = {}
    price = float(current_price or 0)
    if df is not None and not df.empty:
        last = df.iloc[-1]
        recent = df.tail(min(len(df), 120))

        def f(name: str) -> float:
            value = last.get(name)
            try:
                return float(value) if value is not None and pd.notna(value) else 0.0
            except Exception:
                return 0.0

        marker = {
            "open": f("open"),
            "high": f("high"),
            "low": f("low"),
            "close": f("close"),
            "ma_20": f("ma_20"),
            "ma_60": f("ma_60"),
            "ma_120": f("ma_120"),
            "rsi": f("rsi"),
            "high_120": float(recent["high"].max()) if "high" in recent.columns and not recent.empty else 0.0,
        }
        if price <= 0:
            price = marker.get("close", 0.0)

    alpha = 0.0
    if df is not None and "Final_Score" in df.columns and not df["Final_Score"].dropna().empty:
        alpha = float(df["Final_Score"].dropna().iloc[-1])

    return {
        "watch_item": SimpleNamespace(code=info.code, name=info.name, market=info.market),
        "current_price": price,
        "alpha_score": alpha,
        "technical_marker": marker,
        "signal_check": pipeline_result.signal_check or [],
    }


@dataclass
class AnalysisRequest:
    """分析请求参数。"""
    raw_input: str         # 用户原始输入（代码或名称）
    market: str            # "A" / "US"
    period: str            # "3m" / "6m" / "1y" / "3y"
    mode: str = "eod"      # "eod" / "intraday" / "pre"


@dataclass
class AnalysisResponse:
    """分析完整结果（供 UI 渲染）。"""
    stock_info: StockInfo
    chart_path: str
    report_content: str
    backtest_results: dict             # key=策略名, value=BacktestResult
    report_id: int | None = None
    alpha_stats: dict = field(default_factory=dict)
    pipeline_result: PipelineResult | None = None


class AnalysisService:
    """股票分析编排服务。

    使用方式：
        service = AnalysisService()
        response = service.analyze(
            AnalysisRequest(raw_input="茅台", market="A", period="3m"),
            on_progress=lambda msg: print(msg),
            should_stop=lambda: False,
        )
    """

    def __init__(self):
        self._cancelled = False

    def cancel(self):
        """取消正在进行的分析。"""
        self._cancelled = True

    def analyze(
        self,
        request: AnalysisRequest,
        on_progress: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> AnalysisResponse:
        """
        执行完整分析工作流。

        Args:
            request: 分析请求（输入、市场、周期）
            on_progress: 进度回调（线程安全由调用方保证）
            should_stop: 取消检查回调

        Returns:
            AnalysisResponse 包含全部结果

        Raises:
            ValueError: 无法识别的股票
            RuntimeError: 数据获取失败
        """
        self._cancelled = False

        def _progress(msg: str):
            if on_progress:
                on_progress(msg)

        def _stop() -> bool:
            if self._cancelled:
                return True
            if should_stop and should_stop():
                return True
            return False

        # ---- 0. 预测追踪：补验证历史预测 ----
        prediction_stats = None
        validated_predictions = []
        try:
            db = Database()
            db.batch_verify_expired()
        except Exception as e:
            logger.warning(f"预测追踪验证失败: {e}")

        # ---- 1. 搜索股票代码 ----
        code = self._resolve_code(request.raw_input, request.market, _progress)
        if _stop(): return self._empty_response(code)

        # ---- 2. 获取股票信息 ----
        _progress("正在获取股票信息...")
        info = self._fetch_stock_info(code, request.market)
        if _stop(): return self._empty_response(code)

        # 预测追踪统计（需要 code 已知后才能查）
        try:
            prediction_stats = Database().get_prediction_stats(code)
            validated_predictions = Database().get_validated_predictions(code, limit=5)
        except Exception:
            pass

        # ---- 3. 获取股价数据（含缓存） ----
        _progress("正在获取股价数据...")
        df = self._fetch_prices(
            code, request.period, request.market,
            listing_date=info.listing_date,
        )
        if df is None or df.empty:
            raise RuntimeError(f"无法获取 {code} 的股价数据")
        if _stop(): return self._empty_response(code)

        # ---- 4. 获取新闻情感数据 ----
        news_agg = self._fetch_news(
            code, request.market, _progress, info.name,
            include_macro=(request.market == "US"), mode=request.mode,
        )
        if _stop(): return self._empty_response(code)

        news_df = self._build_news_df(code)

        # 无新闻数据时自动调整权重：技术面 100%，新闻面 0%
        # 否则 Final_Score 永远达不到策略阈值（如 0.6/0.7）
        if news_df is None or news_df.empty:
            w_tech, w_news = 1.0, 0.0
            logger.info("无新闻数据，Alpha 权重自动调整为 w_tech=1.0")
        else:
            w_tech, w_news = 0.6, 0.4

        # ---- 4.5. 获取基本面数据（估值 + 财务） ----
        fundamental_data = None
        try:
            from alpha.fundamental import get_fundamental_data
            _progress("正在获取基本面数据...")
            fundamental_data = get_fundamental_data(info.name, code, request.market)
        except Exception as e:
            logger.warning(f"基本面数据获取失败: {e}")

        # ---- 4.6. 获取盘口数据（需数据源支持，TickFlow 免费层无此接口） ----
        # 盘口数据占 Final_Score 权重仅 10%，不可用时自动调整权重
        depth_factor = None

        # ---- 5. 执行分析管道 ----
        _progress("正在执行量化分析...")
        market_type = detect_market(code) or request.market
        depth_score = depth_factor.get("depth_score", 0.0) if depth_factor else 0.0
        depth_avail = depth_factor.get("available", False) if depth_factor else False
        pred_rel = prediction_stats.direction_accuracy_10 if prediction_stats else 1.0
        pipeline_result = run_pipeline(
            df, news_df, market=market_type,
            w_tech=w_tech, w_news=w_news,
            fundamental_data=fundamental_data,
            depth_score=depth_score,
            depth_available=depth_avail,
            prediction_reliability=max(pred_rel, 0.3) if pred_rel > 0 else 1.0,
            stock_code=code,
            expand_pool=False,
            skip_param_tuning=True,
        )
        if _stop(): return self._empty_response(code)

        # ---- 6. 提取因子得分 ----
        alpha_stats = self._extract_alpha_stats(pipeline_result)

        # ---- 7. 生成 K 线图 ----
        _progress("正在生成 K 线图...")
        chart_path = self._generate_chart(pipeline_result.df, pipeline_result.backtest,
                                           code, info.name)

        # ---- 8. 生成报告 ----
        _progress("正在生成分析报告...")
        tech = summarize(pipeline_result.df, info.name)

        # 数据日期范围
        dates = pipeline_result.df["date"]
        data_range = f"{dates.iloc[0].strftime('%Y-%m-%d')} ~ {dates.iloc[-1].strftime('%Y-%m-%d')}"

        # 实时报价（用于报告展示）
        realtime_quote = None
        token_key = "stock_token_us" if request.market == "US" else "stock_token_a"
        if request.market == "US" or Settings().get(token_key, ""):
            try:
                _progress("正在获取实时报价...")
                fetcher = get_stock_fetcher(request.market) if request.market != "US" else None
                tick, quote, session = _fetch_quote_inputs_for_mode(
                    code, request.market, request.mode, fetcher,
                )
                if quote and float(quote.get("latest", 0) or 0) > 0:
                    q_latest = float(quote["latest"])
                    q_prev = float(pipeline_result.df["close"].iloc[-1]) if "close" in pipeline_result.df.columns else 0
                    q_prev = float(quote.get("prev_close", 0) or q_prev)
                    q_change_pct = (q_latest - q_prev) / q_prev if q_prev > 0 else 0.0
                    realtime_quote = {
                        "latest": q_latest,
                        "open": quote.get("open", 0),
                        "high": quote.get("high", 0),
                        "low": quote.get("low", 0),
                        "prev_close": q_prev,
                        "change": q_latest - q_prev,
                        "change_pct": round(q_change_pct, 6),
                        "volume": quote.get("volume", 0),
                        "amount": quote.get("amount", 0),
                        "timestamp": quote.get("timestamp", 0),
                        "status": 0,
                        "vwap": quote.get("vwap", 0),
                        "source": quote.get("source", "TickFlow"),
                        "session": session,
                    }
                    logger.info(
                        f"{realtime_quote['source']} 报价 ({code}): "
                        f"{q_latest:.2f} ({q_change_pct:+.2%})"
                    )
                elif request.market == "US":
                    logger.warning(f"美股延伸时段数据为空或当前处于常规盘中 ({code})")
            except Exception as e:
                logger.warning(f"实时报价获取失败: {e}")

        # ---- 8.5. SWOT 数据 + 同板块分析 ----
        _progress("正在分析同板块标的...")
        peer_data = self._analyze_peers(code, info, request, _progress, _stop)
        swot_data = self._build_swot_data(info, pipeline_result, news_agg)

        report_content = generate_report(
            info.to_dict(), tech, news_agg,
            pipeline_result.backtest, alpha_stats,
            data_range=data_range, depth_factor=depth_factor,
            validation=pipeline_result.validation,
            fundamental_data=pipeline_result.fundamental_data,
            rank_ic=pipeline_result.rank_ic,
            rank_ic_5d=pipeline_result.rank_ic_5d,
            rank_ic_10d=pipeline_result.rank_ic_10d,
            benchmark_return=pipeline_result.benchmark_return,
            realtime_quote=realtime_quote,
            market_regime=pipeline_result.market_regime,
            active_strategies=pipeline_result.active_strategies,
            skipped_strategies=pipeline_result.skipped_strategies,
            param_tuning=pipeline_result.param_tuning,
            swot_data=swot_data,
            peer_data=peer_data,
            operation_plan=pipeline_result.operation_plan,
        )
        if not report_content:
            report_content = "报告生成失败，请稍后重试。"

        # ── 可信度硬摘要 + 执行摘要（插入报告顶部）──
        try:
            from report.prompts import build_executive_summary, build_trust_hard_summary
            sc = pipeline_result.signal_check or []
            fs = float(pipeline_result.df["Final_Score"].dropna().iloc[-1]) if "Final_Score" in pipeline_result.df.columns and not pipeline_result.df["Final_Score"].dropna().empty else 0
            bias = "bullish" if fs > 0.05 else ("bearish" if fs < -0.05 else "neutral")
            evaluation_panel = Database().get_prediction_evaluation_panel(code)
            health_report = Database().get_strategy_health_report(code)
            trust_summary = build_trust_hard_summary(
                data_quality_reports=getattr(pipeline_result, "data_quality", None),
                audit_reports=pipeline_result.strategy_audit,
                signal_checks=sc,
                prediction_stats=prediction_stats,
                evaluation_panel=evaluation_panel,
                health_reports=health_report,
                scope=f"{info.name}（{code}）",
            )
            exec_summary = build_executive_summary(
                audit_report=pipeline_result.strategy_audit,
                operation_plan_signal_count=(len(sc), sum(1 for s in sc if s.get("signal") == "buy")),
                market_bias=bias, final_score=fs,
            )
            report_content = trust_summary + "\n" + (exec_summary + "\n" if exec_summary else "") + report_content
        except Exception as e:
            logger.warning(f"执行摘要构建失败: {e}")

        # ── 策略审计 + 系统操作方案注入第七章和第八章之间 ──
        code_sections = []
        try:
            from report.prompts import build_strategy_audit_section
            if pipeline_result.strategy_audit:
                sec = build_strategy_audit_section(pipeline_result.strategy_audit)
                if sec:
                    code_sections.append(sec)
        except Exception:
            pass
        if pipeline_result.operation_plan:
            code_sections.append(pipeline_result.operation_plan)
        if code_sections:
            section_block = "\n".join(code_sections)
            section_marker = "<!-- SECTION_8_BOUNDARY -->"
            if section_marker in report_content:
                parts = report_content.split(section_marker, 1)
                report_content = parts[0].strip() + "\n\n" + section_block + "\n\n" + section_marker + "\n\n" + parts[1].strip()
            else:
                report_content += "\n" + section_block

        # ── 研究员观察候选池（Tab1 单股）──
        research_observations = []
        try:
            from services.research_observations import (
                apply_history_feedback,
                build_research_confirmation_markdown,
                build_research_history_markdown,
                confirm_research_observations,
            )
            quote_price = None
            if realtime_quote and realtime_quote.get("latest", 0):
                quote_price = float(realtime_quote.get("latest") or 0)
            single_item = _single_stock_research_item(info, pipeline_result, quote_price)
            research_observations = confirm_research_observations(
                holdings_data=[],
                watchlist_data=[single_item],
                llm_report=report_content,
            )
            research_observations = apply_history_feedback(
                research_observations,
                lambda obs_code, pattern: db.get_research_observation_stats(
                    code=obs_code, pattern_type=pattern
                ),
            )
            research_section = build_research_confirmation_markdown(research_observations)
            if research_section:
                report_content += "\n\n" + research_section
            history_section = build_research_history_markdown(
                research_observations,
                lambda obs_code, pattern: db.get_research_observation_stats(
                    code=obs_code, pattern_type=pattern
                ),
            )
            if history_section:
                report_content += "\n" + history_section
        except Exception as e:
            logger.warning(f"单股研究员观察候选池构建失败: {e}")

        # ── 预测追踪 + 健康度（报告末尾）──
        unverified = db.get_latest_unverified_prediction(code)
        report_content += self._build_prediction_footer(
            code, prediction_stats, validated_predictions,
            unverified_count=1 if unverified else 0)

        try:
            health = db.get_strategy_health_report(code)
            candidates = db.get_strategy_param_candidates(code)
            if health or candidates:
                from report.prompts import build_strategy_health_section
                health_section = build_strategy_health_section(health, candidates)
                if health_section:
                    report_content += health_section
        except Exception as e:
            logger.warning(f"策略健康度章节构建失败: {e}")

        # ---- 9. 持久化到数据库 ----
        _progress("正在保存报告...")
        report_id = self._persist_report(info, request.market, request.period,
                                          report_content, chart_path)

        try:
            if research_observations:
                from services.research_observations import observations_to_logs
                for log in observations_to_logs(
                    research_observations,
                    market=request.market,
                    mode=request.mode,
                    report_id=report_id,
                    observed_at=datetime.now().isoformat(),
                ):
                    db.insert_research_observation(log)
        except Exception as e:
            logger.warning(f"单股研究员观察记录入库失败: {e}")

        # ---- 9.5 预测追踪：存入预测记录 ----
        try:
            fs = float(pipeline_result.df["Final_Score"].dropna().iloc[-1])
            pp = float(pipeline_result.df["close"].iloc[-1])
            last_date = str(pipeline_result.df["date"].iloc[-1])[:10]
            current_predictions = self._prediction_signals(pipeline_result)
            primary = current_predictions[0] if current_predictions else {}
            plan_entry = float(primary.get("entry_price", 0.0) or 0.0)
            plan_stop = float(primary.get("stop_loss", 0.0) or 0.0)
            plan_take_profit = float(primary.get("take_profit", 0.0) or 0.0)

            # 整体预测（兼容旧逻辑）
            self._save_prediction(
                code=code, market=request.market, mode="eod",
                report_id=report_id,
                reference_date=last_date,
                direction=str(primary.get("direction") or _extract_direction(report_content, fs)),
                final_score=fs,
                predicted_price=pp,
                prediction_stats=prediction_stats,
                report_content=report_content,
                conservative_entry=plan_entry,
                stop_loss=plan_stop,
                take_profit=plan_take_profit,
                strategy_name="",
                market_regime=getattr(pipeline_result, "market_regime", ""),
            )
            # 按本次真实结构化信号写入，不再用回测最后一笔交易代替当前决策。
            for signal in current_predictions:
                try:
                    self._save_prediction(
                        code=code, market=request.market, mode="eod",
                        report_id=report_id,
                        reference_date=last_date,
                        direction=str(signal["direction"]),
                        final_score=fs,
                        predicted_price=pp,
                        prediction_stats=prediction_stats,
                        report_content=report_content,
                        conservative_entry=float(signal.get("entry_price", 0.0) or 0.0),
                        stop_loss=float(signal.get("stop_loss", 0.0) or 0.0),
                        take_profit=float(signal.get("take_profit", 0.0) or 0.0),
                        strategy_name=str(signal.get("strategy_name") or ""),
                        signal_action=str(signal.get("action") or ""),
                        market_regime=getattr(pipeline_result, "market_regime", ""),
                    )
                except Exception:
                    pass  # 单策略预测失败不阻塞整体流程
        except Exception as e:
            logger.warning(f"预测写入失败: {e}")

        try:
            from services.optimization_scheduler import schedule_deep_optimization
            submitted = schedule_deep_optimization(
                stock_code=code,
                market=market_type,
                df=pipeline_result.df,
                strategy_keys=pipeline_result.active_strategies,
                initial_capital=100000.0,
                news_df=news_df,
            )
            if submitted:
                logger.info(f"{code} 深度参数优化已转入后台，不阻塞报告返回")
        except Exception as e:
            logger.warning(f"后台参数优化调度失败（非致命）: {e}")

        _progress("分析完成")
        return AnalysisResponse(
            stock_info=info,
            chart_path=chart_path or "",
            report_content=report_content,
            backtest_results=pipeline_result.backtest,
            report_id=report_id,
            alpha_stats=alpha_stats,
            pipeline_result=pipeline_result,
        )

    # ======================== 内部方法 ========================

    def _resolve_code(self, raw: str, market: str, progress) -> str:
        """搜索股票名称 → 代码。"""
        code = raw.strip().upper()

        if market == "A":
            if not (code.isascii() and code.isdigit() and len(code) == 6):
                progress("正在搜索 A 股...")
                results = search_a_stock_fallback(raw)
                if not results:
                    results = search_a_stock(raw)
                if not results:
                    raise ValueError(f"未找到与「{raw}」匹配的 A 股")
                code = results[0]["code"]
        else:
            if not (code.isascii() and code.replace(".", "").replace("-", "").isalpha()):
                progress("正在搜索美股...")
                results = search_us_stock_fallback(raw)
                if not results:
                    results = search_us_stock_online(raw)
                if not results:
                    raise ValueError(f"未找到与「{raw}」匹配的美股")
                code = results[0]["code"]
        return code

    def _fetch_stock_info(self, code: str, market: str) -> StockInfo:
        fetcher = get_stock_fetcher(market)
        info = fetcher.fetch_stock_info(code)
        if not info:
            logger.warning(f"未获取到 {code} 的股票信息，使用默认名")
            info = StockInfo(code=code, name=code, market=market)

        # ── 美股：用 Finnhub profile2 补全行业和业务描述 ──
        if market == "US":
            from config.settings import Settings
            finnhub_token = (Settings().get("news_token_us", "") or "").strip()
            if finnhub_token:
                try:
                    from data.finnhub_client import fetch_company_profile
                    profile = fetch_company_profile(finnhub_token, code)
                    if profile:
                        from data.stock_fetcher import _normalize_listing_date
                        info.listing_date = _normalize_listing_date(
                            profile.get("ipo") or profile.get("ipoDate")
                        )
                        if not info.industry:
                            info.industry = profile.get("finnhubIndustry") or profile.get("industry") or ""
                        if not info.description:
                            parts = []
                            for key in ("exchange", "country", "currency", "webUrl"):
                                val = profile.get(key)
                                if val:
                                    parts.append(f"{key}:{val}")
                            market_cap = profile.get("marketCapitalization", 0)
                            if market_cap:
                                parts.append(f"marketCap:{market_cap:,.0f}")
                            info.description = "; ".join(parts)[:2000]
                        logger.info(f"Finnhub 补全: industry={info.industry}, desc={info.description[:80]}...")
                except Exception as e:
                    logger.warning(f"Finnhub profile2 补全失败 ({code}): {e}")

        # ── A 股：用 baostock 补全行业 ──
        if market == "A" and not info.industry:
            try:
                from alpha.fundamental import _fetch_stock_industry_baostock
                industry = _fetch_stock_industry_baostock(code)
                if industry:
                    info.industry = industry
                    logger.info(f"baostock 补全行业: {industry}")
            except Exception as e:
                logger.warning(f"baostock 行业补全失败 ({code}): {e}")

        Database().upsert_stock(info)
        logger.info(f"股票信息: {info.name} ({info.industry or '未知行业'})")
        return info

    def _fetch_prices(
        self,
        code: str,
        period: str,
        market: str = "US",
        listing_date: str = "",
    ) -> pd.DataFrame | None:
        start, end = get_backtest_dates(period)
        from data.stock_fetcher import fetch_cached_prices
        return fetch_cached_prices(
            code, market, start, end, db=Database(), listing_date=listing_date
        )

    def _fetch_news(self, code: str, market: str, progress,
                    name: str = "", include_macro: bool = False,
                    mode: str = "eod") -> dict:
        progress("正在加载新闻...")
        from services.news_service import refresh_stock_news
        news_list = refresh_stock_news(
            code=code, name=name, market=market, mode=mode,
            db=Database(), limit=5, include_macro=include_macro,
        )
        logger.info(f"新闻: {len(news_list)} 条")

        news_agg = aggregate(news_list)
        logger.info(f"情感得分: {news_agg['sentiment_score']:.2f}")
        return news_agg

    def _build_news_df(self, code: str) -> pd.DataFrame | None:
        try:
            news_items = Database().get_news(code, limit=500)
            if not news_items:
                return None
            daily_scores: dict[str, list[float]] = {}
            daily_weights: dict[str, list[float]] = {}
            for n in news_items:
                if not n.sentiment:
                    continue
                date_key = str(n.date)[:10]
                score_map = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
                if date_key not in daily_scores:
                    daily_scores[date_key] = []
                    daily_weights[date_key] = []
                confidence = n.confidence if n.confidence and n.confidence > 0 else 0.5
                source_weight = 0.5 if getattr(n, "is_macro", False) else 1.0
                weight = max(min(confidence, 1.0), 0.1) * source_weight
                daily_scores[date_key].append(score_map.get(n.sentiment, 0.0) * weight)
                daily_weights[date_key].append(weight)
            rows = [
                {
                    "date": k,
                    "finbert_score": sum(v) / sum(daily_weights[k]),
                }
                for k, v in daily_scores.items()
                if sum(daily_weights[k]) > 0
            ]
            if rows:
                return pd.DataFrame(rows)
        except Exception as e:
            logger.warning(f"构造新闻得分 DataFrame 失败: {e}")
        return None

    @staticmethod
    def _extract_alpha_stats(result: PipelineResult) -> dict:
        stats = {}
        if "Final_Score" in result.df.columns:
            valid = result.df["Final_Score"].dropna()
            stats = {
                "mean": round(float(valid.mean()), 3),
                "std": round(float(valid.std()), 3),
                "latest": round(float(valid.iloc[-1]), 3) if len(valid) > 0 else 0,
            }
        return stats

    @staticmethod
    def _generate_chart(df: pd.DataFrame, bt_results: dict,
                        code: str, name: str) -> str:
        chart_df = df.copy()
        for r in bt_results.values():
            if r.fills:
                chart_df["signal"] = ""
                for fill in r.fills:
                    fill_date = fill.date
                    mask = pd.to_datetime(chart_df["date"]).dt.strftime("%Y-%m-%d") == fill_date
                    if mask.any():
                        idx = chart_df[mask].index[0]
                        chart_df.loc[idx, "signal"] = fill.action
                break
        return generate_kline_chart(chart_df, code, name) or ""

    @staticmethod
    def _persist_report(info: StockInfo, market: str, period: str,
                        content: str, chart_path: str,
                        mode: str = "eod",
                        prediction_data: str = "") -> int | None:
        report = AnalysisReport(
            code=info.code, name=info.name, market=market,
            backtest_period=period,
            create_time=datetime.now().isoformat(),
            content=content,
            chart_path=chart_path or "",
            mode=mode,
            prediction_data=prediction_data,
        )
        return Database().insert_report(report)

    # ── 预测追踪 ──

    @staticmethod
    def _build_prediction_footer(code: str, prediction_stats,
                                  validated_predictions: list,
                                  unverified_count: int = 0) -> str:
        from report.prompts import build_prediction_footer
        evaluation_panel = None
        try:
            evaluation_panel = Database().get_prediction_evaluation_panel(code)
        except Exception:
            pass
        return build_prediction_footer(
            code, prediction_stats, validated_predictions,
            unverified_count, evaluation_panel=evaluation_panel)

    @staticmethod
    def _save_prediction(code: str, market: str, mode: str,
                         direction: str, final_score: float,
                         predicted_price: float,
                         prediction_stats=None,
                         report_id: int | None = None,
                         reference_date: str = "",
                         report_content: str = "",
                         conservative_entry: float = 0.0,
                         stop_loss: float = 0.0,
                         take_profit: float = 0.0,
                         strategy_name: str = "",
                         signal_action: str = "",
                         market_regime: str = "") -> int | None:
        """写入一条新的预测记录到 prediction_log。"""
        from data.models import PredictionLog
        from datetime import datetime as _dt
        import re

        # 尝试从报告中提取建议入场价
        entry = conservative_entry or 0.0
        if entry <= 0:
            for pat in [r'(?:入场|买入|回调至).*?\$?(\d+\.?\d*)',
                         r'入场价[：:\s]*\$?(\d+\.?\d*)',
                         r'等待.*?回调.*?\$?(\d+\.?\d*)']:
                m = re.search(pat, report_content or "")
                if m:
                    entry = float(m.group(1))
                    break

        verify_days = {"pre": 1, "intraday": 1, "eod": 5, "portfolio": 7}
        pred = PredictionLog(
            code=code, market=market, mode=mode,
            report_id=report_id,
            predict_time=_dt.now().isoformat(),
            reference_date=reference_date,
            direction=direction,
            final_score=final_score,
            predicted_price=predicted_price,
            conservative_entry=entry,
            entry_mode=(
                "signal_price" if conservative_entry > 0 and mode == "intraday"
                else "next_open" if conservative_entry > 0 and mode in ("eod", "pre")
                else "conditional" if entry > 0
                else "reference"
            ),
            stop_loss=stop_loss,
            take_profit=take_profit,
            verify_after_days=verify_days.get(mode, 5),
            key_reason=f"Final_Score={final_score:+.3f}, status={prediction_stats.status if prediction_stats else 'N/A'}",
            confidence="high" if abs(final_score) > 0.5 else ("medium" if abs(final_score) > 0.2 else "low"),
            strategy_name=strategy_name,
            signal_action=signal_action,
            market_regime=market_regime,
            exit_review_status="pending" if signal_action == "sell" else "not_applicable",
        )
        try:
            return Database().insert_prediction(pred)
        except Exception as e:
            logger.warning(f"预测写入失败 ({code}): {e}")
            return None

    @staticmethod
    def _prediction_trade_levels(
        pipeline_result: PipelineResult | None,
    ) -> tuple[float, float, float, str]:
        """从结构化信号检查结果中提取预测跟踪用入场/止损。"""
        if not pipeline_result or not pipeline_result.signal_check:
            return 0.0, 0.0, 0.0, ""
        for item in pipeline_result.signal_check:
            if item.get("signal") in ("buy", "sell"):
                return (
                    float(item.get("entry_price") or 0.0),
                    float(item.get("stop_loss") or 0.0),
                    float(item.get("take_profit") or 0.0),
                    str(item.get("key") or item.get("variant") or ""),
                )
        return 0.0, 0.0, 0.0, ""

    @staticmethod
    def _prediction_signals(
        pipeline_result: PipelineResult | None,
    ) -> list[dict]:
        """把当前结构化信号转成可验证预测，不从报告文本/回测交易猜测。"""
        if not pipeline_result or not pipeline_result.signal_check:
            return []
        records = []
        for item in pipeline_result.signal_check:
            action = str(item.get("signal") or "").lower()
            if action not in ("buy", "sell"):
                continue
            execution_level = str(item.get("execution_level") or "").upper()
            if execution_level in ("C", "D"):
                continue
            # 当前系统不做裸空；sell 表示减仓/退出后对“避免后续下跌”的验证。
            records.append({
                "action": action,
                "direction": "bullish" if action == "buy" else "bearish",
                "strategy_name": str(item.get("key") or item.get("variant") or ""),
                "entry_price": float(item.get("entry_price") or item.get("trigger_price") or 0.0),
                "stop_loss": float(item.get("stop_loss") or 0.0) if action == "buy" else 0.0,
                "take_profit": float(item.get("take_profit") or 0.0) if action == "buy" else 0.0,
                "execution_level": execution_level,
            })
        return records

    # ── SWOT 数据构建 ──

    @staticmethod
    def _build_news_agg_for_swot(
        code: str,
        info: StockInfo,
        news_list: list | None,
    ) -> dict:
        """
        将增量新闻列表转为 news_agg 格式（供 SWOT 使用）。

        Args:
            code:      股票代码
            info:      股票信息
            news_list: 新闻 Item 列表（盘中/盘前获取的今日新闻）
        """
        if not news_list:
            return {"summary": "暂无最新新闻数据。", "top_news": ""}

        pos = sum(1 for n in news_list if hasattr(n, 'sentiment') and n.sentiment == "positive")
        neg = sum(1 for n in news_list if hasattr(n, 'sentiment') and n.sentiment == "negative")
        total = len(news_list)

        top_lines: list[str] = []
        for n in news_list[:5]:
            emoji = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(
                getattr(n, 'sentiment', 'neutral'), "⚪")
            top_lines.append(f"- {emoji} [{getattr(n, 'date', '')}] {getattr(n, 'title', '')}")

        score = (pos - neg) / total if total > 0 else 0.0
        summary = (
            f"近期新闻整体偏{'正面' if score > 0.2 else ('负面' if score < -0.2 else '中性')}，"
            f"正面 {pos}/{total}，负面 {neg}/{total}。"
        )
        return {"summary": summary, "top_news": "\n".join(top_lines), "sentiment_score": round(score, 4)}

    @staticmethod
    def _build_swot_data(
        info: StockInfo,
        pipeline_result: PipelineResult,
        news_agg: dict,
    ) -> dict:
        """
        从已有数据中提取 SWOT 分析所需的结构化素材。

        Returns:
            {
                "financial": {财务指标},
                "valuation": {估值因子},
                "news": [近期新闻摘要列表],
                "industry": str,
                "company_name": str,
                "market": str,
            }
        """
        swot: dict = {
            "company_name": info.name,
            "code": info.code,
            "market": info.market,
            "industry": info.industry or "未分类",
        }

        # ── 财务数据 ──
        fundamental = (
            pipeline_result.fundamental_data.get("fundamental_factors", {})
            if pipeline_result.fundamental_data
            else {}
        )
        style = (
            pipeline_result.fundamental_data.get("style_factors", {})
            if pipeline_result.fundamental_data
            else {}
        )
        swot["financial"] = {
            "roe": fundamental.get("roe", 0),
            "gross_margin": fundamental.get("gross_margin", 0),
            "debt_ratio": fundamental.get("debt_ratio", 0),
            "net_profit_yoy": fundamental.get("net_profit_yoy", 0),
            "revenue_yoy": fundamental.get("revenue_yoy", 0),
        }
        swot["valuation"] = {
            "pe_percentile": style.get("pe_percentile", 0.5),
            "pb_percentile": style.get("pb_percentile", 0.5),
        }

        # ── 新闻摘要（取 Top 5 标题） ──
        top_news = news_agg.get("top_news", "")
        news_items: list[str] = []
        if top_news:
            for line in top_news.split("\n"):
                line = line.strip()
                if line and line.startswith("-"):
                    news_items.append(line.lstrip("- ").strip())
        swot["news"] = news_items[:5]

        # ── 行情状态 ──
        swot["market_regime"] = pipeline_result.market_regime or "unknown"

        return swot

    # ── 同板块分析 ──

    def _analyze_peers(
        self,
        code: str,
        info: StockInfo,
        request: AnalysisRequest,
        progress: Callable[[str], None],
        stop: Callable[[], bool],
    ) -> list[dict]:
        """
        获取同类股票并并行执行简化版 Alpha 管道（仅技术面），
        按 Final_Score 排名返回。

        使用 ThreadPoolExecutor 并行化，最多 4 个并发，
        将 10 只标的的分析时间从 ~20s 降至 ~5s。
        """
        try:
            from data.peer_fetcher import fetch_peers

            peers = fetch_peers(
                code=code,
                market=request.market,
                industry=info.industry or "",
                limit=10,
            )
        except Exception as e:
            logger.warning(f"获取同类股失败: {e}")
            return []

        if not peers:
            logger.info(f"未获取到 {code} 的同类股")
            return []

        total = len(peers)
        progress(f"正在分析同板块标的…(0/{total})")

        # 每个 peer 的独立分析函数（线程安全）
        def _analyze_one(peer: dict) -> dict | None:
            peer_code = peer["code"]
            peer_name = peer.get("name", peer_code)

            if stop():
                return None

            try:
                start, end = get_backtest_dates(request.period)
                from data.stock_fetcher import fetch_cached_prices
                df = fetch_cached_prices(peer_code, request.market, start, end, db=Database(), min_records=20)
                if df is None:
                    logger.info(f"  跳过 {peer_code}：无 K 线数据")
                    return None

                peer_result = run_pipeline(
                    df, news_df=None, market=request.market,
                    w_tech=1.0, w_news=0.0,
                    skip_param_tuning=True,
                    strategy_names=[],
                    expand_pool=False,
                    run_backtests=False,
                    run_signals=False,
                )

                latest_score = 0.0
                if ("Final_Score" in peer_result.df.columns
                        and not peer_result.df["Final_Score"].dropna().empty):
                    latest_score = float(peer_result.df["Final_Score"].dropna().iloc[-1])

                regime = peer_result.market_regime or "unknown"

                if latest_score > 0.6:
                    verdict = "✅ 强烈关注"
                elif latest_score > 0.3:
                    verdict = "👀 可关注"
                elif latest_score > -0.3:
                    verdict = "➖ 观望"
                else:
                    verdict = "❌ 暂不建议"

                if not peer_name or peer_name == peer_code:
                    try:
                        db_info = Database().get_stock(peer_code)
                        if db_info and db_info.name:
                            peer_name = db_info.name
                    except Exception:
                        pass

                logger.info(f"  同板块 {peer_code}: score={latest_score:+.3f} regime={regime}")
                return {
                    "code": peer_code,
                    "name": peer_name or peer_code,
                    "final_score": round(latest_score, 3),
                    "regime": regime,
                    "verdict": verdict,
                }

            except Exception as e:
                logger.warning(f"同类股 {peer_code} 分析失败: {e}")
                return None

        # 并行执行（最多 4 个并发，避免打爆 TickFlow API）
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results: list[dict] = []
        completed = 0

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_analyze_one, p): p for p in peers}
            for future in as_completed(futures):
                completed += 1
                peer_info = futures[future]
                progress(f"正在分析同板块标的…({completed}/{total}) {peer_info['code']}")
                try:
                    result = future.result()
                    if result is not None:
                        results.append(result)
                except Exception as e:
                    logger.warning(f"同板块并行任务异常: {e}")

        # 按 Final_Score 降序排名
        results.sort(key=lambda r: r["final_score"], reverse=True)
        for i, r in enumerate(results):
            r["rank"] = i + 1

        progress(f"同板块分析完成，共 {len(results)} 只有效结果")
        return results

    @staticmethod
    def _empty_response(code: str) -> AnalysisResponse:
        return AnalysisResponse(
            stock_info=StockInfo(code=code, name=code, market=""),
            chart_path="",
            report_content="",
            backtest_results={},
        )

    # ======================== 盘中分析 ========================

    def analyze_intraday(
        self,
        request: AnalysisRequest,
        on_progress: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> AnalysisResponse:
        """
        执行盘中快速分析（美股 + A 股）。

        流程：
          1. 获取 T-1 日完整分析结果（DB 缓存 or 即时计算）
          2. 并行拉取实时数据（报价 + 盘口 + 增量新闻）
          3. 计算盘中快照
          4. 生成盘中报告（复用 T-1 报告第 1-7 章 + LLM 重写第 8 章）

        要求：配置对应市场的数据源 Token（TickFlow API Key，免费注册即可获取实时行情）。
        """
        from config.settings import Settings
        settings = Settings()
        token_key = "stock_token_us" if request.market == "US" else "stock_token_a"
        token = settings.get(token_key, "")
        if _requires_realtime_token(request.market, "intraday") and not token:
            market_label = "美股" if request.market == "US" else "A股"
            raise RuntimeError(
                f"盘中分析需要配置「{market_label}数据源 Token」（TickFlow API Key），"
                f"请在设置中填写。\n免费注册：https://tickflow.org"
            )

        self._cancelled = False

        def _progress(msg: str):
            if on_progress:
                on_progress(msg)

        def _stop() -> bool:
            if self._cancelled:
                return True
            if should_stop and should_stop():
                return True
            return False

        # ---- 0. 预测追踪：补验证历史预测 ----
        prediction_stats = None
        validated_predictions = []
        try:
            db = Database()
            db.batch_verify_expired()
        except Exception as e:
            logger.warning(f"预测追踪验证失败: {e}")

        # ---- 1. 搜索股票代码 ----
        code = self._resolve_code(request.raw_input, request.market, _progress)
        if _stop(): return self._empty_response(code)

        # ---- 2. 获取股票信息 ----
        _progress("正在获取股票信息...")
        info = self._fetch_stock_info(code, request.market)
        if _stop(): return self._empty_response(code)

        # ---- 3. 获取 T-1 日完整分析结果 + 盘前报告 ----
        t1_report_content, pre_report_content, pre_prediction_data, t1_pipeline_result = \
            self._get_recent_analyses(code, _progress, _stop)
        if _stop(): return self._empty_response(code)

        # ---- 4. 并行拉取实时数据 ----
        _progress("正在获取盘中实时数据...")
        fetcher = get_stock_fetcher(request.market)

        realtime_quote = None
        stock_tick = None
        today_news_list = None

        # 实时报价
        try:
            realtime_quote = fetcher.fetch_quote(code)
        except Exception as e:
            logger.warning(f"实时报价获取失败: {e}")

        # 实时成交（校验交易时段）
        try:
            stock_tick = fetcher.fetch_stock_tick(code)
        except Exception as e:
            logger.warning(f"实时成交数据获取失败: {e}")

        # 增量新闻（今日，含情感分析）
        try:
            from services.news_service import refresh_stock_news
            today_news_list = refresh_stock_news(
                code=code, name=info.name, market=request.market,
                mode="intraday", db=Database(), limit=5,
            )
        except Exception as e:
            logger.warning(f"增量新闻获取失败: {e}")

        if _stop(): return self._empty_response(code)

        if not realtime_quote or realtime_quote.get("latest", 0) <= 0:
            raise RuntimeError(f"无法获取 {code} 的实时报价，盘中分析不可用。")

        # 校验交易时段
        session = detect_session(request.market, stock_tick=stock_tick, stock_quote=realtime_quote)
        logger.info(f"当前交易时段: {session}")
        if session != "intraday":
            raise RuntimeError(
                f"当前不是常规盘中交易时段（检测到 {session}）。"
                "请改用「盘后分析」或在盘前时段使用「盘前分析」。"
            )

        # ---- 5. 计算盘中快照 ----
        _progress("正在计算盘中快照...")
        from services.signal_stabilizer import SignalStabilizer
        stabilizer_decision = SignalStabilizer().should_emit(code, realtime_quote.get("latest", 0))
        if not stabilizer_decision.should_emit and stabilizer_decision.previous_report:
            logger.info(f"盘中信号防抖命中: {stabilizer_decision.reason}")
            _progress("盘中波动较小，复用最近报告...")
            return AnalysisResponse(
                stock_info=info,
                chart_path=stabilizer_decision.previous_report.chart_path or "",
                report_content=stabilizer_decision.previous_report.content,
                backtest_results={},
                report_id=stabilizer_decision.previous_report.id,
            )
        if t1_pipeline_result is None:
            _progress("正在计算 T-1 日分析...")
            t1_pipeline_result = self._run_eod_to_t1(code, request, _progress, _stop)
        if _stop(): return self._empty_response(code)
        if t1_pipeline_result is None:
            raise RuntimeError("T-1 数据获取失败，请检查网络和数据源配置。")
        if not t1_report_content:
            _progress("正在生成 T-1 日报告...")
            t1_report_content = self._generate_t1_report_text(
                code, info, t1_pipeline_result, request.period,
            )
            # 自动生成的 T-1 报告存回 DB，下次可直接复用
            if t1_report_content:
                self._persist_report(info, request.market, request.period,
                                    t1_report_content, "", mode="eod")

        snapshot = compute_intraday_snapshot(
            t1_pipeline_result,
            realtime_quote=realtime_quote,
            today_news=today_news_list,
            session=session,
            market=request.market,
            premarket_prediction_json=pre_prediction_data,
        )
        realtime_decision_df = t1_pipeline_result.df
        try:
            from core.signal_check import refresh_realtime_signal_plan
            realtime_decision_df = refresh_realtime_signal_plan(
                t1_pipeline_result,
                market=request.market,
                current_quote=realtime_quote,
                account_equity=100000.0,
                stock_code=code,
            )
            logger.info(
                f"盘中策略已按实时 OHLC 重算：price={snapshot.latest_price:.2f}, "
                f"signals={len(t1_pipeline_result.signal_check or [])}"
            )
        except Exception as e:
            logger.warning(f"盘中策略实时重算失败，保留T-1条件计划: {e}")
        if _stop(): return self._empty_response(code)

        # ---- 5.5. SWOT + 同板块 ----
        swot_data: dict | None = None
        peer_data: list[dict] | None = None
        if t1_pipeline_result:
            try:
                news_agg = self._build_news_agg_for_swot(code, info, today_news_list)
                swot_data = self._build_swot_data(info, t1_pipeline_result, news_agg)
            except Exception as e:
                logger.warning(f"盘中 SWOT 数据构建失败: {e}")
        if not _stop():
            try:
                peer_data = self._analyze_peers(code, info, request, _progress, _stop)
            except Exception as e:
                logger.warning(f"盘中同板块分析失败: {e}")

        # ---- 6. 生成盘中报告 ----
        _progress("正在生成盘中分析报告...")
        if not t1_report_content:
            t1_report_content = self._generate_t1_report_text(
                code, info, t1_pipeline_result, request.period,
            )

        report_content = generate_intraday_report(
            t1_report_content=t1_report_content,
            snapshot_text=snapshot.markdown,
            stock_info=info.to_dict(),
            swot_data=swot_data,
            peer_data=peer_data,
            pre_report_content=pre_report_content,
            operation_plan=getattr(t1_pipeline_result, "operation_plan", None) if t1_pipeline_result else None,
        )
        if not report_content:
            report_content = "盘中报告生成失败，请稍后重试。"

        # ── 执行摘要 ──
        t1_sc = getattr(t1_pipeline_result, "signal_check", None) if t1_pipeline_result else None
        t1_sc_list = t1_sc or []
        t1_fs = 0.0
        if realtime_decision_df is not None and "Final_Score" in realtime_decision_df.columns:
            try:
                t1_fs = float(realtime_decision_df["Final_Score"].dropna().iloc[-1])
            except Exception:
                pass
        t1_bias = "bullish" if t1_fs > 0.05 else ("bearish" if t1_fs < -0.05 else "neutral")
        try:
            from report.prompts import build_executive_summary
            exec_sum = build_executive_summary(
                audit_report=getattr(t1_pipeline_result, "strategy_audit", None) if t1_pipeline_result else None,
                operation_plan_signal_count=(len(t1_sc_list), sum(1 for s in t1_sc_list if s.get("signal") == "buy")),
                market_bias=t1_bias, final_score=t1_fs,
            )
            if exec_sum:
                report_content = exec_sum + "\n" + report_content
        except Exception:
            pass

        # ── 策略审计 + 系统操作方案（注入 LLM 第八章之前）──
        code_sections = []
        if t1_pipeline_result and t1_pipeline_result.strategy_audit:
            try:
                from report.prompts import build_strategy_audit_section
                sec = build_strategy_audit_section(t1_pipeline_result.strategy_audit)
                if sec:
                    code_sections.append(sec)
            except Exception:
                pass
        op = getattr(t1_pipeline_result, "operation_plan", None) if t1_pipeline_result else None
        if op:
            code_sections.append(op)
        if code_sections:
            import re
            section_block = "\n".join(code_sections)
            # 在 LLM 第八章（## 八、或 ## 8）之前插入
            ch8_match = re.search(r"\n##\s*[八8][、.\s]", report_content)
            if ch8_match:
                idx = ch8_match.start()
                report_content = report_content[:idx] + "\n" + section_block + "\n" + report_content[idx:]
            else:
                report_content += "\n" + section_block

        # ── 预测追踪 + 健康度（报告末尾）──
        unverified = db.get_latest_unverified_prediction(code)
        report_content += self._build_prediction_footer(
            code, prediction_stats, validated_predictions,
            unverified_count=1 if unverified else 0)

        try:
            health = db.get_strategy_health_report(code)
            candidates = db.get_strategy_param_candidates(code)
            if health or candidates:
                from report.prompts import build_strategy_health_section
                sec = build_strategy_health_section(health, candidates)
                if sec:
                    report_content += sec
        except Exception:
            pass

        report_id = self._persist_report(
            info, request.market, request.period,
            report_content, "", mode="intraday",
        )

        _progress("盘中分析完成")

        # 预测追踪：写入盘中预测
        try:
            fs = float(realtime_decision_df["Final_Score"].dropna().iloc[-1]) if realtime_decision_df is not None and "Final_Score" in realtime_decision_df.columns else 0.0
            pp = float(snapshot.latest_price or 0.0)
            current_predictions = self._prediction_signals(t1_pipeline_result)
            primary = current_predictions[0] if current_predictions else {}
            self._save_prediction(
                code=code, market=request.market, mode="intraday",
                reference_date=(
                    str(t1_pipeline_result.df["date"].iloc[-1])[:10]
                    if t1_pipeline_result is not None and "date" in t1_pipeline_result.df.columns
                    else ""
                ),
                direction=str(primary.get("direction") or _extract_direction(report_content, fs)),
                final_score=fs,
                predicted_price=pp,
                prediction_stats=prediction_stats,
                report_content=report_content,
                conservative_entry=float(primary.get("entry_price", 0.0) or 0.0),
                stop_loss=float(primary.get("stop_loss", 0.0) or 0.0),
                take_profit=float(primary.get("take_profit", 0.0) or 0.0),
                strategy_name="",
                market_regime=getattr(t1_pipeline_result, "market_regime", "") if t1_pipeline_result else "",
            )
            for signal in current_predictions:
                self._save_prediction(
                    code=code, market=request.market, mode="intraday",
                    reference_date=(
                        str(t1_pipeline_result.df["date"].iloc[-1])[:10]
                        if t1_pipeline_result is not None and "date" in t1_pipeline_result.df.columns
                        else ""
                    ),
                    direction=str(signal["direction"]),
                    final_score=fs,
                    predicted_price=pp,
                    prediction_stats=prediction_stats,
                    conservative_entry=float(signal.get("entry_price", 0.0) or 0.0),
                    stop_loss=float(signal.get("stop_loss", 0.0) or 0.0),
                    take_profit=float(signal.get("take_profit", 0.0) or 0.0),
                    strategy_name=str(signal.get("strategy_name") or ""),
                    signal_action=str(signal.get("action") or ""),
                    market_regime=getattr(t1_pipeline_result, "market_regime", "") if t1_pipeline_result else "",
                )
        except Exception as e:
            logger.warning(f"盘中预测写入失败: {e}")

        return AnalysisResponse(
            stock_info=info,
            chart_path="",
            report_content=report_content,
            backtest_results=t1_pipeline_result.backtest if t1_pipeline_result else {},
            report_id=report_id,
            alpha_stats=self._extract_alpha_stats(t1_pipeline_result) if t1_pipeline_result else {},
            pipeline_result=t1_pipeline_result,
        )

    # ======================== 盘前分析 ========================

    def analyze_premarket(
        self,
        request: AnalysisRequest,
        on_progress: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> AnalysisResponse:
        """
        执行盘前分析（美股 + A 股）。

        流程：
          1. 获取 T-1 日完整分析结果
          2. 并行拉取盘前数据（stock tick + ETF quote + 隔夜新闻）
          3. 计算盘前快照
          4. 生成盘前报告

        美股：QQQ/SPY ETF 替代 NQ/ES 期货（对预测准确度影响 ≈0）
        A 股：基于 T-1 数据 + 隔夜新闻 + 集合竞价价格

        要求：A 股盘前需配置实时行情 Token；美股盘前延伸时段价格使用 Nasdaq.com。
        """
        from config.settings import Settings
        settings = Settings()
        token_key = "stock_token_us" if request.market == "US" else "stock_token_a"
        token = settings.get(token_key, "")
        if _requires_realtime_token(request.market, "pre") and not token:
            market_label = "美股" if request.market == "US" else "A股"
            raise RuntimeError(
                f"盘前分析需要配置「{market_label}数据源 Token」（TickFlow API Key），"
                f"请在设置中填写。\n免费注册：https://tickflow.org"
            )

        self._cancelled = False

        def _progress(msg: str):
            if on_progress:
                on_progress(msg)

        def _stop() -> bool:
            if self._cancelled:
                return True
            if should_stop and should_stop():
                return True
            return False

        # ---- 0. 预测追踪：补验证历史预测 ----
        prediction_stats = None
        validated_predictions = []
        try:
            db = Database()
            db.batch_verify_expired()
        except Exception as e:
            logger.warning(f"预测追踪验证失败: {e}")

        # ---- 1. 搜索股票代码 ----
        code = self._resolve_code(request.raw_input, request.market, _progress)
        if _stop(): return self._empty_response(code)

        # ---- 2. 获取股票信息 ----
        _progress("正在获取股票信息...")
        info = self._fetch_stock_info(code, request.market)
        if _stop(): return self._empty_response(code)

        # ---- 3. 获取 T-1 日完整分析结果 ----
        t1_report_content, _, _, t1_pipeline_result = self._get_recent_analyses(
            code, _progress, _stop,
        )
        if _stop(): return self._empty_response(code)

        # ---- 4. 并行拉取盘前数据 ----
        _progress("正在获取盘前数据...")
        fetcher = get_stock_fetcher(request.market) if request.market == "A" else None

        stock_tick = None
        nq_quote = None
        es_quote = None
        overnight_news_list = None

        stock_quote = None
        try:
            stock_tick, stock_quote, raw_session_for_pre = _fetch_quote_inputs_for_mode(
                code, request.market, "pre", fetcher,
            )
        except Exception as e:
            logger.warning(f"盘前股票报价获取失败: {e}")
            raw_session_for_pre = detect_session(request.market)

        # 宏观情绪参考：美股 QQQ/SPY ETF，A 股用沪深300/上证50 ETF
        if request.market == "US":
            # 美股盘前个股价格已由统一模式路由直接从 Nasdaq/yfinance 获取。
            if stock_quote:
                logger.info(f"盘前价格: {code}={stock_quote['latest']:.2f}")
            else:
                logger.warning(f"盘前数据为空 ({code})")
                _progress("⚠️ 盘前数据不可用，成交量/价格可能不准确")

            # ── QQQ/SPY 盘前价格同步获取 ──
            try:
                qqq_pre = fetch_us_extended_quote("QQQ")
                if qqq_pre:
                    nq_quote = {
                        "latest": qqq_pre["price"],
                        "open": qqq_pre["open"],
                        "high": qqq_pre["high"],
                        "low": qqq_pre["low"],
                        "prev_close": qqq_pre.get("prev_close", 0),
                        "change": qqq_pre["price"] - qqq_pre.get("prev_close", 0),
                        "change_pct": qqq_pre.get("change_pct", 0),
                        "volume": qqq_pre["volume"],
                        "amount": 0,
                        "timestamp": qqq_pre["timestamp"],
                        "status": 0,
                    }
                    logger.info(f"QQQ 盘前: {qqq_pre['price']:.2f}")
            except Exception as e:
                logger.warning(f"QQQ 盘前失败: {e}")

            try:
                spy_pre = fetch_us_extended_quote("SPY")
                if spy_pre:
                    es_quote = {
                        "latest": spy_pre["price"],
                        "open": spy_pre["open"],
                        "high": spy_pre["high"],
                        "low": spy_pre["low"],
                        "prev_close": spy_pre.get("prev_close", 0),
                        "change": spy_pre["price"] - spy_pre.get("prev_close", 0),
                        "change_pct": spy_pre.get("change_pct", 0),
                        "volume": spy_pre["volume"],
                        "amount": 0,
                        "timestamp": spy_pre["timestamp"],
                        "status": 0,
                    }
                    logger.info(f"SPY 盘前: {spy_pre['price']:.2f}")
            except Exception as e:
                logger.warning(f"SPY 盘前失败: {e}")

        elif request.market == "A":
            # A 股盘前：集合竞价 9:15-9:25，宏观参考用沪深300 + 上证50 ETF
            try:
                hs300 = fetcher.fetch_quote("510300")
                if hs300:
                    nq_quote = hs300
                    logger.info(f"沪深300 ETF (510300): {hs300.get('latest', 0):.2f}")
            except Exception as e:
                logger.warning(f"沪深300 ETF 获取失败: {e}")

            try:
                sz50 = fetcher.fetch_quote("510050")
                if sz50:
                    es_quote = sz50
                    logger.info(f"上证50 ETF (510050): {sz50.get('latest', 0):.2f}")
            except Exception as e:
                logger.warning(f"上证50 ETF 获取失败: {e}")

        # 验证盘前时段。盘前报告只应在开盘前使用；盘中/盘后应切换模式。
        session_for_pre = raw_session_for_pre
        if session_for_pre != "pre":
            raise RuntimeError(
                f"当前不是盘前时段（检测到 {session_for_pre}）。"
                "请在常规交易时段使用「盘中分析」，收盘后使用「盘后分析」。"
            )

        # 隔夜新闻（含情感分析）
        try:
            from services.news_service import refresh_stock_news
            overnight_news_list = refresh_stock_news(
                code=code, name=info.name, market=request.market,
                mode="pre", db=Database(), limit=8,
            )
        except Exception as e:
            logger.warning(f"隔夜新闻获取失败: {e}")

        if _stop(): return self._empty_response(code)

        # 构建期货数据
        futures_data = {}
        futures_data["NQ"] = nq_quote or {}
        futures_data["ES"] = es_quote or {}

        # ---- 5. 确保有 T-1 pipeline result ----
        if t1_pipeline_result is None:
            _progress("正在计算 T-1 日分析...")
            t1_pipeline_result = self._run_eod_to_t1(code, request, _progress, _stop)
        if _stop(): return self._empty_response(code)
        if t1_pipeline_result is None:
            raise RuntimeError("T-1 数据获取失败，请检查网络和数据源配置。")
        if not t1_report_content:
            _progress("正在生成 T-1 日报告...")
            t1_report_content = self._generate_t1_report_text(
                code, info, t1_pipeline_result, request.period,
            )
            if t1_report_content:
                self._persist_report(info, request.market, request.period,
                                    t1_report_content, "", mode="eod")

        # ---- 6. 计算盘前快照 ----
        _progress("正在计算盘前快照...")
        snapshot = compute_premarket_snapshot(
            t1_pipeline_result,
            stock_tick=stock_tick or {},
            futures_data=futures_data,
            overnight_news=overnight_news_list,
            session="pre",
            market=request.market,
            stock_quote=stock_quote,
        )
        if _stop(): return self._empty_response(code)

        # ---- 6.5. SWOT + 同板块 ----
        swot_data: dict | None = None
        peer_data: list[dict] | None = None
        if t1_pipeline_result:
            try:
                news_agg = self._build_news_agg_for_swot(code, info, overnight_news_list)
                swot_data = self._build_swot_data(info, t1_pipeline_result, news_agg)
            except Exception as e:
                logger.warning(f"盘前 SWOT 数据构建失败: {e}")
        if not _stop():
            try:
                peer_data = self._analyze_peers(code, info, request, _progress, _stop)
            except Exception as e:
                logger.warning(f"盘前同板块分析失败: {e}")

        # ---- 7. 生成盘前报告 ----
        _progress("正在生成盘前分析报告...")
        if not t1_report_content:
            t1_report_content = self._generate_t1_report_text(
                code, info, t1_pipeline_result, request.period,
            )

        report_content = generate_premarket_report(
            t1_report_content=t1_report_content,
            snapshot_text=snapshot.markdown,
            stock_info=info.to_dict(),
            swot_data=swot_data,
            peer_data=peer_data,
            operation_plan=getattr(t1_pipeline_result, "operation_plan", None) if t1_pipeline_result else None,
        )
        if not report_content:
            report_content = "盘前报告生成失败，请稍后重试。"

        # ── 执行摘要 ──
        t1_sc = getattr(t1_pipeline_result, "signal_check", None) if t1_pipeline_result else None
        t1_sc_list = t1_sc or []
        t1_fs = 0.0
        if t1_pipeline_result and "Final_Score" in t1_pipeline_result.df.columns:
            try:
                t1_fs = float(t1_pipeline_result.df["Final_Score"].dropna().iloc[-1])
            except Exception:
                pass
        t1_bias = "bullish" if t1_fs > 0.05 else ("bearish" if t1_fs < -0.05 else "neutral")
        try:
            from report.prompts import build_executive_summary
            exec_sum = build_executive_summary(
                audit_report=getattr(t1_pipeline_result, "strategy_audit", None) if t1_pipeline_result else None,
                operation_plan_signal_count=(len(t1_sc_list), sum(1 for s in t1_sc_list if s.get("signal") == "buy")),
                market_bias=t1_bias, final_score=t1_fs,
            )
            if exec_sum:
                report_content = exec_sum + "\n" + report_content
        except Exception:
            pass

        # ── 策略审计 + 系统操作方案（注入 LLM 第八章之前）──
        code_sections = []
        if t1_pipeline_result and t1_pipeline_result.strategy_audit:
            try:
                from report.prompts import build_strategy_audit_section
                sec = build_strategy_audit_section(t1_pipeline_result.strategy_audit)
                if sec:
                    code_sections.append(sec)
            except Exception:
                pass
        op = getattr(t1_pipeline_result, "operation_plan", None) if t1_pipeline_result else None
        if op:
            code_sections.append(op)
        if code_sections:
            import re
            section_block = "\n".join(code_sections)
            ch8_match = re.search(r"\n##\s*[八8][、.\s]", report_content)
            if ch8_match:
                idx = ch8_match.start()
                report_content = report_content[:idx] + "\n" + section_block + "\n" + report_content[idx:]
            else:
                report_content += "\n" + section_block

        # ── 预测追踪 + 健康度（报告末尾）──
        unverified = db.get_latest_unverified_prediction(code)
        report_content += self._build_prediction_footer(
            code, prediction_stats, validated_predictions,
            unverified_count=1 if unverified else 0)

        try:
            health = db.get_strategy_health_report(code)
            candidates = db.get_strategy_param_candidates(code)
            if health or candidates:
                from report.prompts import build_strategy_health_section
                sec = build_strategy_health_section(health, candidates)
                if sec:
                    report_content += sec
        except Exception:
            pass

        # 存储盘前预测数据（供盘中分析时做预测验证）
        import json
        prediction_data = json.dumps({
            "pre_price": snapshot.pre_price,
            "pre_change_pct": snapshot.pre_change_pct,
            "nq_change_pct": snapshot.nq_change_pct,
            "es_change_pct": snapshot.es_change_pct,
            "futures_score": snapshot.futures_score,
            "t1_final_score": snapshot.t1_final_score,
            "t1_date": str(t1_pipeline_result.df["date"].iloc[-1])[:10] if t1_pipeline_result and "date" in t1_pipeline_result.df.columns else "",
            "generated_at": datetime.now().isoformat(),
        }, ensure_ascii=False)
        self._persist_report(info, request.market, request.period,
                            report_content, "", mode="pre",
                            prediction_data=prediction_data)

        # 预测追踪：写入盘前预测
        try:
            fs = float(t1_pipeline_result.df["Final_Score"].dropna().iloc[-1]) if t1_pipeline_result and "Final_Score" in t1_pipeline_result.df.columns else 0.0
            pp = float(t1_pipeline_result.df["close"].iloc[-1]) if t1_pipeline_result else 0.0
            current_predictions = self._prediction_signals(t1_pipeline_result)
            primary = current_predictions[0] if current_predictions else {}
            self._save_prediction(
                code=code, market=request.market, mode="pre",
                reference_date=(
                    str(t1_pipeline_result.df["date"].iloc[-1])[:10]
                    if t1_pipeline_result is not None and "date" in t1_pipeline_result.df.columns
                    else ""
                ),
                direction=str(primary.get("direction") or _extract_direction(report_content, fs)),
                final_score=fs,
                predicted_price=pp,
                prediction_stats=prediction_stats,
                report_content=report_content,
                conservative_entry=float(primary.get("entry_price", 0.0) or 0.0),
                stop_loss=float(primary.get("stop_loss", 0.0) or 0.0),
                take_profit=float(primary.get("take_profit", 0.0) or 0.0),
                strategy_name="",
                market_regime=getattr(t1_pipeline_result, "market_regime", "") if t1_pipeline_result else "",
            )
            for signal in current_predictions:
                self._save_prediction(
                    code=code, market=request.market, mode="pre",
                    reference_date=(
                        str(t1_pipeline_result.df["date"].iloc[-1])[:10]
                        if t1_pipeline_result is not None and "date" in t1_pipeline_result.df.columns
                        else ""
                    ),
                    direction=str(signal["direction"]),
                    final_score=fs,
                    predicted_price=pp,
                    prediction_stats=prediction_stats,
                    conservative_entry=float(signal.get("entry_price", 0.0) or 0.0),
                    stop_loss=float(signal.get("stop_loss", 0.0) or 0.0),
                    take_profit=float(signal.get("take_profit", 0.0) or 0.0),
                    strategy_name=str(signal.get("strategy_name") or ""),
                    signal_action=str(signal.get("action") or ""),
                    market_regime=getattr(t1_pipeline_result, "market_regime", "") if t1_pipeline_result else "",
                )
        except Exception as e:
            logger.warning(f"盘前预测写入失败: {e}")

        _progress("盘前分析完成")
        return AnalysisResponse(
            stock_info=info,
            chart_path="",
            report_content=report_content,
            backtest_results=t1_pipeline_result.backtest if t1_pipeline_result else {},
            alpha_stats=self._extract_alpha_stats(t1_pipeline_result) if t1_pipeline_result else {},
            pipeline_result=t1_pipeline_result,
        )

    # ======================== T-1 日分析缓存与获取 ========================

    @staticmethod
    def _previous_trading_day() -> str:
        """返回上一个交易日（跳过周末）。"""
        from datetime import date as dt_date, timedelta
        today = dt_date.today()
        d = today - timedelta(days=1)
        while d.weekday() >= 5:  # 周六=5, 周日=6
            d -= timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    def _get_recent_analyses(
        self, code: str,
        progress, stop,
    ) -> tuple[str | None, str | None, str | None, PipelineResult | None]:
        """
        从 DB 缓存读取 T-1 盘后分析报告。

        缓存策略：
          - 盘后(eod)报告：必须来自上一个交易日（T-1），跨周末自动跳过
          - 盘前(pre)报告：最近 12 小时内

        如果不是真正的 T-1 报告，即使存在也不复用，让调用方重新生成。

        Returns:
            (eod_report_content, pre_report_content, pre_prediction_data, pipeline_result)
            — pipeline_result 始终为 None（不可从 text 恢复）
        """
        eod_report = None
        pre_report = None
        pre_prediction = None
        try:
            eod_reports = Database().get_reports_by_code(
                code, mode="eod", since_hours=168,
            )
            if eod_reports:
                # 直接取最新一份 EOD 报告（已按时间倒序排列）
                eod_report = eod_reports[0].content
                logger.info(f"复用最新盘后缓存报告: {eod_reports[0].create_time}, "
                            f"共 {len(eod_reports)} 份候选")

            # 查询最近的盘前报告（12 小时内）
            pre_reports = Database().get_reports_by_code(
                code, mode="pre", since_hours=12,
            )
            if pre_reports:
                pre_report = pre_reports[0].content
                pre_prediction = pre_reports[0].prediction_data or None
                logger.info(
                    f"复用盘前缓存报告: {pre_reports[0].create_time}, "
                    f"{len(pre_report)} chars, prediction={'有' if pre_prediction else '无'}"
                )
        except Exception as e:
            logger.warning(f"读取近期缓存报告失败: {e}")

        return eod_report, pre_report, pre_prediction, None

    def _run_eod_to_t1(
        self, code: str, request: AnalysisRequest,
        progress, stop,
    ) -> PipelineResult | None:
        """
        执行一次简化的盘后分析管道到 T-1 日（不含报告生成）。
        用于盘中/盘前分析时，如果没有 T-1 缓存则即时计算。
        """
        try:
            from utils.dates import get_backtest_dates
            start, end = get_backtest_dates(request.period)
            # 如果 end 包含今天，截断到昨天（T-1）
            today_str = datetime.now().strftime("%Y-%m-%d")
            if end >= today_str:
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                end = yesterday

            from data.stock_fetcher import fetch_cached_prices
            df = fetch_cached_prices(code, request.market, start, end, db=Database())
            if df is None:
                logger.warning("T-1 日数据为空")
                return None

            news_df = self._build_news_df(code)
            if news_df is None or news_df.empty:
                w_tech, w_news = 1.0, 0.0
            else:
                w_tech, w_news = 0.6, 0.4

            # 获取基本面数据
            fundamental_data = None
            try:
                from alpha.fundamental import get_fundamental_data
                info = self._fetch_stock_info(code, request.market)
                fundamental_data = get_fundamental_data(info.name if info else code, code, request.market)
            except Exception as e:
                logger.warning(f"基本面数据获取失败: {e}")

            market_type = detect_market(code) or request.market
            validation_mode = request.mode if request.mode in ("intraday", "pre") else "eod"
            pipeline_result = run_pipeline(
                df, news_df, market=market_type,
                w_tech=w_tech, w_news=w_news,
                validation_mode=validation_mode,
                fundamental_data=fundamental_data,
                stock_code=code,
                expand_pool=False,
                skip_param_tuning=True,
            )
            return pipeline_result
        except Exception as e:
            logger.error(f"T-1 日分析管道执行失败: {e}")
            return None

    def _generate_t1_report_text(
        self, code: str, info: StockInfo,
        pipeline_result: PipelineResult, period: str,
    ) -> str:
        """生成确定性的 T-1 上下文，供盘中/盘前最终 LLM 使用。

        中间上下文不再单独调用一次 LLM；最终报告仍保留一次 LLM
        综合解读，避免无缓存时连续生成两份长报告。
        """
        try:
            tech = summarize(pipeline_result.df, info.name)
            dates = pipeline_result.df["date"]
            data_range = f"{dates.iloc[0].strftime('%Y-%m-%d')} ~ {dates.iloc[-1].strftime('%Y-%m-%d')}"
            alpha_stats = self._extract_alpha_stats(pipeline_result)

            # 新闻汇总
            news_agg = {"summary": "暂无新闻数据", "top_news": "", "sentiment_score": 0.0}
            try:
                news_list = Database().get_news(code, limit=5)
                if news_list:
                    news_agg = aggregate(news_list)
            except Exception:
                pass

            content = generate_report(
                info.to_dict(), tech, news_agg,
                pipeline_result.backtest, alpha_stats,
                data_range=data_range,
                validation=pipeline_result.validation,
                fundamental_data=pipeline_result.fundamental_data,
                rank_ic=pipeline_result.rank_ic,
                rank_ic_5d=pipeline_result.rank_ic_5d,
                rank_ic_10d=pipeline_result.rank_ic_10d,
                benchmark_return=pipeline_result.benchmark_return,
                market_regime=pipeline_result.market_regime,
                active_strategies=pipeline_result.active_strategies,
                skipped_strategies=pipeline_result.skipped_strategies,
                param_tuning=pipeline_result.param_tuning,
                use_llm=False,
            )
            return content or ""
        except Exception as e:
            logger.error(f"生成 T-1 报告文本失败: {e}")
            return ""
