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
from data.stock_fetcher import get_stock_fetcher, fetch_cached_prices
from indicators.technical import summarize as summarize_technical
from utils.dates import get_backtest_dates
from utils.market import detect_market, search_us_stock_online, search_a_stock

logger = logging.getLogger(__name__)


def _build_portfolio_operation_summary(
    holdings_data: list[dict],
    watchlist_data: list[dict],
    market: str,
    balance,
) -> str:
    """构建组合级操作方案 Markdown 汇总。

    汇总所有持仓+关注股票的信号检查结果，生成组合级建议：
      - 哪些股票该加仓（有买入信号 + 审计 PASS）
      - 哪些该持有/观望（无信号）
      - 仓位分配建议
    """
    lines = [
        "\n---\n",
        "## 🎯 组合操作方案（代码生成）\n",
        f"> ⚠️ 以下方案由系统基于每只股票的策略审计和实时信号**自动生成**，非 LLM 建议。\n",
    ]

    all_data = holdings_data + watchlist_data
    buy_signals = []
    hold_signals = []

    for d in all_data:
        obj = d.get("holding") or d.get("watch_item")
        code = obj.code if obj else "?"
        name = obj.name if obj else code
        sc = d.get("signal_check") or []
        audit_summary = d.get("strategy_audit") or {}

        buys = [s for s in sc if s.get("signal") == "buy"]
        if buys:
            buy_signals.append({
                "code": code, "name": name,
                "price": d.get("current_price", 0),
                "buy_strategies": buys,
                "audit": audit_summary,
                "is_holding": d.get("holding") is not None,
            })
        else:
            hold_signals.append({
                "code": code, "name": name,
                "price": d.get("current_price", 0),
                "is_holding": d.get("holding") is not None,
            })

    # ── 有买入信号的股票 ──
    if buy_signals:
        lines.append("### 🟢 有买入信号的股票\n")
        lines.append("| 股票 | 现价 | 推荐策略 | 审计 | 操作 |")
        lines.append("|------|------|---------|------|------|")
        for bs in buy_signals:
            best = bs["buy_strategies"][0]
            audit_label = (
                f"PASS={bs['audit'].get('pass', 0)}, "
                f"FAIL={bs['audit'].get('fail', 0)}"
            )
            action = "加仓/买入" if bs["is_holding"] else "关注买入"
            lines.append(
                f"| {bs['name']}({bs['code']}) "
                f"| ${bs['price']:.2f} "
                f"| {best.get('name', '?')[:20]} "
                f"| {audit_label} "
                f"| **{action}** |"
            )
        lines.append("")

    # ── 无信号的股票 ──
    if hold_signals:
        lines.append("### ⚪ 观望中的股票\n")
        lines.append("| 股票 | 现价 | 状态 |")
        lines.append("|------|------|------|")
        for hs in hold_signals[:10]:
            action = "持有观望" if hs["is_holding"] else "等信号"
            lines.append(f"| {hs['name']}({hs['code']}) | ${hs['price']:.2f} | {action} |")
        lines.append("")

    # ── 组合级统计 ──
    total = len(all_data)
    buy_count = len(buy_signals)
    lines.append(f"**组合信号统计**: {buy_count}/{total} 只股票有买入信号\n")

    # ── 仓位建议 ──
    if buy_signals:
        available = (
            balance.us_balance if market == "US" else balance.a_balance
        )
        if available > 0:
            per_stock_pct = min(0.30, 1.0 / len(buy_signals))
            per_stock_cash = available * per_stock_pct
            lines.append(f"**仓位分配建议**: 可用资金 ${available:,.0f}，分散到 {len(buy_signals)} 只信号股，")
            lines.append(f"每只建议仓位约 {per_stock_pct*100:.0f}%（${per_stock_cash:,.0f}）。")
        lines.append("")

    lines.append("*以上方案由系统自动生成，具体执行请结合个人风险偏好。*\n")
    return "\n".join(lines)


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

        # ---- 0. 预测追踪：补验证历史预测 ----
        try:
            self.db.batch_verify_expired()
        except Exception as e:
            logger.warning(f"预测追踪验证失败: {e}")

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
                # 获取价格（复用公共缓存+增量更新函数）
                df = fetch_cached_prices(code, market, start, end,
                                         db=self.db, min_records=20)
                if df is None:
                    logger.warning(f"{code} 数据不足（<20条），跳过")
                    continue

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
                    from alpha.fundamental import get_fundamental_data
                    stock_name = ""
                    if is_holding and i < len(holdings):
                        stock_name = holdings[i].name
                    elif not is_holding:
                        watch_idx = i - len(holdings)
                        if watch_idx < len(watchlist):
                            stock_name = watchlist[watch_idx].name
                    fundamental_data = get_fundamental_data(stock_name, code, market)
                except Exception as e:
                    logger.warning(f"{code} 基本面数据获取失败: {e}")

                # 跑量化管道（跳过度参数，加速）
                result = run_pipeline(
                    df, news_df=news_df, market=market,
                    fundamental_data=fundamental_data,
                    skip_param_tuning=True,
                    stock_code=code,
                )


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
                    # 新架构字段
                    "operation_plan": getattr(result, "operation_plan", None),
                    "signal_check": getattr(result, "signal_check", None),
                    "strategy_audit": (
                        result.strategy_audit.summary
                        if getattr(result, "strategy_audit", None) else None
                    ),
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
        # 美股盘前/盘后 → Nasdaq.com 延伸时段（yfinance 降级）
        # 美股盘中 → TickFlow 实时数据
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
                        # 非盘中 → Nasdaq.com 延伸时段（自动降级 yfinance）
                        use_extended = session != "intraday"

                        if use_extended:
                            ext_data = fetch_us_extended_quote(code)
                            if ext_data and ext_data.get("price", 0) > 0:
                                rt_price = ext_data["price"]
                                rt_timestamp = ext_data.get("timestamp", 0)
                                rt_source = "Nasdaq.com延伸时段"
                                logger.info(
                                    f"延伸时段 ({code}): {rt_price:.2f}, session={session}"
                                )
                            else:
                                logger.warning(f"延伸时段数据为空 ({code})，回退到K线收盘价")
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

        # Step 1.8: 构建组合级操作方案汇总
        portfolio_plan = _build_portfolio_operation_summary(
            holdings_data, watchlist_data, market, balance
        )

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
            portfolio_operation_plan=portfolio_plan,
        )

        # Step 3: 拼接预测追踪尾部 + 存入 reports 表
        portfolio_code = f"PORTFOLIO_{market}"
        try:
            from report.prompts import build_prediction_footer
            port_stats = self.db.get_prediction_stats(portfolio_code)
            port_validated = self.db.get_validated_predictions(portfolio_code, limit=5)
            unverified = self.db.get_latest_unverified_prediction(portfolio_code)
            report_content += build_prediction_footer(
                portfolio_code, port_stats, port_validated,
                unverified_count=1 if unverified else 0)
        except Exception as e:
            logger.warning(f"组合预测 footer 构建失败: {e}")

        # 拼接策略健康度追踪（持续优化闭环 — 汇总所有持仓+关注）
        try:
            all_health = []
            for obj in (holdings_data + watchlist_data):
                item = obj.get("holding") or obj.get("watch_item")
                if item:
                    h = self.db.get_strategy_health_report(item.code)
                    for entry in h:
                        entry["stock_code"] = item.code
                    all_health.extend(h)
            if all_health:
                from report.prompts import build_strategy_health_section
                health_section = build_strategy_health_section(all_health[:20])
                if health_section:
                    report_content += health_section
        except Exception as e:
            logger.warning(f"组合策略健康度构建失败: {e}")

        market_label = "美股" if market == "US" else "A股"
        report = AnalysisReport(
            code=portfolio_code,
            name=f"{market_label}持仓综合分析",
            market=market,
            backtest_period=period,
            create_time=datetime.now().isoformat(),
            content=report_content,
            mode=mode,
        )
        report_id = self.db.insert_report(report)

        # 预测追踪：写入组合预测
        try:
            from services.analysis_service import _extract_direction
            direction = _extract_direction(report_content, 0.0)  # 组合无 Final_Score，默认 neutral
            from data.models import PredictionLog
            from datetime import datetime as _dt
            pred = PredictionLog(
                code=portfolio_code, market=market, mode=mode,
                report_id=report_id,
                predict_time=_dt.now().isoformat(),
                direction=direction,
                verify_after_days=7,
                key_reason=f"组合分析：{len(holdings)}只持仓+{len(watchlist)}只关注",
            )
            self.db.insert_prediction(pred)
        except Exception as e:
            logger.warning(f"组合预测写入失败: {e}")

        return {
            "report_content": report_content,
            "report_id": report_id,
        }
