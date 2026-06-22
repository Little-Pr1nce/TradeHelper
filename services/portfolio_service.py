"""
持仓管理与综合分析服务。

负责：
  1. 持仓/关注/余额的 CRUD
  2. 股票代码搜索（自动识别市场 + 返显名称）
  3. 持仓综合分析：遍历持仓+关注 → 跑量化管道 → LLM 生成综合报告
"""

import logging
import time
from datetime import datetime
from typing import Any

import pandas as pd

from config.settings import Settings
from core.pipeline import run_pipeline
from data.database import Database
from data.models import Holding, WatchItem, AccountBalance, AnalysisReport
from data.stock_fetcher import get_stock_fetcher
from indicators.technical import summarize as summarize_technical
from utils.dates import get_backtest_dates
from utils.market import detect_market, search_us_stock_online, search_a_stock

logger = logging.getLogger(__name__)


class PortfolioService:
    """持仓管理和综合分析服务。"""

    def __init__(self):
        self.db = Database()

    # ======================== 股票搜索 ========================

    @staticmethod
    def search_stock(code: str) -> dict | None:
        """输入代码，自动识别市场并返回股票名称（优先中文名）。

        Returns:
            {"code": str, "name": str, "market": str} | None
        """
        from utils.market import search_us_stock_fallback

        code = code.strip().upper()
        market = detect_market(code)

        if not market:
            # 尝试模糊搜索
            results = search_us_stock_online(code) or search_a_stock(code)
            if results:
                return PortfolioService._prefer_chinese_name(results[0])
            return None

        if market == "US":
            # 优先在线搜索，再用本地中文名字典覆盖
            results = search_us_stock_online(code)
            if results:
                return PortfolioService._prefer_chinese_name(results[0])
            # 在线搜索失败 → 走本地字典
            fallback = search_us_stock_fallback(code)
            if fallback:
                return fallback[0]
        elif market == "A":
            results = search_a_stock(code)
            if results:
                return results[0]

        return {"code": code, "name": code, "market": market}

    @staticmethod
    def _prefer_chinese_name(result: dict) -> dict:
        """如果本地中文名字典有该股票的中文名，优先使用中文名。"""
        from utils.market import search_us_stock_fallback
        fallback = search_us_stock_fallback(result.get("code", ""))
        if fallback:
            # fallback[0]["name"] 的第一个元素就是中文名
            cn_name = fallback[0].get("name", "")
            if cn_name and any('一' <= ch <= '鿿' for ch in cn_name):
                result["name"] = cn_name
        return result

    # ======================== 持仓 CRUD ========================

    def list_holdings(self, market: str = "") -> list[Holding]:
        return self.db.list_holdings(market)

    def add_or_update_holding(self, code: str, name: str, market: str,
                              shares: float, cost_price: float):
        self.db.upsert_holding(Holding(
            code=code, name=name, market=market,
            shares=shares, cost_price=cost_price,
        ))

    def delete_holding(self, holding_id: int):
        self.db.delete_holding(holding_id)

    # ======================== 关注 CRUD ========================

    def list_watchlist(self, market: str = "") -> list[WatchItem]:
        return self.db.list_watchlist(market)

    def add_watch_item(self, code: str, name: str, market: str):
        self.db.upsert_watch_item(WatchItem(code=code, name=name, market=market))

    def delete_watch_item(self, item_id: int):
        self.db.delete_watch_item(item_id)

    # ======================== 余额 CRUD ========================

    def get_balance(self) -> AccountBalance:
        return self.db.get_balance()

    def save_balance(self, us_balance: float, a_balance: float):
        self.db.save_balance(AccountBalance(
            us_balance=us_balance, a_balance=a_balance,
        ))

    # ======================== 核心：持仓综合分析 ========================

    def analyze_portfolio(
        self,
        market: str,
        period: str = "1y",
        mode: str = "eod",
        on_progress=None,
    ) -> dict[str, Any]:
        """执行持仓综合分析。

        Args:
            market: 分析市场 ("US" | "A")
            period: 回测周期 ("6m" | "1y" | "3y")
            mode: 分析模式 ("eod" | "intraday" | "pre")
            on_progress: 进度回调 (msg: str) -> None

        Returns:
            {"report_content": str, "report_id": int | None}
        """
        holdings = self.db.list_holdings(market)
        watchlist = self.db.list_watchlist(market)
        balance = self.db.get_balance()

        if not holdings and not watchlist:
            raise ValueError(f"当前没有{market}持仓或关注股票，请先添加。")

        start, end = get_backtest_dates(period)
        fetcher = get_stock_fetcher(market)
        all_codes = [h.code for h in holdings] + [w.code for w in watchlist]

        # Step 1: 拉取所有股票的价格数据
        if on_progress:
            on_progress(f"正在获取 {len(all_codes)} 只股票的 K 线数据...")

        holdings_data = []
        watchlist_data = []

        for i, code in enumerate(all_codes):
            is_holding = i < len(holdings)
            if on_progress:
                on_progress(f"正在分析 {code}（{i+1}/{len(all_codes)}）...")

            try:
                # 获取价格（与 tab1 _fetch_prices 逻辑一致：缓存 + 增量更新）
                prices = self.db.get_prices(code, start, end)

                if not prices:
                    # 缓存为空 → 全量拉取
                    logger.info(f"{code} 缓存为空，联网拉取 {start}~{end}")
                    new_prices = fetcher.fetch_price_history(code, start, end)
                    if new_prices:
                        self.db.insert_prices(new_prices)
                    prices = self.db.get_prices(code, start, end)
                else:
                    last_date = prices[-1].date
                    logger.info(f"{code} 缓存: {len(prices)} 条 ({prices[0].date}~{last_date})")

                    # 增量拉取
                    from datetime import date as dt_date, timedelta
                    next_day = (dt_date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
                    today_str = dt_date.today().isoformat()
                    logger.info(f"{code} 检查增量 {next_day}~{today_str}")
                    new_prices = fetcher.fetch_price_history(code, next_day, today_str)
                    latest_new_date = max((p.date for p in new_prices), default="")
                    if latest_new_date > last_date:
                        self.db.insert_prices(new_prices)
                        prices = self.db.get_prices(code, start, end)
                        new_count = sum(1 for p in new_prices if p.date > last_date)
                        logger.info(f"{code} 增量获取 {new_count} 条新数据（最新 {latest_new_date}）")
                    else:
                        logger.info(f"{code} 无增量数据（缓存最新 {last_date}，数据源最新 {latest_new_date or '无'}）")

                if not prices or len(prices) < 20:
                    logger.warning(f"{code} 数据不足（<20条），跳过")
                    continue

                df = pd.DataFrame([p.to_dict() for p in prices])
                df["date"] = pd.to_datetime(df["date"])

                # 获取新闻（尝试从缓存）
                news_df = None
                try:
                    news_items = self.db.get_recent_news_with_sentiment(code, hours=72, limit=30)
                    if news_items:
                        news_df = pd.DataFrame([{
                            "date": n.date,
                            "title": n.title,
                            "sentiment": n.sentiment,
                            "confidence": n.confidence,
                            "finbert_score": (1.0 if n.sentiment == "positive" else
                                             -1.0 if n.sentiment == "negative" else 0.0),
                        } for n in news_items])
                except Exception:
                    pass

                # 获取基本面数据
                fundamental_data = None
                try:
                    from alpha.fundamental import fetch_fundamental_factors
                    settings = Settings()
                    # 从 holding/watch_item 中取股票名称
                    stock_name = ""
                    if is_holding and i < len(holdings):
                        stock_name = holdings[i].name
                    elif not is_holding:
                        watch_idx = i - len(holdings)
                        if watch_idx < len(watchlist):
                            stock_name = watchlist[watch_idx].name
                    fundamental_data = fetch_fundamental_factors(
                        name=stock_name, code=code, market=market,
                        model=settings.get("llm_model", ""),
                        base_url=settings.get("llm_base_url", ""),
                        api_key=settings.get("llm_api_key", ""),
                        finnhub_token=settings.get("news_token_us", ""),
                    )
                except Exception as e:
                    logger.warning(f"{code} 基本面数据获取失败: {e}")

                # 跑量化管道（跳过度参数，加速）
                result = run_pipeline(
                    df, news_df=news_df, market=market,
                    fundamental_data=fundamental_data,
                    skip_param_tuning=True,
                )

                # API 调用间短暂延迟，避免触发频率限制
                time.sleep(0.5)

                # 提取 K 线最新收盘价及日期
                latest_date = str(df["date"].iloc[-1].strftime("%Y-%m-%d"))
                latest_close = float(df["close"].iloc[-1])
                current_price = latest_close if latest_close > 0 else None
                price_date = latest_date
                price_source = f"K线收盘价（{latest_date}）"

                # 提取 Alpha 最新得分
                latest_score = None
                if "Final_Score" in result.df.columns and not result.df["Final_Score"].dropna().empty:
                    latest_score = float(result.df["Final_Score"].dropna().iloc[-1])

                # 提取技术面摘要
                tech_summary = summarize_technical(result.df, name=code)

                # 提取新闻摘要
                news_summary = ""
                if news_df is not None and not news_df.empty:
                    pos = int((news_df["finbert_score"] > 0.1).sum())
                    neg = int((news_df["finbert_score"] < -0.1).sum())
                    neu = len(news_df) - pos - neg
                    news_summary = f"新闻 {len(news_df)} 条：正面 {pos}、负面 {neg}、中性 {neu}"

                # 提取行情状态
                market_regime = getattr(result, "market_regime", "unknown") or "unknown"
                regime_labels = {
                    "trending_volatile": "强趋势+高波动",
                    "trending_steady": "慢涨/弱趋势",
                    "trending": "趋势市",
                    "ranging": "震荡市",
                    "transitional": "趋势形成中",
                }
                regime_label = regime_labels.get(market_regime, market_regime)

                # 提取 Rank IC（Alpha 因子有效性）
                rank_ic_info = ""
                rank_ic = getattr(result, "rank_ic", None) or {}
                if rank_ic.get("rank_ic_mean") is not None:
                    ic = rank_ic["rank_ic_mean"]
                    ic_ir = rank_ic.get("ic_ir", 0)
                    judgement = "有效正向" if ic > 0.05 else ("有效反向" if ic < -0.05 else "预测力弱")
                    rank_ic_info = f"Rank IC = {ic:+.4f}（{judgement}），IC_IR = {ic_ir:.2f}"

                # 提取基本面数据
                fund_info = ""
                fd = getattr(result, "fundamental_data", None) or {}
                if fd:
                    sf = fd.get("style_factors") or {}
                    ff = fd.get("fundamental_factors") or {}
                    parts = []
                    if sf.get("pe_percentile") is not None:
                        parts.append(f"PE分位={sf['pe_percentile']:.1%}")
                    if sf.get("pb_percentile") is not None:
                        parts.append(f"PB分位={sf['pb_percentile']:.1%}")
                    if ff.get("roe") is not None:
                        parts.append(f"ROE={ff['roe']:.1%}")
                    if ff.get("gross_margin") is not None:
                        parts.append(f"毛利率={ff['gross_margin']:.1%}")
                    if parts:
                        fund_info = "基本面：" + "、".join(parts)

                # 提取基准收益
                benchmark_return = getattr(result, "benchmark_return", 0) or 0

                # 提取策略适配信息
                active_strats = getattr(result, "active_strategies", []) or []
                skipped_strats = getattr(result, "skipped_strategies", []) or []
                regime_adapt_info = ""
                if active_strats or skipped_strats:
                    regime_adapt_info = f"行情={regime_label}（{market_regime}），适配策略数={len(active_strats)}，跳过策略数={len(skipped_strats)}"

                item_data = {
                    "current_price": current_price,
                    "price_date": price_date,
                    "price_source": price_source,
                    "technical": tech_summary,
                    "backtest": result.backtest,
                    "alpha_score": latest_score,
                    "news_summary": news_summary,
                    "market_regime": regime_label,
                    "rank_ic_info": rank_ic_info,
                    "fund_info": fund_info,
                    "benchmark_return": benchmark_return,
                    "regime_adapt_info": regime_adapt_info,
                }

                if is_holding:
                    item_data["holding"] = holdings[i]
                    holdings_data.append(item_data)
                else:
                    # 关注股票索引需要减去持仓数量
                    watch_idx = i - len(holdings)
                    if watch_idx < len(watchlist):
                        item_data["watch_item"] = watchlist[watch_idx]
                        watchlist_data.append(item_data)

            except Exception as e:
                logger.error(f"分析 {code} 失败: {e}")
                continue

        if not holdings_data and not watchlist_data:
            raise RuntimeError("所有股票的数据获取均失败，无法生成报告。")

        # Step 1.5: 获取最新价格（覆盖 K 线收盘价）
        # 美股盘前 → yfinance 延伸时段（盘前可获取）
        # 美股盘中 → TickFlow 实时数据
        # 美股盘后/EOD → 保持 K 线收盘价（yfinance 夜盘拿不到）
        # A 股 → TickFlow 实时行情（如有 token）
        should_fetch_quote = mode in ("intraday", "pre") and Settings().get(
            "stock_token_us" if market == "US" else "stock_token_a", ""
        )
        if should_fetch_quote:
            if on_progress:
                on_progress("正在获取实时报价...")
            from utils.session import detect_session
            from data.stock_fetcher import fetch_us_extended_quote

            all_item_data = holdings_data + watchlist_data
            for item_data in all_item_data:
                obj = item_data.get("holding") or item_data.get("watch_item")
                if not obj:
                    continue
                code = obj.code
                try:
                    rt_price = None
                    rt_timestamp = 0
                    rt_source = ""

                    # 先尝试 TickFlow 获取 tick/quote（用于检测交易时段）
                    tick = fetcher.fetch_stock_tick(code) if hasattr(fetcher, 'fetch_stock_tick') else None
                    quote = fetcher.fetch_quote(code) if hasattr(fetcher, 'fetch_quote') else None

                    if market == "US":
                        # 检测当前交易时段
                        session = detect_session("US", stock_tick=tick, stock_quote=quote)
                        # 只有盘前用 yfinance（夜盘/盘后 yfinance 拿不到）
                        use_yfinance = session == "pre"

                        if use_yfinance:
                            yf_data = fetch_us_extended_quote(code)
                            if yf_data and yf_data.get("price", 0) > 0:
                                rt_price = yf_data["price"]
                                rt_timestamp = yf_data.get("timestamp", 0)
                                rt_source = f"yfinance盘前数据"
                                logger.info(
                                    f"yfinance 盘前 ({code}): {rt_price:.2f}, session={session}"
                                )
                            else:
                                logger.warning(f"yfinance 盘前数据为空 ({code})，回退到K线收盘价")
                        elif session == "intraday":
                            # 盘中 → TickFlow 实时数据
                            if tick and tick.get("latest", 0) > 0:
                                rt_price = tick["latest"]
                                rt_timestamp = tick.get("timestamp", 0)
                                rt_source = "TickFlow实时报价"
                                logger.info(f"TickFlow 实时报价 ({code}): {rt_price:.2f}")
                        else:
                            # 盘后/休市 → 保持 K 线收盘价
                            pass
                    else:
                        # A 股：TickFlow 实时行情
                        if tick and tick.get("latest", 0) > 0:
                            rt_price = tick["latest"]
                            rt_timestamp = tick.get("timestamp", 0)
                            rt_source = "TickFlow实时报价"
                        elif quote and quote.get("latest", 0) > 0:
                            rt_price = quote["latest"]
                            rt_timestamp = quote.get("timestamp", 0)
                            rt_source = "实时行情"

                    if rt_price and rt_price > 0:
                        from datetime import datetime as dt
                        if rt_timestamp > 0:
                            ts_str = dt.fromtimestamp(rt_timestamp / 1000).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                        else:
                            ts_str = "实时"
                        item_data["current_price"] = rt_price
                        item_data["price_date"] = ts_str
                        item_data["price_source"] = f"{rt_source}（{ts_str}）"
                except Exception as e:
                    logger.warning(f"获取 {code} 实时报价失败: {e}")

        # Step 2: 生成报告
        if on_progress:
            on_progress("正在生成综合持仓报告...")

        balance_dict = {
            "us_balance": balance.us_balance,
            "a_balance": balance.a_balance,
        }

        from report.generator import generate_portfolio_report
        report_content = generate_portfolio_report(
            balance=balance_dict,
            holdings_data=holdings_data,
            watchlist_data=watchlist_data,
            market=market,
            period=period,
            mode=mode,
        )

        # Step 3: 存入 reports 表（以变现在历史报告中展示）
        market_label = "美股" if market == "US" else "A股"
        report = AnalysisReport(
            code=f"PORTFOLIO_{market}",
            name=f"{market_label}持仓综合分析",
            market=market,
            backtest_period=period,
            create_time=datetime.now().isoformat(),
            content=report_content,
            mode=mode,
        )
        report_id = self.db.insert_report(report)

        return {
            "report_content": report_content,
            "report_id": report_id,
        }
