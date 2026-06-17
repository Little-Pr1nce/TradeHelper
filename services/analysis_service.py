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
from typing import Callable

import pandas as pd

from data.database import Database
from data.stock_fetcher import get_stock_fetcher
from data.news_fetcher import fetch_news
from data.models import StockInfo, PriceData, AnalysisReport
from indicators.technical import calc_all_indicators, summarize
from indicators.sentiment import analyze, aggregate
from core.pipeline import run_pipeline, AnalysisResult as PipelineResult
from core.pipeline import compute_intraday_snapshot, compute_premarket_snapshot
from report.chart import generate_kline_chart
from report.generator import generate_report, generate_intraday_report, generate_premarket_report
from utils.dates import get_backtest_dates
from utils.market import detect_market, search_a_stock, search_a_stock_fallback
from utils.market import search_us_stock_online, search_us_stock_fallback
from utils.session import detect_session

logger = logging.getLogger(__name__)


def _fetch_premarket_from_yfinance(code: str) -> dict | None:
    """兼容旧调用名：美股盘前/盘后价格统一走 yfinance helper。"""
    from data.stock_fetcher import fetch_us_extended_quote

    return fetch_us_extended_quote(code)


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

        # ---- 1. 搜索股票代码 ----
        code = self._resolve_code(request.raw_input, request.market, _progress)
        if _stop(): return self._empty_response(code)

        # ---- 2. 获取股票信息 ----
        _progress("正在获取股票信息...")
        info = self._fetch_stock_info(code, request.market)
        if _stop(): return self._empty_response(code)

        # ---- 3. 获取股价数据（含缓存） ----
        _progress("正在获取股价数据...")
        df = self._fetch_prices(code, request.period, request.market)
        if df is None or df.empty:
            raise RuntimeError(f"无法获取 {code} 的股价数据")
        if _stop(): return self._empty_response(code)

        # ---- 4. 获取新闻情感数据 ----
        news_agg = self._fetch_news(code, request.market, _progress, info.name, include_macro=(request.market == "US"))
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
            from config.settings import Settings
            from alpha.fundamental import fetch_fundamental_factors
            settings = Settings()
            _progress("正在获取基本面数据...")
            fundamental_data = fetch_fundamental_factors(
                name=info.name, code=code, market=request.market,
                model=settings.get("llm_model", ""),
                base_url=settings.get("llm_base_url", ""),
                api_key=settings.get("llm_api_key", ""),
                finnhub_token=settings.get("news_token_us", ""),
            )
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
        pipeline_result = run_pipeline(
            df, news_df, market=market_type,
            w_tech=w_tech, w_news=w_news,
            fundamental_data=fundamental_data,
            depth_score=depth_score,
            depth_available=depth_avail,
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
        if Settings().get(token_key, ""):
            try:
                _progress("正在获取实时报价...")
                fetcher = get_stock_fetcher(request.market)
                tick = fetcher.fetch_stock_tick(code) if hasattr(fetcher, 'fetch_stock_tick') else None
                quote = fetcher.fetch_quote(code) if hasattr(fetcher, 'fetch_quote') else None

                # 检测当前交易时段
                # 美股：只有盘中（regular hours）用 TickFlow 实时数据，
                # 盘前/盘后/休市都用 yfinance（TickFlow 不支持延伸时段）
                session = detect_session(request.market, stock_tick=tick, stock_quote=quote)
                use_yfinance = request.market == "US" and session != "intraday"

                # 美股盘前/盘后：TickFlow 不支持延伸时段，用 yfinance
                if use_yfinance:
                    try:
                        yf_data = _fetch_premarket_from_yfinance(code)
                        if yf_data:
                            q_prev = float(pipeline_result.df["close"].iloc[-1]) if "close" in pipeline_result.df.columns else 0
                            realtime_quote = {
                                "latest": yf_data["price"],
                                "open": yf_data["open"],
                                "high": yf_data["high"],
                                "low": yf_data["low"],
                                "prev_close": q_prev,
                                "change": yf_data["price"] - q_prev,
                                "change_pct": round((yf_data["price"] - q_prev) / q_prev, 6) if q_prev > 0 else 0.0,
                                "volume": yf_data["volume"],
                                "amount": 0,
                                "timestamp": yf_data["timestamp"],
                                "status": 0,
                                "vwap": 0,
                            }
                            logger.info(f"yfinance 延伸时段报价 ({code}): {yf_data['price']:.2f}")
                        else:
                            logger.warning(f"yfinance 延伸时段数据为空 ({code})")
                            _progress("⚠️ yfinance 盘前/盘后数据不可用，涨跌幅/成交量可能为 0")
                    except Exception as e:
                        logger.warning(f"yfinance 延伸时段报价失败 ({code}): {e}")
                        _progress("⚠️ yfinance 盘前/盘后数据不可用，涨跌幅/成交量可能为 0")

                # 盘中/非美股 → 用 TickFlow（实时）
                if realtime_quote is None and tick:
                    q_latest = tick.get("latest", 0)
                    q_prev = float(pipeline_result.df["close"].iloc[-1]) if "close" in pipeline_result.df.columns else 0
                    q_change_pct = (q_latest - q_prev) / q_prev if q_prev > 0 else 0.0
                    realtime_quote = {
                        "latest": q_latest,
                        "open": quote.get("open", 0) if quote else 0,
                        "high": quote.get("high", 0) if quote else 0,
                        "low": quote.get("low", 0) if quote else 0,
                        "prev_close": q_prev,
                        "change": q_latest - q_prev,
                        "change_pct": round(q_change_pct, 6),
                        "volume": quote.get("volume", 0) if quote else tick.get("volume", 0),
                        "amount": quote.get("amount", 0) if quote else 0,
                        "timestamp": tick.get("timestamp", 0),
                        "status": 0,
                        "vwap": quote.get("vwap", 0) if quote else 0,
                    }
                    logger.info(f"TickFlow 实时报价 ({code}): {q_latest:.2f} ({q_change_pct:+.2%})")
                elif realtime_quote is None and quote:
                    realtime_quote = quote
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
        )
        if not report_content:
            report_content = "报告生成失败，请稍后重试。"

        # ---- 9. 持久化到数据库 ----
        _progress("正在保存报告...")
        report_id = self._persist_report(info, request.market, request.period,
                                          report_content, chart_path)

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

    def _fetch_prices(self, code: str, period: str, market: str = "US") -> pd.DataFrame | None:
        start, end = get_backtest_dates(period)
        prices = Database().get_prices(code, start, end)

        if not prices:
            # 缓存为空 → 全量拉取
            logger.info(f"缓存为空，联网拉取 {start}~{end}")
            fetcher = get_stock_fetcher(market)
            new_prices = fetcher.fetch_price_history(code, start, end)
            if new_prices:
                Database().insert_prices(new_prices)
            prices = Database().get_prices(code, start, end)
        else:
            last_date = prices[-1].date
            logger.info(f"缓存: {len(prices)} 条 ({prices[0].date}~{last_date})")

            # 增量拉取（按 count 返回最近 N 条，不会因周末/节假日返回空列表）
            from datetime import date as dt_date, timedelta
            next_day = (dt_date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
            today_str = dt_date.today().isoformat()
            logger.info(f"检查增量 {next_day}~{today_str}")
            fetcher = get_stock_fetcher(market)
            new_prices = fetcher.fetch_price_history(code, next_day, today_str)
            # 不能仅靠 new_prices 是否空判断——数据源按 count 取最近 N 条，
            # 不会因为区间内无交易日就返回空列表。必须比较最新一条的日期。
            latest_new_date = max((p.date for p in new_prices), default="")
            if latest_new_date > last_date:
                Database().insert_prices(new_prices)
                prices = Database().get_prices(code, start, end)
                new_count = sum(1 for p in new_prices if p.date > last_date)
                logger.info(f"增量获取 {new_count} 条新数据（最新 {latest_new_date}）")
            else:
                logger.info(f"无增量数据（缓存最新 {last_date}，数据源最新 {latest_new_date or '无'}）")

        if not prices:
            return None

        df = pd.DataFrame([p.to_dict() for p in prices])
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    def _fetch_news(self, code: str, market: str, progress,
                    name: str = "", include_macro: bool = False) -> dict:
        progress("正在加载新闻...")
        from config.settings import Settings
        settings = Settings()
        news_list = fetch_news(
            name=name, code=code, market=market,
            news_token_us=settings.get("news_token_us", ""),
            news_token_a=settings.get("news_token_a", ""),
            limit=5,
            include_macro=include_macro,
        )
        logger.info(f"新闻: {len(news_list)} 条")

        if news_list:
            needs_analysis = [n for n in news_list if not n.sentiment]
            if needs_analysis:
                progress("正在进行新闻情感分析...")
                analyzed = analyze(needs_analysis)
                analyzed_map = {(n.date, n.title): n for n in analyzed}
                news_list = [
                    analyzed_map.get((n.date, n.title), n) for n in news_list
                ]
            Database().insert_news(news_list)

        news_agg = aggregate(news_list)
        logger.info(f"情感得分: {news_agg['sentiment_score']:.2f}")
        return news_agg

    def _build_news_df(self, code: str) -> pd.DataFrame | None:
        try:
            news_items = Database().get_news(code, limit=500)
            if not news_items:
                return None
            daily_scores: dict[str, list[float]] = {}
            for n in news_items:
                if not n.sentiment:
                    continue
                date_key = str(n.date)[:10]
                score_map = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
                if date_key not in daily_scores:
                    daily_scores[date_key] = []
                daily_scores[date_key].append(score_map.get(n.sentiment, 0.0))
            rows = [{"date": k, "finbert_score": sum(v) / len(v)}
                    for k, v in daily_scores.items()]
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
                prices = Database().get_prices(peer_code, start, end)
                if not prices:
                    fetcher = get_stock_fetcher(request.market)
                    prices = fetcher.fetch_price_history(peer_code, start, end)
                    if prices:
                        Database().insert_prices(prices)
                        prices = Database().get_prices(peer_code, start, end)
                if not prices:
                    logger.info(f"  跳过 {peer_code}：无 K 线数据")
                    return None

                import pandas as pd
                df = pd.DataFrame([p.to_dict() for p in prices])
                df["date"] = pd.to_datetime(df["date"])

                peer_result = run_pipeline(
                    df, news_df=None, market=request.market,
                    w_tech=1.0, w_news=0.0,
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
                        db_info = Database().get_stock_info(peer_code)
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
        if not token:
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
            today_news_list = fetch_news(
                name=info.name, code=code, market=request.market,
                news_token_us=settings.get("news_token_us", ""),
                news_token_a=settings.get("news_token_a", ""),
                limit=5,
            )
            if today_news_list:
                needs_analysis = [n for n in today_news_list if not n.sentiment]
                if needs_analysis:
                    from indicators.sentiment import analyze
                    analyzed = analyze(needs_analysis)
                    analyzed_map = {(n.date, n.title): n for n in analyzed}
                    today_news_list = [
                        analyzed_map.get((n.date, n.title), n)
                        for n in today_news_list
                    ]
        except Exception as e:
            logger.warning(f"增量新闻获取失败: {e}")

        if _stop(): return self._empty_response(code)

        if not realtime_quote or realtime_quote.get("latest", 0) <= 0:
            raise RuntimeError(f"无法获取 {code} 的实时报价，盘中分析不可用。")

        # 校验交易时段
        session = detect_session(request.market, stock_tick=stock_tick, stock_quote=realtime_quote)
        logger.info(f"当前交易时段: {session}")

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
        )
        if not report_content:
            report_content = "盘中报告生成失败，请稍后重试。"

        report_id = self._persist_report(
            info, request.market, request.period,
            report_content, "", mode="intraday",
        )

        _progress("盘中分析完成")
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

        要求：配置对应市场的数据源 Token（TickFlow API Key）。
        """
        from config.settings import Settings
        settings = Settings()
        token_key = "stock_token_us" if request.market == "US" else "stock_token_a"
        token = settings.get(token_key, "")
        if not token:
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
        fetcher = get_stock_fetcher(request.market)

        stock_tick = None
        nq_quote = None
        es_quote = None
        nq_kline = None
        es_kline = None
        overnight_news_list = None

        # 股票 tick（盘前价格 + 交易时段）
        try:
            stock_tick = fetcher.fetch_stock_tick(code)
        except Exception as e:
            logger.warning(f"股票 tick 获取失败: {e}")

        # 股票实时报价（获取累计成交量，tick 接口只返回单笔量）
        stock_quote = None
        try:
            stock_quote = fetcher.fetch_quote(code)
        except Exception as e:
            logger.warning(f"股票实时报价获取失败: {e}")

        # 宏观情绪参考：美股 QQQ/SPY ETF，A 股暂无（后续可加 A50 ETF）
        if request.market == "US":
            # QQQ/SPY ETF 替代 NQ/ES 期货
            try:
                nq_quote = fetcher.fetch_future_quote("US", "NQ")
            except Exception as e:
                logger.warning(f"QQQ ETF 报价获取失败: {e}")

            try:
                es_quote = fetcher.fetch_future_quote("US", "ES")
            except Exception as e:
                logger.warning(f"SPY ETF 报价获取失败: {e}")

            # 分钟 K 线不再获取，fetch_future_kline 返回空列表
            try:
                nq_kline = fetcher.fetch_future_kline("US", "NQ", kType=2, limit=12)
            except Exception as e:
                logger.warning(f"QQQ K 线获取失败: {e}")

            try:
                es_kline = fetcher.fetch_future_kline("US", "ES", kType=2, limit=12)
            except Exception as e:
                logger.warning(f"SPY K 线获取失败: {e}")

            # ── 美股盘前价格：TickFlow 不支持盘前盘后，用 yfinance 补充 ──
            try:
                _progress("正在获取盘前实时价格...")
                pre_data = _fetch_premarket_from_yfinance(code)
                if pre_data:
                    stock_tick = {
                        "latest": pre_data["price"],
                        "volume": pre_data["volume"],
                        "timestamp": pre_data["timestamp"],
                        "trading_phase": 1,  # 盘前交易
                    }
                    stock_quote = {
                        "latest": pre_data["price"],
                        "open": pre_data["open"],
                        "high": pre_data["high"],
                        "low": pre_data["low"],
                        "prev_close": stock_quote.get("prev_close", pre_data.get("prev_close", 0)) if stock_quote else pre_data.get("prev_close", 0),
                        "change": pre_data["price"] - (stock_quote.get("prev_close", 0) if stock_quote else 0),
                        "change_pct": pre_data.get("change_pct", 0),
                        "volume": pre_data["volume"],
                        "amount": 0,
                        "timestamp": pre_data["timestamp"],
                        "status": 0,
                    }
                    logger.info(f"yfinance 盘前价格: {code}={pre_data['price']:.2f}")
                else:
                    logger.warning(f"yfinance 盘前数据为空 ({code})")
                    _progress("⚠️ yfinance 盘前数据不可用，成交量/价格可能不准确")
            except Exception as e:
                logger.warning(f"yfinance 盘前价格获取失败 ({code}): {e}")
                _progress("⚠️ yfinance 盘前数据获取失败，成交量/价格可能不准确")

            # ── QQQ/SPY 盘前价格同步用 yfinance ──
            try:
                qqq_pre = _fetch_premarket_from_yfinance("QQQ")
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
                    logger.info(f"yfinance QQQ 盘前: {qqq_pre['price']:.2f}")
            except Exception as e:
                logger.warning(f"yfinance QQQ 盘前失败: {e}")

            try:
                spy_pre = _fetch_premarket_from_yfinance("SPY")
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
                    logger.info(f"yfinance SPY 盘前: {spy_pre['price']:.2f}")
            except Exception as e:
                logger.warning(f"yfinance SPY 盘前失败: {e}")

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

        # 验证盘前时段
        if request.market == "US":
            if stock_tick and stock_tick.get("trading_phase") != 1:
                logger.warning(
                    f"当前不在盘前交易时段（te={stock_tick.get('trading_phase')}），"
                    f"盘前分析可能不准确"
                )

        # 隔夜新闻（含情感分析）
        try:
            overnight_news_list = fetch_news(
                name=info.name, code=code, market=request.market,
                news_token_us=settings.get("news_token_us", ""),
                news_token_a=settings.get("news_token_a", ""),
                limit=8,
            )
            if overnight_news_list:
                needs_analysis = [n for n in overnight_news_list if not n.sentiment]
                if needs_analysis:
                    from indicators.sentiment import analyze
                    analyzed = analyze(needs_analysis)
                    analyzed_map = {(n.date, n.title): n for n in analyzed}
                    overnight_news_list = [
                        analyzed_map.get((n.date, n.title), n)
                        for n in overnight_news_list
                    ]
        except Exception as e:
            logger.warning(f"隔夜新闻获取失败: {e}")

        if _stop(): return self._empty_response(code)

        # 构建期货数据
        futures_data = {}
        if nq_quote:
            nq_quote["kline_5min"] = nq_kline or []
        futures_data["NQ"] = nq_quote or {}
        if es_quote:
            es_quote["kline_5min"] = es_kline or []
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
        )
        if not report_content:
            report_content = "盘前报告生成失败，请稍后重试。"

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
            t1_date = self._previous_trading_day()
            eod_reports = Database().get_reports_by_code(
                code, mode="eod", since_hours=168,
            )
            for r in eod_reports:
                # 找到第一份真正 T-1 日期的报告（已按时间倒序，第一个=最新）
                report_date = (r.create_time or "")[:10]
                if report_date == t1_date:
                    eod_report = r.content
                    logger.info(f"复用 T-1({t1_date}) 盘后缓存报告: {r.create_time}, "
                                f"同日期共{sum(1 for x in eod_reports if (x.create_time or '')[:10]==t1_date)}份，取最新")
                    break
            if eod_reports and not eod_report:
                # 有缓存但不是 T-1 的 → 跳过，提示将重新生成
                latest = eod_reports[0]
                latest_date = (latest.create_time or "")[:10]
                logger.info(f"缓存报告日期({latest_date})≠T-1({t1_date})，弃用缓存，将重新生成 T-1 分析")

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

            prices = Database().get_prices(code, start, end)
            if not prices:
                fetcher = get_stock_fetcher(request.market)
                new_prices = fetcher.fetch_price_history(code, start, end)
                if new_prices:
                    Database().insert_prices(new_prices)
                prices = Database().get_prices(code, start, end)

            if not prices:
                logger.warning("T-1 日数据为空")
                return None

            df = pd.DataFrame([p.to_dict() for p in prices])
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            news_df = self._build_news_df(code)
            if news_df is None or news_df.empty:
                w_tech, w_news = 1.0, 0.0
            else:
                w_tech, w_news = 0.6, 0.4

            # 获取基本面数据
            fundamental_data = None
            try:
                from config.settings import Settings
                from alpha.fundamental import fetch_fundamental_factors
                settings = Settings()
                info = self._fetch_stock_info(code, request.market)
                fundamental_data = fetch_fundamental_factors(
                    name=info.name if info else code, code=code, market=request.market,
                    model=settings.get("llm_model", ""),
                    base_url=settings.get("llm_base_url", ""),
                    api_key=settings.get("llm_api_key", ""),
                    finnhub_token=settings.get("news_token_us", ""),
                )
            except Exception as e:
                logger.warning(f"基本面数据获取失败: {e}")

            market_type = detect_market(code) or request.market
            validation_mode = request.mode if request.mode in ("intraday", "pre") else "eod"
            pipeline_result = run_pipeline(
                df, news_df, market=market_type,
                w_tech=w_tech, w_news=w_news,
                validation_mode=validation_mode,
                fundamental_data=fundamental_data,
            )
            return pipeline_result
        except Exception as e:
            logger.error(f"T-1 日分析管道执行失败: {e}")
            return None

    def _generate_t1_report_text(
        self, code: str, info: StockInfo,
        pipeline_result: PipelineResult, period: str,
    ) -> str:
        """基于 pipeline_result 生成 T-1 日报告文本（调用完整报告生成流程）。"""
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
            )
            return content or ""
        except Exception as e:
            logger.error(f"生成 T-1 报告文本失败: {e}")
            return ""
