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
        df = self._fetch_prices(code, request.period)
        if df is None or df.empty:
            raise RuntimeError(f"无法获取 {code} 的股价数据")
        if _stop(): return self._empty_response(code)

        # ---- 4. 获取新闻情感数据 ----
        news_agg = self._fetch_news(code, request.market, _progress, info.name)
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

        # ---- 5. 执行分析管道 ----
        _progress("正在执行量化分析...")
        market_type = detect_market(code) or request.market
        pipeline_result = run_pipeline(
            df, news_df, market=market_type,
            w_tech=w_tech, w_news=w_news,
            fundamental_data=fundamental_data,
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

        # 盘口数据（实时信号，仅 itick）
        depth_factor = None
        realtime_quote = None
        if Settings().get("stock_data_token", ""):
            try:
                _progress("正在获取实时盘口...")
                from alpha.depth_factor import fetch_depth_factor
                depth_factor = fetch_depth_factor(
                    code, request.market, Settings().get("stock_data_token", ""),
                )
            except Exception as e:
                logger.warning(f"盘口数据获取失败: {e}")
            try:
                _progress("正在获取实时报价...")
                fetcher = get_stock_fetcher()
                realtime_quote = fetcher.fetch_quote(code) if hasattr(fetcher, 'fetch_quote') else None
            except Exception as e:
                logger.warning(f"实时报价获取失败: {e}")

        report_content = generate_report(
            info.to_dict(), tech, news_agg,
            pipeline_result.backtest, alpha_stats,
            data_range=data_range, depth_factor=depth_factor,
            validation=pipeline_result.validation,
            fundamental_data=pipeline_result.fundamental_data,
            rank_ic=pipeline_result.rank_ic,
            benchmark_return=pipeline_result.benchmark_return,
            realtime_quote=realtime_quote,
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
        fetcher = get_stock_fetcher()
        info = fetcher.fetch_stock_info(code)
        if info:
            Database().upsert_stock(info)
            logger.info(f"股票信息: {info.name} ({info.industry})")
            return info
        logger.warning(f"未获取到 {code} 的股票信息，使用默认名")
        return StockInfo(code=code, name=code, market=market)

    def _fetch_prices(self, code: str, period: str) -> pd.DataFrame | None:
        start, end = get_backtest_dates(period)
        prices = Database().get_prices(code, start, end)

        if not prices:
            # 缓存为空 → 全量拉取
            logger.info(f"缓存为空，联网拉取 {start}~{end}")
            fetcher = get_stock_fetcher()
            new_prices = fetcher.fetch_price_history(code, start, end)
            if new_prices:
                Database().insert_prices(new_prices)
            prices = Database().get_prices(code, start, end)
        else:
            last_date = prices[-1].date
            logger.info(f"缓存: {len(prices)} 条 ({prices[0].date}~{last_date})")

            # 增量拉取（itick 按 limit 返回最近 N 条，不会因周末/节假日返回空列表）
            from datetime import date as dt_date, timedelta
            next_day = (dt_date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
            today_str = dt_date.today().isoformat()
            logger.info(f"检查增量 {next_day}~{today_str}")
            fetcher = get_stock_fetcher()
            new_prices = fetcher.fetch_price_history(code, next_day, today_str)
            # 不能仅靠 new_prices 是否空判断——itick 按 limit 取最近 N 条，
            # 不会因为区间内无交易日就返回空列表。必须比较最新一条的日期。
            latest_new_date = max((p.date for p in new_prices), default="")
            if latest_new_date > last_date:
                Database().insert_prices(new_prices)
                prices = Database().get_prices(code, start, end)
                new_count = sum(1 for p in new_prices if p.date > last_date)
                logger.info(f"增量获取 {new_count} 条新数据（最新 {latest_new_date}）")
            else:
                logger.info(f"无增量数据（缓存最新 {last_date}，itick 最新 {latest_new_date or '无'}）")

        if not prices:
            return None

        df = pd.DataFrame([p.to_dict() for p in prices])
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    def _fetch_news(self, code: str, market: str, progress,
                    name: str = "") -> dict:
        progress("正在加载新闻...")
        from config.settings import Settings
        settings = Settings()
        news_list = fetch_news(
            name=name, code=code, market=market,
            news_token_us=settings.get("news_token_us", ""),
            news_token_a=settings.get("news_token_a", ""),
            limit=5,
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
                        content: str, chart_path: str) -> int | None:
        report = AnalysisReport(
            code=info.code, name=info.name, market=market,
            backtest_period=period,
            create_time=datetime.now().isoformat(),
            content=content,
            chart_path=chart_path or "",
        )
        return Database().insert_report(report)

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
        执行盘中快速分析。

        流程：
          1. 获取 T-1 日完整分析结果（DB 缓存 or 即时计算）
          2. 并行拉取实时数据（报价 + 盘口 + 增量新闻）
          3. 计算盘中快照
          4. 生成盘中报告（复用 T-1 报告第 1-7 章 + LLM 重写第 8 章）

        要求：配置 itick stock_data_token。
        """
        from config.settings import Settings
        settings = Settings()
        token = settings.get("stock_data_token", "")
        if not token:
            raise RuntimeError("盘中分析需要配置 itick stock_data_token，请在设置中填写。")

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
        t1_report_content, t1_pipeline_result = self._get_t1_analysis(
            code, request, _progress, _stop,
        )
        if _stop(): return self._empty_response(code)

        # ---- 4. 并行拉取实时数据 ----
        _progress("正在获取盘中实时数据...")
        fetcher = get_stock_fetcher()

        realtime_quote = None
        depth_factor = None
        stock_tick = None
        today_news_list = None

        # 实时报价
        try:
            realtime_quote = fetcher.fetch_quote(code)
        except Exception as e:
            logger.warning(f"实时报价获取失败: {e}")

        # 盘口数据
        try:
            from alpha.depth_factor import fetch_depth_factor
            depth_factor = fetch_depth_factor(code, request.market, token)
        except Exception as e:
            logger.warning(f"盘口数据获取失败: {e}")

        # 实时成交（校验交易时段）
        try:
            stock_tick = fetcher.fetch_stock_tick(code)
        except Exception as e:
            logger.warning(f"实时成交数据获取失败: {e}")

        # 增量新闻（今日）
        try:
            today_news_list = fetch_news(
                name=info.name, code=code, market=request.market,
                news_token_us=settings.get("news_token_us", ""),
                news_token_a=settings.get("news_token_a", ""),
                limit=5,
            )
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
        if t1_pipeline_result is None:
            # 没有 T-1 pipeline result → 跑一次简化版分析到 T-1 日
            _progress("正在计算 T-1 日分析...")
            t1_pipeline_result = self._run_eod_to_t1(code, request, _progress, _stop)
            if _stop(): return self._empty_response(code)
            # 也生成 T-1 报告文本
            if not t1_report_content:
                t1_report_content = self._generate_t1_report_text(
                    code, info, t1_pipeline_result, request.period,
                )

        snapshot = compute_intraday_snapshot(
            t1_pipeline_result,
            realtime_quote=realtime_quote,
            depth_factor=depth_factor,
            today_news=today_news_list,
            session=session,
        )
        if _stop(): return self._empty_response(code)

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
        )
        if not report_content:
            report_content = "盘中报告生成失败，请稍后重试。"

        _progress("盘中分析完成")
        return AnalysisResponse(
            stock_info=info,
            chart_path="",
            report_content=report_content,
            backtest_results=t1_pipeline_result.backtest if t1_pipeline_result else {},
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
        执行盘前分析（美股）。

        流程：
          1. 获取 T-1 日完整分析结果
          2. 并行拉取盘前数据（stock tick + 期货 quote + 期货 kline + 隔夜新闻）
          3. 计算盘前快照
          4. 生成盘前报告

        要求：
          - 配置 itick stock_data_token
          - 当前仅支持美股（US）
        """
        from config.settings import Settings
        settings = Settings()
        token = settings.get("stock_data_token", "")
        if not token:
            raise RuntimeError("盘前分析需要配置 itick stock_data_token，请在设置中填写。")

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
        t1_report_content, t1_pipeline_result = self._get_t1_analysis(
            code, request, _progress, _stop,
        )
        if _stop(): return self._empty_response(code)

        # ---- 4. 并行拉取盘前数据 ----
        _progress("正在获取盘前数据...")
        fetcher = get_stock_fetcher()

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

        # 验证盘前时段
        if stock_tick and stock_tick.get("trading_phase") != 1:
            logger.warning(
                f"当前不在盘前交易时段（te={stock_tick.get('trading_phase')}），"
                f"盘前分析可能不准确"
            )

        # 纳指期货报价
        try:
            nq_quote = fetcher.fetch_future_quote("US", "NQ")
        except Exception as e:
            logger.warning(f"纳指期货报价获取失败: {e}")

        # 标普期货报价
        try:
            es_quote = fetcher.fetch_future_quote("US", "ES")
        except Exception as e:
            logger.warning(f"标普期货报价获取失败: {e}")

        # 纳指期货 5 分钟 K 线（最近 12 根 = 1 小时）
        try:
            nq_kline = fetcher.fetch_future_kline("US", "NQ", kType=2, limit=12)
        except Exception as e:
            logger.warning(f"纳指期货 K 线获取失败: {e}")

        # 标普期货 5 分钟 K 线
        try:
            es_kline = fetcher.fetch_future_kline("US", "ES", kType=2, limit=12)
        except Exception as e:
            logger.warning(f"标普期货 K 线获取失败: {e}")

        # 隔夜新闻
        try:
            overnight_news_list = fetch_news(
                name=info.name, code=code, market=request.market,
                news_token_us=settings.get("news_token_us", ""),
                news_token_a=settings.get("news_token_a", ""),
                limit=8,
            )
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
            if not t1_report_content:
                t1_report_content = self._generate_t1_report_text(
                    code, info, t1_pipeline_result, request.period,
                )

        # ---- 6. 计算盘前快照 ----
        _progress("正在计算盘前快照...")
        snapshot = compute_premarket_snapshot(
            t1_pipeline_result,
            stock_tick=stock_tick or {},
            futures_data=futures_data,
            overnight_news=overnight_news_list,
            session="pre",
        )
        if _stop(): return self._empty_response(code)

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
        )
        if not report_content:
            report_content = "盘前报告生成失败，请稍后重试。"

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

    def _get_t1_analysis(
        self, code: str, request: AnalysisRequest,
        progress, stop,
    ) -> tuple[str | None, PipelineResult | None]:
        """
        尝试从 DB 缓存读取最近 24 小时内的盘后分析报告。

        Returns:
            (report_content, pipeline_result) — 可能为 None（无缓存）
        """
        try:
            reports = Database().get_reports_by_code(code)
            if reports:
                latest = reports[0]
                created = None
                if latest.create_time:
                    try:
                        created = datetime.fromisoformat(latest.create_time)
                    except Exception:
                        pass
                if created and (datetime.now() - created) < timedelta(hours=24):
                    logger.info(
                        f"复用 T-1 日缓存报告: {latest.create_time}, {len(latest.content)} chars"
                    )
                    return latest.content, None  # pipeline_result 不可从 content 恢复，传 None
        except Exception as e:
            logger.warning(f"读取 T-1 缓存报告失败: {e}")

        return None, None

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
                fetcher = get_stock_fetcher()
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

            market_type = detect_market(code) or request.market
            pipeline_result = run_pipeline(
                df, news_df, market=market_type,
                w_tech=w_tech, w_news=w_news,
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
                benchmark_return=pipeline_result.benchmark_return,
            )
            return content or ""
        except Exception as e:
            logger.error(f"生成 T-1 报告文本失败: {e}")
            return ""
