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
from datetime import datetime
from typing import Callable

import pandas as pd

from data.database import Database
from data.stock_fetcher import get_stock_fetcher
from data.news_fetcher import fetch_news
from data.models import StockInfo, PriceData, AnalysisReport
from indicators.technical import calc_all_indicators, summarize
from indicators.sentiment import analyze, aggregate
from indicators.constants import NEWS_CACHE_HOURS, NEWS_FETCH_LIMIT
from core.pipeline import run_pipeline, AnalysisResult as PipelineResult
from report.chart import generate_kline_chart
from report.generator import generate_report
from utils.dates import get_backtest_dates
from utils.market import detect_market, search_a_stock, search_a_stock_fallback
from utils.market import search_us_stock_online, search_us_stock_fallback

logger = logging.getLogger(__name__)


@dataclass
class AnalysisRequest:
    """分析请求参数。"""
    raw_input: str         # 用户原始输入（代码或名称）
    market: str            # "A" / "US"
    period: str            # "3m" / "6m" / "1y" / "3y"
    data_source: str = "free"  # "free" / "custom"（付费数据源）


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
        info = self._fetch_stock_info(code, request.market, request.data_source)
        if _stop(): return self._empty_response(code)

        # ---- 3. 获取股价数据（含缓存） ----
        _progress("正在获取股价数据...")
        df = self._fetch_prices(code, request.period, request.data_source)
        if df is None or df.empty:
            raise RuntimeError(f"无法获取 {code} 的股价数据")
        if _stop(): return self._empty_response(code)

        # ---- 4. 获取新闻情感数据 ----
        news_agg = self._fetch_news(code, request.market, _progress)
        if _stop(): return self._empty_response(code)

        news_df = self._build_news_df(code)

        # ---- 5. 执行分析管道 ----
        _progress("正在执行量化分析...")
        market_type = detect_market(code) or request.market
        pipeline_result = run_pipeline(df, news_df, market=market_type)
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
        report_content = generate_report(
            info.to_dict(), tech, news_agg,
            pipeline_result.backtest, alpha_stats,
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

    def _fetch_stock_info(self, code: str, market: str, data_source: str) -> StockInfo:
        fetcher = get_stock_fetcher(data_source)
        info = fetcher.fetch_stock_info(code)
        if info:
            Database().upsert_stock(info)
            logger.info(f"股票信息: {info.name} ({info.industry})")
            return info
        logger.warning(f"未获取到 {code} 的股票信息，使用默认名")
        return StockInfo(code=code, name=code, market=market)

    def _fetch_prices(self, code: str, period: str, data_source: str = "free") -> pd.DataFrame | None:
        start, end = get_backtest_dates(period)
        prices = Database().get_prices(code, start, end)
        logger.info(f"缓存数据: {len(prices)} 条 ({start}~{end})")

        if self._cache_needs_refresh(prices, start, end):
            logger.info(f"缓存不足，联网拉取")
            fetcher = get_stock_fetcher(data_source)
            new_prices = fetcher.fetch_price_history(code, start, end)
            if new_prices:
                Database().insert_prices(new_prices)
                prices = Database().get_prices(code, start, end)
                logger.info(f"联网获取 {len(new_prices)} 条，合计 {len(prices)} 条")
        else:
            if prices:
                logger.info(f"使用缓存（最新 {prices[-1].date}）")

        if not prices:
            return None

        df = pd.DataFrame([p.to_dict() for p in prices])
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    @staticmethod
    def _cache_needs_refresh(prices, start: str, end: str) -> bool:
        from datetime import date, timedelta
        if len(prices) < 5:
            return True
        first_date = prices[0].date
        last_date = prices[-1].date
        if first_date and start and first_date > start:
            try:
                d_first = date.fromisoformat(first_date)
                d_start = date.fromisoformat(start)
                if (d_first - d_start).days > 7:
                    return True
            except ValueError:
                pass
        if last_date < (date.today() - timedelta(days=7)).isoformat():
            return True
        return False

    def _fetch_news(self, code: str, market: str, progress) -> dict:
        progress("正在加载新闻...")
        cached = Database().get_recent_news_with_sentiment(
            code, hours=NEWS_CACHE_HOURS, limit=NEWS_FETCH_LIMIT
        )
        if cached:
            logger.info(f"命中新闻缓存 {len(cached)} 条")
            return aggregate(cached)

        news_list = []
        try:
            progress("正在获取新闻...")
            news_list = fetch_news(code, market, limit=NEWS_FETCH_LIMIT)
        except Exception as e:
            logger.warning(f"新闻抓取失败: {e}")
        logger.info(f"新闻: {len(news_list)} 条")

        if news_list:
            progress("正在进行新闻情感分析...")
            news_list = analyze(news_list)
            try:
                Database().insert_news(news_list)
            except Exception as e:
                logger.warning(f"新闻持久化失败: {e}")

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
