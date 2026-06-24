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
from strategies.base import Position
from utils.dates import get_backtest_dates
from utils.market import detect_market, search_us_stock_online, search_a_stock

logger = logging.getLogger(__name__)


def _should_fetch_realtime_quote(market: str, mode: str) -> bool:
    """组合页是否需要拉取当前价。美股延伸时段不依赖 TickFlow token。"""
    if mode not in ("intraday", "pre"):
        return False
    if market == "US":
        return True
    return bool(Settings().get("stock_token_a", ""))


def _format_quote_time(timestamp: int | float) -> str:
    if timestamp and timestamp > 0:
        try:
            from datetime import datetime as dt
            return dt.fromtimestamp(float(timestamp) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return "实时"


def _fetch_portfolio_realtime_quote(code: str, market: str, fetcher) -> dict | None:
    """获取组合页使用的当前价，返回 {price, source, timestamp, session}。"""
    try:
        from utils.session import detect_session
        from data.stock_fetcher import fetch_us_extended_quote

        tick = None
        quote = None
        try:
            tick = fetcher.fetch_stock_tick(code) if hasattr(fetcher, "fetch_stock_tick") else None
        except Exception as e:
            logger.debug(f"{code} tick 获取失败: {e}")
        try:
            quote = fetcher.fetch_quote(code) if hasattr(fetcher, "fetch_quote") else None
        except Exception as e:
            logger.debug(f"{code} quote 获取失败: {e}")

        session = detect_session(market, stock_tick=tick, stock_quote=quote)

        if market == "US" and session != "intraday":
            ext_data = fetch_us_extended_quote(code)
            if ext_data and ext_data.get("price", 0) > 0:
                return {
                    "price": float(ext_data["price"]),
                    "timestamp": ext_data.get("timestamp", 0),
                    "source": "Nasdaq.com延伸时段",
                    "session": session,
                }

        if tick and tick.get("latest", 0) > 0:
            return {
                "price": float(tick["latest"]),
                "timestamp": tick.get("timestamp", 0),
                "source": "TickFlow实时报价",
                "session": session,
            }
        if quote and quote.get("latest", 0) > 0:
            return {
                "price": float(quote["latest"]),
                "timestamp": quote.get("timestamp", 0),
                "source": "实时报价",
                "session": session,
            }

        if market == "US":
            ext_data = fetch_us_extended_quote(code)
            if ext_data and ext_data.get("price", 0) > 0:
                return {
                    "price": float(ext_data["price"]),
                    "timestamp": ext_data.get("timestamp", 0),
                    "source": "Nasdaq.com报价",
                    "session": session,
                }
    except Exception as e:
        logger.warning(f"获取 {code} 当前价失败: {e}")
    return None


def _build_portfolio_operation_summary(
    holdings_data: list[dict],
    watchlist_data: list[dict],
    market: str,
    balance,
    account_equity: float,
) -> str:
    """构建组合级操作方案 Markdown 汇总。

    汇总每只股票已有的 operation_plan（来自 signal_check.py 的完整方案），
    生成组合级视图：有信号股票直接嵌入保守/激进方案，无信号股票嵌入候选策略+触发条件。
    """
    currency = "$" if market == "US" else "¥"
    lines = [
        "\n---\n",
        "## 🎯 组合操作方案（代码生成）\n",
        "> ⚠️ 以下方案由系统基于策略审计和实时信号**自动生成**，非 LLM 建议。\n",
        "> ⏱ 有效窗口: **5 个交易日** | 若到期未触发，下次分析时重新评估。\n",
    ]

    all_data = holdings_data + watchlist_data

    # ── 分类：有信号 vs 无信号 ──
    sell_stocks = []  # 持仓退出/减仓信号
    buy_stocks = []   # 有买入信号的股票
    hold_stocks = []  # 无信号的股票

    for d in all_data:
        obj = d.get("holding") or d.get("watch_item")
        code = obj.code if obj else "?"
        name = obj.name if obj else code
        sc_list = d.get("signal_check") or []
        op_plan = d.get("operation_plan")  # str（plan.markdown）或 None

        buys = [s for s in sc_list if s.get("signal") == "buy"]
        sells = [s for s in sc_list if s.get("signal") == "sell"]
        if sells:
            sell_stocks.append({
                "code": code, "name": name,
                "price": d.get("current_price", 0),
                "position_value": d.get("position_value", 0.0),
                "sell_count": len(sells),
                "op_plan": op_plan,
            })
        elif buys:
            top_buy = buys[0]
            buy_stocks.append({
                "code": code, "name": name,
                "price": d.get("current_price", 0),
                "position_value": d.get("position_value", 0.0),
                "target_pct": max(float(top_buy.get("position_pct", 0) or 0), 0.0),
                "buy_count": len(buys),
                "op_plan": op_plan,
            })
        else:
            hold_stocks.append({
                "code": code, "name": name,
                "price": d.get("current_price", 0),
                "op_plan": op_plan,
            })

    def _clean_plan_md(md: str) -> str:
        """清理 signal_check.py 生成的 markdown，去掉重复的大标题和分隔线。"""
        if not md:
            return ""
        md = md.strip()
        # 去掉开头的 --- 和 ## 🎯 系统操作方案 标题（已有外层标题）
        for prefix in ["---", "## 🎯 系统操作方案（代码生成）"]:
            if md.startswith(prefix):
                md = md[len(prefix):].strip()
        # 去掉 > ⚠️ 以下方案... 和 > ⏱ 有效窗口... 提示行（外层已有）
        for hint in [
            "> ⚠️ 以下方案由系统基于策略审计和实时信号**自动生成**，非 LLM 建议。",
            "> ⏱ 有效窗口: **5 个交易日**",
        ]:
            if md.startswith(hint):
                # 找到换行后截断
                nl = md.find("\n")
                if nl != -1:
                    md = md[nl+1:].strip()
        return md

    # ── 已持仓且有卖出信号的股票：优先展示 ──
    if sell_stocks:
        lines.append(f"### 🔴 持仓退出/减仓信号（{len(sell_stocks)} 只）\n")
        for ss in sell_stocks:
            lines.append(f"#### {ss['name']}（{ss['code']}）— 现价 {currency}{ss['price']:.2f}\n")
            plan_md = ss["op_plan"]
            if isinstance(plan_md, str) and plan_md.strip():
                lines.append(_clean_plan_md(plan_md))
            else:
                lines.append(f"> {ss['sell_count']} 个策略发出卖出信号，请检查持仓风险。\n")
            lines.append("")

    # ── 有买入信号的股票：直接嵌入各自的保守/激进方案 ──
    if buy_stocks:
        lines.append(f"### 🟢 有买入信号的股票（{len(buy_stocks)} 只）\n")
        for bs in buy_stocks:
            lines.append(f"#### {bs['name']}（{bs['code']}）— 现价 {currency}{bs['price']:.2f}\n")
            plan_md = bs["op_plan"]
            if isinstance(plan_md, str) and plan_md.strip():
                lines.append(_clean_plan_md(plan_md))
            else:
                lines.append(f"> {bs['buy_count']} 个策略发出买入信号，但系统未生成详细操作方案。\n")
            lines.append("")

    # ── 无信号的股票：嵌入候选策略 + 触发条件 ──
    if hold_stocks:
        lines.append(f"### ⚪ 无买入信号的股票（{len(hold_stocks)} 只）\n")
        for hs in hold_stocks:
            lines.append(f"#### {hs['name']}（{hs['code']}）— 现价 {currency}{hs['price']:.2f}\n")
            plan_md = hs["op_plan"]
            if isinstance(plan_md, str) and plan_md.strip():
                lines.append(_clean_plan_md(plan_md))
            else:
                lines.append("> 当前无策略发出买入信号，保持观望。\n")
            lines.append("")

    # ── 组合级仓位分配建议 ──
    total = len(all_data)
    buy_count = len(buy_stocks)
    sell_count = len(sell_stocks)
    lines.append(f"**组合信号统计**: {sell_count} 只持仓有退出/减仓信号，{buy_count}/{total} 只股票有买入信号\n")
    lines.append(f"**估算账户权益**: {currency}{account_equity:,.2f}\n")

    if buy_stocks:
        available = (
            balance.us_balance if market == "US" else balance.a_balance
        )
        lines.append(f"**可用资金**: {currency}{available:,.2f}\n")

        sizing_rows = []
        for bs in buy_stocks:
            target_pct = min(bs.get("target_pct", 0.0), 0.40)
            target_value = account_equity * target_pct
            current_value = float(bs.get("position_value", 0.0) or 0.0)
            need_cash = max(target_value - current_value, 0.0)
            sizing_rows.append((bs, target_pct, current_value, need_cash))

        total_need = sum(row[3] for row in sizing_rows)
        scale = min(1.0, available / total_need) if total_need > 0 and available > 0 else 0.0

        if total_need <= 0:
            lines.append("> 当前有买入信号的股票已达到或超过策略目标仓位，不建议继续加仓。\n")
        elif available > 0:
            lines.append("**资金分配建议**（按策略目标仓位，已扣除现有持仓市值）：\n")
            lines.append("| 股票 | 目标仓位 | 当前市值 | 建议新增金额 | 说明 |")
            lines.append("|------|------:|------:|------:|------|")
            for bs, target_pct, current_value, need_cash in sizing_rows:
                actual_cash = need_cash * scale
                note = "资金充足" if scale >= 0.999 else f"可用资金不足，按 {scale:.0%} 缩放"
                if need_cash <= 0:
                    note = "当前持仓已达到或超过目标仓位"
                lines.append(
                    f"| {bs['name']}（{bs['code']}） | {target_pct*100:.0f}% | "
                    f"{currency}{current_value:,.0f} | {currency}{actual_cash:,.0f} | {note} |"
                )
            lines.append("")
        else:
            lines.append("> ⚠️ 可用资金不足，如需买入请先卖出部分持仓。\n")
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
        price_frames: dict[str, pd.DataFrame] = {}
        quote_map: dict[str, dict] = {}

        should_fetch_quote = _should_fetch_realtime_quote(market, mode)
        if should_fetch_quote:
            if on_progress:
                on_progress("正在获取当前报价...")
            for code in all_codes:
                quote_data = _fetch_portfolio_realtime_quote(code, market, fetcher)
                if quote_data and quote_data.get("price", 0) > 0:
                    quote_map[code] = quote_data
            sessions = {str(q.get("session", "")) for q in quote_map.values() if q.get("session")}
            if mode == "intraday" and "intraday" not in sessions:
                raise RuntimeError(
                    f"当前不是常规盘中交易时段（检测到 {', '.join(sorted(sessions)) or '未知'}）。"
                    "请改用「盘后分析」或在盘前时段使用「盘前分析」。"
                )
            if mode == "pre" and "pre" not in sessions:
                raise RuntimeError(
                    f"当前不是盘前时段（检测到 {', '.join(sorted(sessions)) or '未知'}）。"
                    "请在常规交易时段使用「盘中分析」，收盘后使用「盘后分析」。"
                )

        # 先获取所有价格并估算市场当前账户权益。组合页有真实余额和持仓，
        # 信号仓位应按这个权益换算，而不是 Tab1 的 10 万参考账户。
        account_equity = float(balance.us_balance if market == "US" else balance.a_balance)
        for h in holdings:
            try:
                df_h = fetch_cached_prices(h.code, market, start, end,
                                           db=self.db, min_records=20)
                if df_h is not None:
                    price_frames[h.code] = df_h
                    latest_close = float(df_h["close"].iloc[-1])
                    mark_price = (
                        float(quote_map[h.code]["price"])
                        if h.code in quote_map and quote_map[h.code].get("price", 0) > 0
                        else latest_close if latest_close > 0
                        else float(h.cost_price or 0)
                    )
                else:
                    mark_price = (
                        float(quote_map[h.code]["price"])
                        if h.code in quote_map and quote_map[h.code].get("price", 0) > 0
                        else float(h.cost_price or 0)
                    )
                account_equity += float(h.shares or 0) * mark_price
            except Exception as e:
                logger.warning(f"{h.code} 组合权益估算失败，使用成本价: {e}")
                account_equity += float(h.shares or 0) * float(h.cost_price or 0)

        if account_equity <= 0:
            account_equity = 100000.0

        for i, code in enumerate(all_codes):
            is_holding = i < len(holdings)
            if on_progress:
                on_progress(f"正在分析 {code}（{i+1}/{len(all_codes)}）...")

            try:
                # 获取价格（复用公共缓存+增量更新函数）
                df = price_frames.get(code)
                if df is None:
                    df = fetch_cached_prices(code, market, start, end,
                                             db=self.db, min_records=20)
                    if df is not None:
                        price_frames[code] = df
                if df is None:
                    logger.warning(f"{code} 数据不足（<20条），跳过")
                    continue

                # 当前操作价：盘中/盘前优先用实时报价；否则用 K 线最后收盘价。
                latest_date = str(df["date"].iloc[-1].strftime("%Y-%m-%d"))
                latest_close = float(df["close"].iloc[-1])
                quote_data = quote_map.get(code)
                if quote_data and quote_data.get("price", 0) > 0:
                    current_price = float(quote_data["price"])
                    price_date = _format_quote_time(quote_data.get("timestamp", 0))
                    price_source = f"{quote_data.get('source', '实时报价')}（{price_date}）"
                else:
                    current_price = latest_close if latest_close > 0 else None
                    price_date = latest_date
                    price_source = f"K线收盘价（{latest_date}）"
                position_value = 0.0
                if is_holding and i < len(holdings) and current_price:
                    position_value = float(holdings[i].shares or 0) * float(current_price)

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

                # 无新闻数据时自动调整权重：技术面 100%，新闻面 0%
                # 否则 Final_Score 永远达不到策略阈值（与 Tab1 保持一致）
                if news_df is None or news_df.empty:
                    w_tech, w_news = 1.0, 0.0
                else:
                    w_tech, w_news = 0.6, 0.4

                current_position = None
                if is_holding and i < len(holdings):
                    holding = holdings[i]
                    shares = int(holding.shares or 0)
                    cost_price = float(holding.cost_price or 0)
                    if shares > 0:
                        highest_close = float(df["close"].max()) if "close" in df.columns else cost_price
                        current_position = Position(
                            shares=shares,
                            avg_cost=cost_price,
                            entry_date="",
                            entry_price=cost_price,
                            highest_close=max(highest_close, cost_price),
                            stop_loss=cost_price * 0.92 if cost_price > 0 else 0.0,
                        )

                # 跑量化管道（跳过度参数，加速）
                result = run_pipeline(
                    df, news_df=news_df, market=market,
                    initial_capital=account_equity,
                    account_equity=account_equity,
                    current_position=current_position,
                    current_price=current_price,
                    w_tech=w_tech, w_news=w_news,
                    fundamental_data=fundamental_data,
                    skip_param_tuning=True,
                    stock_code=code,
                )

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
                    "position_value": position_value,
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

        # Step 1.8: 构建组合级操作方案汇总
        portfolio_plan = _build_portfolio_operation_summary(
            holdings_data, watchlist_data, market, balance, account_equity
        )

        # Step 2: 生成报告（代码方案在前，LLM 翻译在后）
        if on_progress:
            on_progress("正在生成综合持仓报告...")

        balance_dict = {
            "us_balance": balance.us_balance,
            "a_balance": balance.a_balance,
        }

        # 先拼代码生成章节（操作方案 → LLM 解读之前展示）
        portfolio_code = f"PORTFOLIO_{market}"
        report_content = ""

        # 2a. 组合操作方案（代码生成，放在最前面）
        if portfolio_plan:
            report_content += portfolio_plan + "\n\n---\n\n"
            report_content += "> 📖 **以下为 AI 分析师的解读报告**，基于上述系统方案进行翻译和风险分析。\n\n"

        # 2b. LLM 报告（翻译代码方案为 K 线图语言）
        from report.generator import generate_portfolio_report
        llm_report = generate_portfolio_report(
            balance=balance_dict,
            holdings_data=holdings_data,
            watchlist_data=watchlist_data,
            market=market,
            period=period,
            mode=mode,
            portfolio_operation_plan=portfolio_plan,
        )
        report_content += llm_report

        # Step 3: 追加追踪章节到报告正文
        try:
            from report.prompts import build_prediction_footer
            port_stats = self.db.get_prediction_stats(portfolio_code)
            port_validated = self.db.get_validated_predictions(portfolio_code, limit=5)
            unverified = self.db.get_latest_unverified_prediction(portfolio_code)
            evaluation_panel = self.db.get_prediction_evaluation_panel(portfolio_code)
            report_content += build_prediction_footer(
                portfolio_code, port_stats, port_validated,
                unverified_count=1 if unverified else 0,
                evaluation_panel=evaluation_panel)
        except Exception as e:
            logger.warning(f"组合预测 footer 构建失败: {e}")

        # 3c. 策略健康度追踪（持续优化闭环 — 汇总所有持仓+关注）
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
                market_regime="portfolio",
            )
            self.db.insert_prediction(pred)
        except Exception as e:
            logger.warning(f"组合预测写入失败: {e}")

        return {
            "report_content": report_content,
            "report_id": report_id,
        }
