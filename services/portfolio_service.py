"""
持仓管理与综合分析服务。

负责：
  1. 持仓/关注/余额的 CRUD
  2. 股票代码搜索（自动识别市场 + 返显名称）
  3. 持仓综合分析：遍历持仓+关注 → 跑量化管道 → LLM 生成综合报告
"""

import logging
import math
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pandas as pd

from config.settings import Settings
from core.data_quality import evaluate_extended_liquidity_proxy
from core.pipeline import run_pipeline
from core.signal_check import select_actionable_sell_signals
from data.database import Database
from data.models import Holding, WatchItem, AccountBalance, AnalysisReport
from data.stock_fetcher import (
    get_stock_fetcher,
    fetch_cached_prices,
    resolve_listing_date,
)
from indicators.technical import summarize as summarize_technical
from strategies.base import Position
from services.news_service import (
    analyze_and_store_news,
    fetch_stock_news_items,
    news_items_to_df,
)
from utils.dates import get_backtest_dates
from utils.market import (
    detect_market,
    search_a_stock,
    search_a_stock_fallback,
    search_us_stock_online,
)

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
            return dt.fromtimestamp(_normalize_epoch_seconds(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return "实时"


def _normalize_epoch_seconds(timestamp: int | float) -> float:
    try:
        value = float(timestamp or 0)
    except (TypeError, ValueError):
        return 0.0
    return value / 1000.0 if value > 10_000_000_000 else value


def _evaluate_realtime_quote_quality(
    quote: dict | None,
    mode: str,
    *,
    now_epoch: float | None = None,
) -> dict:
    """逐股实时报价闸门，避免组合中部分成功掩盖单股失败。"""
    required = mode in ("intraday", "pre")
    price = float((quote or {}).get("price", 0.0) or 0.0)
    timestamp = _normalize_epoch_seconds((quote or {}).get("timestamp", 0))
    max_age = 15 * 60 if mode == "intraday" else 45 * 60
    now_value = float(now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp())
    age_seconds = now_value - timestamp if timestamp > 0 else None
    fresh = bool(
        timestamp > 0
        and age_seconds is not None
        and -300 <= age_seconds <= max_age
    )
    open_p = float((quote or {}).get("open", 0.0) or 0.0)
    high_p = float((quote or {}).get("high", 0.0) or 0.0)
    low_p = float((quote or {}).get("low", 0.0) or 0.0)
    ohlc_complete = bool(
        min(open_p, high_p, low_p, price) > 0
        and high_p >= max(open_p, low_p, price)
        and low_p <= min(open_p, high_p, price)
    )
    issues = []
    warnings = []
    if required and price <= 0:
        issues.append("当前时段实时报价缺失")
    elif required and not fresh:
        if timestamp <= 0:
            issues.append("实时报价缺少可验证时间戳")
        else:
            issues.append(f"实时报价已过期（约{max(age_seconds or 0, 0)/60:.0f}分钟）")
    if required and price > 0 and fresh and not ohlc_complete:
        warnings.append("实时价可用，但盘中OHLC不完整，不确认冲高回落/日内触线形态")
    liquidity = evaluate_extended_liquidity_proxy(quote, mode)
    if required and liquidity.get("applied") and liquidity.get("warning"):
        warnings.append(str(liquidity["warning"]))
    return {
        "required": required,
        "available": price > 0,
        "fresh": fresh,
        "ohlc_complete": ohlc_complete,
        "age_seconds": age_seconds,
        "issues": issues,
        "warnings": warnings,
        "liquidity_proxy": (
            liquidity.get("proxy") if liquidity.get("applied") else ""
        ),
        "liquidity_position_multiplier": liquidity.get("position_multiplier", 1.0),
        "spread_pct": liquidity.get("spread_pct"),
    }


def _quote_payload(
    *,
    price: float,
    source: str,
    session: str,
    timestamp: int | float,
    bar: dict | None,
) -> dict:
    """保留报价中已取得的 OHLCV，缺失字段不伪造。"""
    source_bar = bar or {}
    return {
        "price": float(price),
        "latest": float(price),
        "open": float(source_bar.get("open", 0.0) or 0.0),
        "high": float(source_bar.get("high", 0.0) or 0.0),
        "low": float(source_bar.get("low", 0.0) or 0.0),
        "volume": float(source_bar.get("volume", 0.0) or 0.0),
        "prev_close": float(source_bar.get("prev_close", 0.0) or 0.0),
        "bid": float(source_bar.get("bid", 0.0) or 0.0),
        "ask": float(source_bar.get("ask", 0.0) or 0.0),
        "bid_size": float(source_bar.get("bid_size", 0.0) or 0.0),
        "ask_size": float(source_bar.get("ask_size", 0.0) or 0.0),
        "timestamp": timestamp or source_bar.get("timestamp", 0),
        "source": source,
        "session": session,
    }


def _top_candidate_signal(signals: list[dict]) -> dict:
    """从 signal_check 结果中提取最接近触发的候选策略，用于组合摘要表。"""
    if not signals:
        return {}
    ranked = sorted(
        signals,
        key=lambda s: float(s.get("rank_score", 0) or 0),
        reverse=True,
    )
    top = ranked[0]
    audit = top.get("audit") or top.get("audit_verdict") or "—"
    audit_label = {"PASS": "✅", "CONDITIONAL": "⚠️"}.get(audit, audit)
    reason = str(top.get("no_signal_reason") or top.get("reason") or "等待策略条件确认")
    if len(reason) > 70:
        reason = reason[:67] + "..."
    return {
        "name": str(top.get("name") or top.get("strategy_name") or "—")[:24],
        "audit": audit_label,
        "reason": reason,
    }


def _portfolio_risk_overlay(item: dict, account_equity: float) -> dict | None:
    """组合层持仓风控覆盖，不依赖单个策略是否生成 sell 订单。"""
    holding = item.get("holding")
    if not holding:
        return None

    price = float(item.get("current_price") or 0)
    cost = float(getattr(holding, "cost_price", 0) or 0)
    shares = float(getattr(holding, "shares", 0) or 0)
    if price <= 0 or cost <= 0 or shares <= 0:
        return None

    pnl_pct = (price - cost) / cost
    position_value = price * shares
    weight = position_value / account_equity if account_equity and account_equity > 0 else 0.0
    alpha = item.get("alpha_score")
    alpha = float(alpha) if alpha is not None else 0.0

    bearish_reasons = []
    if pnl_pct <= -0.20:
        bearish_reasons.append(f"浮亏 {pnl_pct:+.1%} 超过 20% 风险线")
    elif pnl_pct <= -0.08:
        bearish_reasons.append(f"浮亏 {pnl_pct:+.1%} 跌破 8% 止损线")

    if alpha <= -0.30:
        bearish_reasons.append(f"Alpha={alpha:+.3f} 明显偏空")
    elif alpha <= -0.12:
        bearish_reasons.append(f"Alpha={alpha:+.3f} 偏空")

    tech = item.get("technical") or ""
    if "空头" in tech or "死叉" in tech:
        bearish_reasons.append("技术面出现空头/死叉描述")

    if weight >= 0.30 and (alpha < 0 or pnl_pct < 0):
        bearish_reasons.append(f"单票仓位 {weight:.1%} 过高且缺少正向确认")
    elif weight >= 0.20 and alpha <= -0.20:
        bearish_reasons.append(f"单票仓位 {weight:.1%} 偏高且 Alpha 偏空")

    if pnl_pct >= 0.15 and (alpha < -0.10 or "空头" in tech or "死叉" in tech):
        bearish_reasons.append(f"已有 {pnl_pct:+.1%} 浮盈但趋势转弱，优先保护利润")

    marker = item.get("technical_marker") or {}
    high = float(marker.get("high") or 0)
    close = float(marker.get("close") or price)
    high_120 = float(marker.get("high_120") or 0)
    if high > 0 and close > 0 and pnl_pct >= 0.10:
        pullback_from_high = (close - high) / high
        is_near_period_high = high_120 > 0 and high >= high_120 * 0.995
        if is_near_period_high and pullback_from_high <= -0.035:
            bearish_reasons.append(
                f"当日最高 {high:.2f} 接近120日高点，收盘回落 {pullback_from_high:.1%}，浮盈需锁定"
            )

    if not bearish_reasons:
        return None

    profit_lock = any("浮盈需锁定" in r or "优先保护利润" in r for r in bearish_reasons)
    if pnl_pct <= -0.20 and alpha <= -0.12:
        action = "🔴 建议清仓/强制复核"
    elif pnl_pct <= -0.08 or alpha <= -0.30 or weight >= 0.30:
        action = "🟠 建议减仓/降风险"
    elif profit_lock:
        action = "🟡 建议部分止盈/上移止损"
    else:
        action = "🟡 建议设硬止损"

    return {
        "action": action,
        "pnl_pct": pnl_pct,
        "weight": weight,
        "reason": "；".join(bearish_reasons[:3]),
    }


def _compute_portfolio_risk_snapshot(
    holdings_data: list[dict],
    price_frames: dict[str, pd.DataFrame] | None,
    account_equity: float,
) -> dict:
    """计算组合集中度、相关性、波动和新增仓位容量。"""
    weights: dict[str, float] = {}
    for item in holdings_data:
        holding = item.get("holding")
        if not holding or account_equity <= 0:
            continue
        price = float(item.get("current_price", 0.0) or 0.0)
        shares = float(getattr(holding, "shares", 0.0) or 0.0)
        if price > 0 and shares > 0:
            weights[holding.code] = price * shares / account_equity

    gross_exposure = sum(weights.values())
    max_code = max(weights, key=weights.get) if weights else ""
    max_weight = weights.get(max_code, 0.0)
    hhi = sum(weight * weight for weight in weights.values())
    correlations: dict[tuple[str, str], float] = {}
    high_corr_pairs: list[dict] = []
    annualized_vol = 0.0

    series = {}
    analysis_codes = sorted(set(weights) | set((price_frames or {}).keys()))
    for code in analysis_codes:
        frame = (price_frames or {}).get(code)
        if frame is None or frame.empty or not {"date", "close"}.issubset(frame.columns):
            continue
        data = frame[["date", "close"]].copy().tail(90)
        data["date"] = pd.to_datetime(data["date"], errors="coerce")
        data["close"] = pd.to_numeric(data["close"], errors="coerce")
        data = data.dropna().drop_duplicates("date").set_index("date")
        returns = data["close"].pct_change().dropna()
        if len(returns) >= 20:
            series[code] = returns.rename(code)

    if series:
        aligned = pd.concat(series.values(), axis=1, join="inner").dropna()
        if len(aligned) >= 20:
            corr = aligned.corr()
            codes = list(aligned.columns)
            for i, left in enumerate(codes):
                for right in codes[i + 1:]:
                    value = float(corr.loc[left, right])
                    correlations[(left, right)] = value
                    correlations[(right, left)] = value
                    combined_weight = weights.get(left, 0.0) + weights.get(right, 0.0)
                    if (
                        left in weights and right in weights
                        and value >= 0.75 and combined_weight >= 0.20
                    ):
                        high_corr_pairs.append({
                            "left": left,
                            "right": right,
                            "correlation": value,
                            "combined_weight": combined_weight,
                        })
            covariance = aligned.cov() * 252
            ordered_weights = pd.Series(
                {code: weights.get(code, 0.0) for code in aligned.columns}
            )
            variance = float(ordered_weights.T.dot(covariance).dot(ordered_weights))
            annualized_vol = math.sqrt(max(variance, 0.0))

    risk_flags = []
    if max_weight >= 0.30:
        risk_flags.append(f"{max_code} 单票占比 {max_weight:.1%}，超过30%红线")
    elif max_weight >= 0.20:
        risk_flags.append(f"{max_code} 单票占比 {max_weight:.1%}，集中度偏高")
    if hhi >= 0.25:
        risk_flags.append(f"持仓集中度 HHI={hhi:.2f}，组合分散不足")
    if annualized_vol >= 0.35:
        risk_flags.append(f"估算年化波动 {annualized_vol:.1%}，组合波动偏高")
    if high_corr_pairs:
        risk_flags.append(f"存在 {len(high_corr_pairs)} 组高相关重仓")

    return {
        "weights": weights,
        "gross_exposure": gross_exposure,
        "max_code": max_code,
        "max_weight": max_weight,
        "hhi": hhi,
        "annualized_vol": annualized_vol,
        "correlations": correlations,
        "high_corr_pairs": high_corr_pairs,
        "risk_flags": risk_flags,
        "new_position_capacity_pct": max(0.0, 0.90 - gross_exposure),
    }


def _estimate_account_equity(
    holdings: list,
    cash_balance: float,
    price_frames: dict[str, pd.DataFrame],
    quote_map: dict[str, dict] | None = None,
    quote_quality_map: dict[str, dict] | None = None,
) -> tuple[float, dict[str, float]]:
    """用同一批冻结价格计算账户权益，避免分子/分母使用不同价格。"""
    equity = max(float(cash_balance or 0.0), 0.0)
    marks: dict[str, float] = {}
    quote_map = quote_map or {}
    quote_quality_map = quote_quality_map or {}
    for holding in holdings:
        quote = quote_map.get(holding.code) or {}
        quality = quote_quality_map.get(holding.code) or {}
        quote_usable = bool(
            float(quote.get("price", 0.0) or 0.0) > 0
            and (not quality.get("required") or quality.get("fresh"))
        )
        frame = price_frames.get(holding.code)
        close = 0.0
        if frame is not None and not frame.empty and "close" in frame.columns:
            close = float(frame["close"].iloc[-1] or 0.0)
        mark = (
            float(quote["price"]) if quote_usable
            else close if close > 0
            else float(holding.cost_price or 0.0)
        )
        marks[holding.code] = mark
        equity += float(holding.shares or 0.0) * mark
    return max(equity, 0.0), marks


def _portfolio_risk_snapshot_markdown(snapshot: dict) -> str:
    if not snapshot.get("weights"):
        return ""
    lines = [
        "### 组合风险预算\n",
        "| 指标 | 当前值 | 约束 |",
        "|------|------:|------|",
        f"| 股票总仓位 | {snapshot['gross_exposure']:.1%} | 建议不超过90% |",
        f"| 最大单票 | {snapshot['max_code'] or '—'} {snapshot['max_weight']:.1%} | 新增后不超过25% |",
        f"| 集中度 HHI | {snapshot['hhi']:.2f} | ≥0.25 视为集中 |",
        f"| 估算年化波动 | {snapshot['annualized_vol']:.1%} | ≥35% 视为高波动 |",
        f"| 剩余新增容量 | {snapshot['new_position_capacity_pct']:.1%} | 达到0时禁止新增风险 |",
        "",
    ]
    if snapshot.get("high_corr_pairs"):
        lines.extend([
            "**高相关持仓约束**：",
            "| 股票组合 | 相关系数 | 合计仓位 | 处理 |",
            "|------|------:|------:|------|",
        ])
        for pair in snapshot["high_corr_pairs"][:6]:
            lines.append(
                f"| {pair['left']} / {pair['right']} | {pair['correlation']:.2f} | "
                f"{pair['combined_weight']:.1%} | 合计仓位不再扩大，优先选择更强者 |"
            )
        lines.append("")
    if snapshot.get("risk_flags"):
        lines.append(f"> 风险提醒：{'；'.join(snapshot['risk_flags'])}\n")
    return "\n".join(lines)


def _support_rebound_candidate(item: dict) -> dict | None:
    """识别关键均线支撑候选，作为待确认观察点，而非无条件买入。"""
    marker = item.get("technical_marker") or {}
    price = float(item.get("current_price") or marker.get("close") or 0)
    low = float(marker.get("low") or 0)
    close = float(marker.get("close") or price)
    ma120 = float(marker.get("ma_120") or 0)
    ma60 = float(marker.get("ma_60") or 0)
    alpha = item.get("alpha_score")
    alpha = float(alpha) if alpha is not None else 0.0

    if price <= 0 or low <= 0 or ma120 <= 0:
        return None

    touched_ma120 = low <= ma120 * 1.01 and low >= ma120 * 0.97
    reclaimed = close >= ma120 or price >= ma120
    if not touched_ma120:
        return None

    if reclaimed:
        action = "🟢 支撑反弹候选"
        trigger = f"低点 {low:.2f} 触及 MA120={ma120:.2f} 后重新站回，允许小仓位验证"
    else:
        action = "🟡 支撑待确认"
        trigger = f"低点 {low:.2f} 触及 MA120={ma120:.2f}，但收盘/现价仍低于 MA120；需重新站回 {ma120:.2f}"

    reason_parts = [trigger]
    if ma60 > 0:
        relation = "上方" if price >= ma60 else "下方"
        reason_parts.append(f"现价在 MA60={ma60:.2f} {relation}")
    if alpha < -0.10:
        reason_parts.append(f"Alpha={alpha:+.3f} 偏空，仓位应保守")

    return {
        "action": action,
        "trigger": "；".join(reason_parts),
        "ma120": ma120,
        "low": low,
        "price": price,
        "confirmed": reclaimed,
    }


def _latest_technical_marker(df: pd.DataFrame) -> dict:
    """提取组合层风控/支撑判断需要的最新技术数据。"""
    if df is None or df.empty:
        return {}
    last = df.iloc[-1]
    recent = df.tail(min(len(df), 120))

    def f(name: str) -> float:
        value = last.get(name)
        try:
            return float(value) if value is not None and pd.notna(value) else 0.0
        except Exception:
            return 0.0

    high_120 = 0.0
    try:
        high_120 = float(recent["high"].max()) if "high" in recent.columns else 0.0
    except Exception:
        high_120 = 0.0

    atr14 = 0.0
    try:
        hist = df.tail(min(len(df), 30)).copy()
        prev_close = hist["close"].shift(1)
        tr = pd.concat([
            (hist["high"] - hist["low"]).abs(),
            (hist["high"] - prev_close).abs(),
            (hist["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr14 = float(tr.rolling(14).mean().iloc[-1])
    except Exception:
        atr14 = 0.0

    return {
        "open": f("open"),
        "high": f("high"),
        "low": f("low"),
        "close": f("close"),
        "ma_20": f("ma_20"),
        "ma_60": f("ma_60"),
        "ma_120": f("ma_120"),
        "bb_upper": f("bb_upper"),
        "bb_lower": f("bb_lower"),
        "rsi": f("rsi"),
        "high_120": high_120,
        "atr14": atr14 if pd.notna(atr14) else 0.0,
    }


def _mode_trigger_label(mode: str) -> str:
    if mode == "intraday":
        return "盘中触发交易计划（基于当前价，条件满足可当日执行）"
    if mode == "pre":
        return "盘前触发交易计划（基于盘前价/T-1数据，开盘后确认执行）"
    return "盘后触发交易计划（基于收盘数据，下一交易日盘中确认执行）"


def _build_trigger_plan_row(
    item: dict,
    account_equity: float,
    currency: str,
    allow_new_positions: bool = True,
) -> dict:
    """为单只股票生成买/卖/持有的条件触发表。"""
    obj = item.get("holding") or item.get("watch_item")
    code = obj.code if obj else "?"
    name = obj.name if obj else code
    is_holding = bool(item.get("holding"))
    marker = item.get("technical_marker") or {}
    price = float(item.get("current_price") or marker.get("close") or 0)
    alpha = item.get("alpha_score")
    alpha = float(alpha) if alpha is not None else 0.0
    ma20 = float(marker.get("ma_20") or 0)
    ma60 = float(marker.get("ma_60") or 0)
    ma120 = float(marker.get("ma_120") or 0)
    high = float(marker.get("high") or 0)
    high_120 = float(marker.get("high_120") or 0)
    atr14 = float(marker.get("atr14") or 0)

    support = _support_rebound_candidate(item)
    risk = _portfolio_risk_overlay(item, account_equity) if is_holding else None
    signals = item.get("signal_check") or []
    buy_signal = next((s for s in signals if s.get("signal") == "buy"), None)
    data_quality = item.get("data_quality") or {}
    quality_blocked = data_quality.get("status") == "blocked" or data_quality.get("action") == "block"

    symbol = f"{name}（{code}）"
    state = "持仓" if is_holding else "关注"
    conservative = "持有/等待确认" if is_holding else "等待买点"
    aggressive = "不主动加仓" if is_holding else "小仓试探需确认"
    buy_trigger = "暂无买入触发条件"
    sell_trigger = "无持仓" if not is_holding else "未触发减仓条件"
    invalidation = "5个交易日未触发则重新评估"

    if buy_signal:
        entry = float(buy_signal.get("entry_price") or price or 0)
        stop = float(buy_signal.get("stop_loss") or 0)
        pos = float(buy_signal.get("position_pct") or 0)
        buy_trigger = (
            f"策略买入信号已触发，参考价 {currency}{entry:.2f}，目标仓位 {pos:.1%}"
            if entry > 0 else "策略买入信号已触发"
        )
        if stop > 0:
            invalidation = f"跌破策略止损 {currency}{stop:.2f} 失效"
        conservative = "按策略仓位执行"
        aggressive = "可按策略上限执行，但不得超过账户风控上限"
        if not allow_new_positions:
            buy_trigger = (
                f"策略买入信号已触发（参考价 {currency}{entry:.2f}），"
                "但组合新增容量或可用资金为0，仅保留观察"
                if entry > 0 else
                "策略买入信号已触发，但组合新增容量或可用资金为0，仅保留观察"
            )
            conservative = "禁止新增仓位"
            aggressive = "禁止新增仓位"
    elif support:
        buy_trigger = support["trigger"]
        if support.get("confirmed"):
            conservative = "小仓位验证支撑"
            aggressive = "小仓试探，不超过账户5%-8%"
        else:
            conservative = "等待重新站回关键线"
            aggressive = "只观察，不提前追单"
        invalidation = f"收盘低于 MA120 的 98%（{currency}{ma120*0.98:.2f}）则支撑失效" if ma120 > 0 else invalidation
    elif ma20 > 0:
        threshold = ma20 if alpha >= 0 else max(ma20, ma60)
        if threshold > 0:
            buy_trigger = (
                f"价格重新站上 {currency}{threshold:.2f} 且 Final_Score 转正/保持为正后再考虑"
            )
        invalidation = f"跌破 MA20={currency}{ma20:.2f} 且无法在2个交易日内收回，则买入假设失效"

    if risk:
        conservative = risk["action"]
        aggressive = "保留底仓，剩余仓位用触发条件管理"
        sell_trigger = risk["reason"]
        if "部分止盈" in risk["action"] and high > 0:
            lock_line = high * 0.965
            sell_trigger = (
                f"若价格低于冲高回落锁利线 {currency}{lock_line:.2f}"
                f"（当日高点{currency}{high:.2f}回撤3.5%），先部分止盈或上移止损"
            )
            if high_120 > 0:
                buy_trigger = (
                    f"不追高；只有重新突破 {currency}{high:.2f} 且回落不超过2%时，才考虑加仓"
                )
        elif "清仓" in risk["action"]:
            sell_trigger = f"风险已触发：{risk['reason']}；反弹不能站回 MA20/MA60 时优先退出"
        elif "减仓" in risk["action"]:
            sell_trigger = f"风险已触发：{risk['reason']}；反弹无法站回关键均线时减仓"
    elif is_holding:
        holding = item.get("holding")
        cost = float(getattr(holding, "cost_price", 0) or 0)
        stop_candidates = [v for v in [cost * 0.92 if cost > 0 else 0, ma20 * 0.98 if ma20 > 0 else 0] if v > 0]
        stop_line = max(stop_candidates) if stop_candidates else 0
        if stop_line > 0:
            sell_trigger = f"跌破 {currency}{stop_line:.2f} 且2个交易日内未收回，减仓/止损"
        if high > 0 and atr14 > 0:
            trail = high - 1.5 * atr14
            sell_trigger += f"；若从阶段高点回落至 {currency}{trail:.2f} 以下，上移止损"

    if is_holding and price > 0 and ma60 > 0 and ma120 > 0 and not buy_signal:
        if price > ma60 > ma120 and not risk:
            conservative = "趋势未破，继续持有"
            aggressive = "回踩MA20/MA60不破可加少量"

    if quality_blocked:
        issues = data_quality.get("issues") or data_quality.get("warnings") or ["数据质量阻断"]
        conservative = "暂停操作，先复核数据"
        aggressive = "禁止执行"
        buy_trigger = "数据质量阻断，禁止新开仓/加仓"
        sell_trigger = "持仓只做人工风控复核，不按本次价格自动交易" if is_holding else "无持仓"
        invalidation = f"修复K线缓存并重新生成报告后再判断；原因：{'；'.join(str(x) for x in issues[:2])}"

    return {
        "symbol": symbol,
        "state": state,
        "price": price,
        "price_reliable": not quality_blocked,
        "conservative": conservative,
        "aggressive": aggressive,
        "buy_trigger": buy_trigger,
        "sell_trigger": sell_trigger,
        "invalidation": invalidation,
    }


def _build_conditional_trigger_plan(
    all_data: list[dict],
    mode: str,
    currency: str,
    account_equity: float,
    allow_new_positions: bool = True,
) -> str:
    """组合级条件触发计划：盘中/盘前/盘后都输出可执行条件。"""
    if not all_data:
        return ""
    lines = [
        f"### 📌 {_mode_trigger_label(mode)}\n",
        "> **先看当前动作，再看未来条件**：持仓行若已提示减仓，后面的买入条件表示“减仓后何时允许重新加回”，不是同时买入。\n",
        "| 股票 | 类型 | 当前价 | 当前保守方案 | 当前激进方案 | 未来买入/重新加回条件 | 当前卖出/减仓条件 | 失效条件 |",
        "|------|------|------:|------|------|------|------|------|",
    ]
    for item in all_data:
        row = _build_trigger_plan_row(
            item, account_equity, currency,
            allow_new_positions=allow_new_positions,
        )
        price_str = f"{currency}{row['price']:.2f}" if row["price"] > 0 else "—"
        if not row.get("price_reliable", True) and price_str != "—":
            price_str += "（待复核）"
        lines.append(
            f"| {row['symbol']} | {row['state']} | {price_str} | "
            f"{row['conservative']} | {row['aggressive']} | "
            f"{row['buy_trigger']} | {row['sell_trigger']} | {row['invalidation']} |"
        )
    lines.append("")
    lines.append(
        "> 盘中报告用于当日盯盘；盘前/盘后用于下一交易日确认。"
        "“重新站上均线且 Final_Score 转正”代表风险解除后重新评估，不是到价自动下单。\n"
    )
    return "\n".join(lines)


def _build_data_quality_overview(all_data: list[dict]) -> str:
    """组合层数据质量概览。"""
    rows = []
    for item in all_data:
        obj = item.get("holding") or item.get("watch_item")
        dq = item.get("data_quality") or {}
        if not obj or not dq:
            continue
        status = dq.get("status", "ok")
        if status == "ok":
            continue
        issue = "；".join((dq.get("issues") or dq.get("warnings") or dq.get("missing") or [])[:2])
        rows.append({
            "symbol": f"{obj.name}（{obj.code}）",
            "score": float(dq.get("score", 0) or 0),
            "status": status,
            "action": dq.get("action", "normal"),
            "multiplier": float(dq.get("max_position_multiplier", 1.0) or 0),
            "issue": issue or "数据质量降级",
        })
    if not rows:
        return ""
    status_label = {
        "watch": "🟡 观察",
        "degraded": "🟠 降级",
        "blocked": "🔴 阻断",
    }
    lines = [
        "### 🧾 数据质量概览\n",
        "| 股票 | 评分 | 状态 | 交易闸门 | 仓位倍率 | 主要原因 |",
        "|------|------:|------|------|------:|------|",
    ]
    for row in rows:
        lines.append(
            f"| {row['symbol']} | {row['score']:.0f}/100 | "
            f"{status_label.get(row['status'], row['status'])} | {row['action']} | "
            f"{row['multiplier']:.0%} | {row['issue']} |"
        )
    lines.append("")
    lines.append("> 数据质量为降级/阻断时，系统会自动降低执行等级、压缩仓位或禁止新开仓。\n")
    return "\n".join(lines)


def _expectancy_label(expectancy: str) -> str:
    return {
        "positive": "正期望",
        "negative": "负期望",
        "insufficient": "样本不足",
    }.get(expectancy or "", expectancy or "样本不足")


def _pattern_label(pattern_type: str) -> str:
    return {
        "profit_lock": "冲高回落锁利",
        "ma120_support": "MA120支撑",
        "momentum_chase": "动量追高",
        "position_risk": "持仓风控",
        "failed_breakout": "假突破/突破失败",
        "oversold_reversal": "超卖反转",
        "trend_pullback": "趋势回调",
    }.get(pattern_type or "", pattern_type or "未知形态")


def _build_historical_evaluation_markdown(
    forecast_rows: list[dict],
    plan_rows: list[dict],
    prediction_rows: list[dict],
    observation_rows: list[dict],
    market: str,
    exit_rows: list[dict] | None = None,
    forecast_summary: dict | None = None,
    joint_rows: list[dict] | None = None,
    minute_rows: list[dict] | None = None,
    context_rows: list[dict] | None = None,
) -> str:
    """构建历史预测评估面板 Markdown，用于 Tab3 UI 和报告复用。"""
    market_label = "美股" if market == "US" else "A股"
    lines = [
        f"### {market_label}历史预测评估面板：预测与策略分账\n",
        "> 预测准不准与方案赚不赚钱分开统计；样本不足时不能作为可执行依据。\n",
        "#### 三步阅读法\n",
        "1. **先看样本**：少于10次只算积累，10～29次只作初步观察，至少30次才开始比较模型。",
        "2. **再看预测**：Brier、Log Loss、ECE越低越好，80%区间命中应在样本充分后接近80%。",
        "3. **最后看交易**：联合OOF超额收益为正、回撤可控，才说明完整建议链有价值。\n",
    ]

    total_verified = sum(int(row.get("verified", 0) or 0) for row in forecast_rows)
    total_pending = sum(int(row.get("pending", 0) or 0) for row in forecast_rows)
    total_unsupported = sum(int(row.get("unsupported", 0) or 0) for row in forecast_rows)
    lines.extend([
        "#### 当前预测样本状态\n",
        "| 有效已验证 | 等待目标日 | 已隔离无效记录 | 当前能否评价模型 |",
        "|------:|------:|------:|------|",
        f"| {total_verified} | {total_pending} | {total_unsupported} | "
        f"{'可以初步评价' if total_verified >= 10 else '不能，继续积累'} |",
        "",
    ])

    lines.append("#### 新版独立概率预测\n")
    if forecast_rows:
        if total_verified <= 0:
            grouped: dict[str, dict[int, dict]] = {}
            for row in forecast_rows:
                grouped.setdefault(str(row["label"]), {})[int(row["horizon"])] = row

            def _compact_status(item: dict | None) -> str:
                if not item:
                    return "—"
                parts = []
                pending = int(item.get("pending", 0) or 0)
                unsupported = int(item.get("unsupported", 0) or 0)
                if pending:
                    parts.append(f"待验证{pending}")
                if unsupported:
                    parts.append(f"已隔离{unsupported}")
                return " / ".join(parts) or "—"

            lines.extend([
                "> 当前没有有效到期样本，先用紧凑表展示各周期进度；指标表会在首批预测到期后自动展开。\n",
                "| 标的 | 1日状态 | 3日状态 | 5日状态 |",
                "|------|------|------|------|",
            ])
            for label, horizons in grouped.items():
                lines.append(
                    f"| {label} | {_compact_status(horizons.get(1))} "
                    f"| {_compact_status(horizons.get(3))} "
                    f"| {_compact_status(horizons.get(5))} |"
                )
        else:
            lines.extend([
                "| 标的 | 周期 | 已验证 | 待验证 | 已隔离 | 方向正确率 | Brier | Log Loss | ECE | 80%区间命中 |",
                "|------|------:|------:|------:|------:|------:|------:|------:|------:|------:|",
            ])
            for row in forecast_rows:
                verified = int(row["verified"])
                accuracy = f"{row['accuracy']:.0%}" if verified else "—"
                brier = f"{row['brier_score']:.3f}" if verified else "—"
                log_loss = f"{row['log_loss']:.3f}" if verified else "—"
                ece = f"{row['ece']:.1%}" if verified else "—"
                coverage = f"{row['interval_coverage']:.0%}" if verified else "—"
                lines.append(
                    f"| {row['label']} | {row['horizon']}日 "
                    f"| {row['verified']} | {row['pending']} | {row.get('unsupported', 0)} "
                    f"| {accuracy} | {brier} | {log_loss} | {ece} | {coverage} |"
                )
        lines.append(
            "\n> **用途**：判断预测模块本身准不准。Brier看整体概率误差；Log Loss重罚高置信度错误；"
            "ECE看模型说的把握与实际命中是否一致；没有已验证样本时显示“—”。\n"
        )
    else:
        lines.append("- 暂无新版独立预测。生成报告后会产生明确目标交易日的预测并自动验证。\n")

    summary = forecast_summary or {}
    calibration = summary.get("calibration_bins") or []
    if calibration:
        lines.extend([
            "#### 预测校准曲线（置信度分箱）\n",
            "> **校准图怎么读**：横轴是模型声称的平均把握，纵轴是实际正确率；"
            "灰色对角线是理想状态，蓝线越贴近它越可信；n是该点样本数。\n",
            "| 预测置信度区间 | 样本 | 平均置信度 | 实际命中率 | 校准偏差 |",
            "|------|------:|------:|------:|------:|",
        ])
        for item in calibration:
            lines.append(
                f"| {float(item.get('lower', 0)):.0%}–{float(item.get('upper', 0)):.0%} "
                f"| {int(item.get('count', 0))} "
                f"| {float(item.get('mean_confidence', 0)):.1%} "
                f"| {float(item.get('accuracy', 0)):.1%} "
                f"| {float(item.get('gap', 0)):.1%} |"
            )
        lines.append("")
        lines.append(
            "> **怎么看**：先看每档样本，再比较“平均置信度”和“实际命中率”；"
            "两者越接近，校准偏差越小。\n"
        )
    else:
        lines.extend([
            "#### 预测校准曲线（置信度分箱）\n",
            "- 暂无有效已验证预测：校准图保留灰色理想线，但暂不绘制蓝色实际曲线和分箱表。"
            "待1/3/5日目标交易日到期并取得正式收盘价后自动出现。\n",
        ])

    regime_metrics = summary.get("regime_metrics") or {}
    if regime_metrics:
        regime_labels = {
            "trending_volatile": "趋势高波", "trending_steady": "趋势平稳",
            "ranging": "震荡", "transitional": "过渡", "unknown": "未知",
        }
        lines.extend([
            "#### 不同市场状态下的预测表现\n",
            "| 市场状态 | 样本 | 方向正确率 | Brier | Log Loss | ECE |",
            "|------|------:|------:|------:|------:|------:|",
        ])
        for regime, item in sorted(regime_metrics.items()):
            lines.append(
                f"| {regime_labels.get(regime, regime)} "
                f"| {int(item.get('samples', 0))} "
                f"| {float(item.get('accuracy', 0)):.0%} "
                f"| {float(item.get('brier_score', 0)):.3f} "
                f"| {float(item.get('log_loss', 0)):.3f} "
                f"| {float(item.get('ece', 0)):.1%} |"
            )
        lines.append("")
        lines.append(
            "> **用途**：找出模型适合趋势市还是震荡市。必须先看各状态样本数，再比较正确率和误差。\n"
        )
    else:
        lines.extend([
            "#### 不同市场状态下的预测表现\n",
            "- 暂无有效已验证预测，暂时无法判断模型在趋势、震荡或高波动市场中的表现。\n",
        ])

    lines.append("#### 最终建议链样本外回放（联合OOF）：预测 + 策略 + 风控官\n")
    if joint_rows:
        lines.extend([
            "| 标的 | 测试区间 | 决策点 | 交易 | 扣成本收益 | 基准收益 | 超额收益 | 夏普 | 最大回撤 | 漂移 | 结论 |",
            "|------|------|------:|------:|------:|------:|------:|------:|------:|------|------|",
        ])
        for row in joint_rows[:20]:
            stock = str(row.get("label") or row.get("code") or "")
            trades = int(row.get("total_trades", 0))
            excess = float(row.get("excess_return", 0) or 0)
            conclusion = (
                "样本不足" if trades < 3
                else "正超额" if excess > 0
                else "未跑赢基准"
            )
            drift_status = str(row.get("drift_status") or "stable")
            drift_label = {
                "stable": "稳定", "warning": "预警", "critical": "严重",
            }.get(drift_status, drift_status)
            lines.append(
                f"| {stock} | {row.get('data_start', '')} 至 {row.get('data_end', '')} "
                f"| {int(row.get('samples', 0))} | {int(row.get('total_trades', 0))} "
                f"| {float(row.get('total_return', 0)):+.2%} "
                f"| {float(row.get('benchmark_return', 0)):+.2%} "
                f"| {float(row.get('excess_return', 0)):+.2%} "
                f"| {float(row.get('sharpe_ratio', 0)):.2f} "
                f"| {float(row.get('max_drawdown', 0)):.2%} | {drift_label} | {conclusion} |"
            )
        lines.append(
            "\n> 该表只使用每个测试折之前的数据选择预测参数和策略；"
            "它评价的是最终建议链，而不是单个策略的全样本回测。"
            "优先看交易数、超额收益和最大回撤；交易少于3笔只能判为样本不足。\n"
        )
        drift_rows = [
            row for row in joint_rows
            if str(row.get("drift_status") or "stable") in ("warning", "critical")
        ]
        for row in drift_rows[:8]:
            reasons = "；".join(row.get("drift_reasons") or []) or "近期指标恶化"
            lines.append(
                f"> **漂移提醒 {row.get('label') or row.get('code')}**：{reasons}。"
                "该提醒只会降低新开仓，不会阻止止损或锁利。\n"
            )
        horizon_lines = []
        for row in joint_rows[:12]:
            stock = str(row.get("label") or row.get("code") or "")
            for horizon, metrics in sorted(
                (row.get("horizon_metrics") or {}).items(), key=lambda item: int(item[0])
            ):
                horizon_lines.append(
                    f"| {stock} | {horizon}日 | {int(metrics.get('samples', 0))} "
                    f"| {float(metrics.get('brier_score', 0)):.3f} "
                    f"| {float(metrics.get('log_loss', 0)):.3f} "
                    f"| {float(metrics.get('ece', 0)):.1%} |"
                )
        if horizon_lines:
            lines.extend([
                "#### 样本外回放中的1/3/5日预测质量（联合OOF）\n",
                "| 标的 | 周期 | 样本 | Brier | Log Loss | ECE |",
                "|------|------:|------:|------:|------:|------:|",
                *horizon_lines,
                "",
                "> **用途**：比较1日、3日、5日哪个预测周期更可靠；只有样本接近时，才比较Brier、Log Loss和ECE。\n",
            ])
        trace_lines = []
        for row in joint_rows[:8]:
            stock = str(row.get("label") or row.get("code") or "")
            for event in (row.get("trace") or [])[-8:]:
                for horizon, forecast in sorted(
                    (event.get("forecasts") or {}).items(), key=lambda item: int(item[0])
                ):
                    actual = str(forecast.get("actual_direction") or "—")
                    correct = forecast.get("correct")
                    broker_status = str(event.get("broker_status") or "")
                    if broker_status == "filled":
                        broker_text = (
                            f"成交{event.get('executed_action', '')} "
                            f"{int(event.get('executed_shares', 0) or 0)}股"
                            f"@{float(event.get('fill_price', 0) or 0):.2f}"
                        )
                    elif broker_status == "rejected":
                        broker_text = "Broker拒绝/未成交"
                    else:
                        broker_text = "未提交订单"
                    trace_lines.append(
                        f"| {stock} | {event.get('date', '')} | {forecast.get('target_date') or horizon + '日后'} "
                        f"| {horizon}日{forecast.get('direction', '—')} | {actual} "
                        f"| {'正确' if correct == 1 else '错误' if correct == 0 else '待核'} "
                        f"| {event.get('action', 'watch')}/{event.get('execution_level', 'C')} "
                        f"| {broker_text} |"
                    )
        if trace_lines:
            lines.extend([
                "#### 样本外逐事件明细（联合OOF最近记录）\n",
                "| 标的 | 预测发生日 | 目标日 | 当时预测 | 实际结果 | 对错 | 策略决策 | Broker结果 |",
                "|------|------|------|------|------|------|------|------|",
                *trace_lines[:40],
                "",
                "> **用途**：逐条追责预测、策略、风控和成交。先检查预测发生日早于目标日，再看预测对错和未成交原因。\n",
            ])
    else:
        lines.append("- 暂无联合OOF结果；下一次后台预测优化完成后生成。\n")

    lines.append("#### 盘中分钟K前瞻证据\n")
    if minute_rows:
        lines.extend([
            "| 标的 | 分钟K | 交易日 | 来源 | 待验证方案 | 已验证方案 | 覆盖截止 |",
            "|------|------:|------:|------|------:|------:|------|",
        ])
        for row in minute_rows[:20]:
            lines.append(
                f"| {row.get('label') or row.get('code', '')} "
                f"| {int(row.get('bar_count', 0))} | {int(row.get('sessions', 0))} "
                f"| {row.get('sources') or '—'} | {int(row.get('pending', 0))} "
                f"| {int(row.get('evaluated', 0))} | {row.get('last_session') or '—'} |"
            )
        lines.append(
            "\n> 分钟K只验证采集之后的盘中方案，并与正式日K隔离；"
            "缺少信号后分钟路径时继续等待，不使用整日最高/最低价推断顺序。\n"
        )
    else:
        lines.append("- 尚无分钟K前瞻证据；首次盘中分析后由后台开始积累。\n")

    lines.append("#### 新闻/基本面历史时点快照\n")
    if context_rows:
        lines.extend([
            "| 标的 | 快照 | 含新闻 | 含基本面 | 基本面来源 | 首次冻结 | 最近冻结 |",
            "|------|------:|------:|------:|------|------|------|",
        ])
        for row in context_rows[:20]:
            lines.append(
                f"| {row.get('label') or row.get('code', '')} "
                f"| {int(row.get('snapshot_count', 0))} "
                f"| {int(row.get('news_snapshots', 0))} "
                f"| {int(row.get('fundamental_snapshots', 0))} "
                f"| {row.get('fundamental_sources') or '—'} "
                f"| {str(row.get('first_captured_at') or '')[:10]} "
                f"| {str(row.get('last_captured_at') or '')[:10]} |"
            )
        lines.append(
            "\n> 快照只从应用真实抓取时间开始积累，不把今天看到的数据回填到过去；"
            "覆盖不足前，新闻和基本面不会进入历史预测 OOF。\n"
        )
    else:
        lines.append("- 尚无历史时点上下文；下一次 Tab1/Tab3 分析会开始冻结。\n")

    lines.append("#### 新版交易方案表现\n")
    if plan_rows:
        intent_labels = {
            "alpha_entry": "策略建仓", "alpha_exit": "策略退出",
            "risk_exit": "风险退出", "profit_lock": "锁利",
        }
        lines.extend([
            "| 股票/策略 | 意图 | 记录/独立日 | 扣成本平均表现 | 正收益率 | 平均有利波动 | 平均不利波动 | 验证证据 |",
            "|------|------|------:|------:|------:|------:|------:|------|",
        ])
        for row in plan_rows[:20]:
            if int(row.get("intraday_count", 0) or 0) > 0:
                provider_count = int(row.get("provider_evidence_count", 0) or 0)
                supplemental_count = int(row.get("supplemental_evidence_count", 0) or 0)
                evidence = (
                    f"分钟K：供应商{provider_count}，补充源{supplemental_count}"
                    f"（{row.get('evidence_sources') or '来源未知'}）"
                )
            else:
                evidence = "正式日K"
            lines.append(
                f"| {row.get('code', '')}/{row.get('strategy_key', '')} "
                f"| {intent_labels.get(row.get('signal_intent', ''), row.get('signal_intent', ''))} "
                f"| {int(row.get('count', 0))}/{int(row.get('independent_days', 0))} "
                f"| {float(row.get('avg_net_return', 0) or 0):+.2%} "
                f"| {float(row.get('positive_rate', 0) or 0):.0%} "
                f"| {float(row.get('avg_mfe', 0) or 0):+.2%} "
                f"| {float(row.get('avg_mae', 0) or 0):+.2%} | {evidence} |"
            )
        lines.append("")
    else:
        lines.append("- 暂无完成 5 个交易日复盘的新版交易方案。\n")

    if prediction_rows:
        lines.append("#### 旧版动作派生记录（仅兼容参考）\n")
        lines.extend([
            "| 标的 | 已验证 | 待验证 | 不可验证 | 方向正确率 | 平均方向净收益 | 期望 |",
            "|------|------:|------:|------:|------:|------:|------|",
        ])
        for row in prediction_rows:
            lines.append(
                f"| {row.get('label', '')} "
                f"| {int(row.get('count', 0))} "
                f"| {int(row.get('pending', 0))} "
                f"| {int(row.get('unsupported', 0))} "
                f"| {float(row.get('accuracy', 0)):.0%} "
                f"| {float(row.get('avg_return', 0)):+.2%} "
                f"| {_expectancy_label(str(row.get('expectancy', 'insufficient')))} |"
            )
        lines.append("")

    if exit_rows:
        lines.extend([
            "#### 卖出后退出质量\n",
            "| 股票/策略 | 样本 | 5日涨跌 | 10日涨跌 | 20日涨跌 | 避免损失 | 机会成本 | 有效率 |",
            "|------|------:|------:|------:|------:|------:|------:|------:|",
        ])
        for row in exit_rows[:15]:
            lines.append(
                f"| {row.get('label', '')} / {row.get('strategy_name', '')} "
                f"| {int(row.get('count', 0))} "
                f"| {float(row.get('avg_return_5d', 0)):+.2%} "
                f"| {float(row.get('avg_return_10d', 0)):+.2%} "
                f"| {float(row.get('avg_return_20d', 0)):+.2%} "
                f"| {float(row.get('avg_avoided_loss', 0)):.2%} "
                f"| {float(row.get('avg_opportunity_cost', 0)):.2%} "
                f"| {float(row.get('effective_rate', 0)):.0%} |"
            )
        lines.append("")

    if observation_rows:
        lines.extend([
            "#### 观察形态表现\n",
            "| 股票 | 形态 | 等级 | 总样本 | LLM样本/胜率 | 5日胜率 | 5日均值 | 10日均值 | 最大不利 | 期望 |",
            "|------|------|------|------:|------:|------:|------:|------:|------:|------|",
        ])
        for row in observation_rows:
            symbol = f"{row.get('name') or row.get('code')}（{row.get('code')}）"
            lines.append(
                f"| {symbol} "
                f"| {_pattern_label(str(row.get('pattern_type', '')))} "
                f"| {row.get('execution_level', '') or '—'} "
                f"| {int(row.get('count', 0))} "
                f"| {int(row.get('llm_count', 0))}/"
                f"{float(row.get('llm_win_rate_5d', 0)):.0%} "
                f"| {float(row.get('win_rate_5d', 0)):.0%} "
                f"| {float(row.get('avg_return_5d', 0)):+.2%} "
                f"| {float(row.get('avg_return_10d', 0)):+.2%} "
                f"| {float(row.get('avg_adverse', 0)):+.2%} "
                f"| {_expectancy_label(str(row.get('expectancy', 'insufficient')))} |"
            )
        lines.append("")

    lines.append("> 面板用于回答“系统过去是否有效”。正期望且风险可控的建议才允许进入更高执行等级。")
    return "\n".join(lines)


def _fetch_portfolio_realtime_quote(
    code: str,
    market: str,
    fetcher,
    mode: str = "intraday",
    prefetched_quote: dict | None = None,
    prefetch_attempted: bool = False,
) -> dict | None:
    """获取组合页使用的当前价，返回 {price, source, timestamp, session}。"""
    try:
        from utils.session import detect_session
        from data.stock_fetcher import fetch_us_extended_quote

        # Premarket is an explicit user-selected horizon. Do not let a stale
        # TickFlow timestamp reclassify it as yesterday's intraday session.
        if market == "US" and mode == "pre":
            ext_data = fetch_us_extended_quote(code)
            if ext_data and ext_data.get("price", 0) > 0:
                session = detect_session(market, stock_quote=ext_data)
                source = str(ext_data.get("source") or "Nasdaq.com")
                return _quote_payload(
                    price=float(ext_data["price"]),
                    timestamp=ext_data.get("timestamp", 0),
                    source=f"{source}延伸时段",
                    session=session,
                    bar=ext_data,
                )
            return None

        quote = prefetched_quote
        if quote is None and not prefetch_attempted:
            try:
                quote = fetcher.fetch_quote(code) if hasattr(fetcher, "fetch_quote") else None
            except Exception as e:
                logger.debug(f"{code} quote 获取失败: {e}")
        tick = None
        if quote and quote.get("latest", 0) > 0:
            tick = {
                "latest": quote["latest"],
                "volume": quote.get("volume", 0),
                "timestamp": quote.get("timestamp", 0),
            }

        session = detect_session(market, stock_tick=tick, stock_quote=quote)

        if tick and tick.get("latest", 0) > 0:
            return _quote_payload(
                price=float(tick["latest"]),
                timestamp=tick.get("timestamp", 0),
                source="TickFlow实时报价",
                session=session,
                bar=quote or tick,
            )
        if quote and quote.get("latest", 0) > 0:
            return _quote_payload(
                price=float(quote["latest"]),
                timestamp=quote.get("timestamp", 0),
                source="实时报价",
                session=session,
                bar=quote,
            )

    except Exception as e:
        logger.warning(f"获取 {code} 当前价失败: {e}")
    return None


def _build_portfolio_operation_summary(
    holdings_data: list[dict],
    watchlist_data: list[dict],
    market: str,
    balance,
    account_equity: float,
    mode: str = "eod",
    price_frames: dict[str, pd.DataFrame] | None = None,
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
    portfolio_risk = _compute_portfolio_risk_snapshot(
        holdings_data, price_frames, account_equity
    )
    available = balance.us_balance if market == "US" else balance.a_balance
    allow_new_positions = bool(
        account_equity > 0
        and float(available or 0.0) > 0
        and float(portfolio_risk.get("new_position_capacity_pct", 0.0) or 0.0) > 0
    )

    # ── 分类：策略信号 + 组合风控 ──
    sell_stocks = []  # 持仓退出/减仓信号
    risk_stocks = []  # 持仓风控覆盖提示：不依赖策略是否发出 sell
    support_stocks = []  # 关键均线支撑/反弹候选
    buy_stocks = []   # 有买入信号的股票
    hold_stocks = []  # 无买入信号的股票

    for d in all_data:
        obj = d.get("holding") or d.get("watch_item")
        code = obj.code if obj else "?"
        name = obj.name if obj else code
        sc_list = d.get("signal_check") or []
        op_plan = d.get("operation_plan")  # str（plan.markdown）或 None

        buys = [s for s in sc_list if s.get("signal") == "buy"]
        raw_sells = [s for s in sc_list if s.get("signal") == "sell"]
        sells = select_actionable_sell_signals(raw_sells)
        if sells:
            sell_stocks.append({
                "code": code, "name": name,
                "price": d.get("current_price", 0),
                "position_value": d.get("position_value", 0.0),
                "sell_count": len(sells),
                "op_plan": op_plan,
            })

        if d.get("holding") and not sells:
            risk = _portfolio_risk_overlay(d, account_equity)
            if risk:
                risk_stocks.append({
                    "code": code, "name": name,
                    "price": d.get("current_price", 0),
                    **risk,
                })

        support = _support_rebound_candidate(d)
        if support and not buys:
            support_stocks.append({
                "code": code, "name": name,
                **support,
            })

        if buys:
            top_buy = buys[0]
            buy_stocks.append({
                "code": code, "name": name,
                "price": d.get("current_price", 0),
                "position_value": d.get("position_value", 0.0),
                "target_pct": max(float(top_buy.get("position_pct", 0) or 0), 0.0),
                "buy_count": len(buys),
                "op_plan": op_plan,
            })
        elif not sells:
            hold_stocks.append({
                "code": code, "name": name,
                "price": d.get("current_price", 0),
                "is_holding": bool(d.get("holding")),
                "top_candidate": _top_candidate_signal(sc_list),
            })

    total = len(all_data)
    buy_candidate_count = len(buy_stocks)
    buy_count = buy_candidate_count if allow_new_positions else 0
    sell_count = len(sell_stocks)
    risk_count = len(risk_stocks)
    support_count = len(support_stocks)
    hold_count = len(hold_stocks)
    mode_desc = {
        "intraday": "盘中可执行条件",
        "pre": "盘前情景计划",
        "eod": "下一交易日计划",
    }.get(mode, "条件计划")

    lines.extend([
        "### 一分钟操作台\n",
        f"> 报告模式：**{mode_desc}**。先处理退出/风控，再看买入/支撑，最后看暂不操作清单。\n",
        "| 优先级 | 类别 | 数量 | 处理方式 |",
        "|:---:|------|------:|------|",
        f"| 1 | 持仓退出/减仓 | {sell_count} | 有策略卖出信号时优先复核实时价和止损线 |",
        f"| 2 | 持仓风控 | {risk_count} | 即使策略未卖出，也要检查浮亏、集中度和锁利线 |",
        f"| 3 | 买入/加仓 | {buy_count} | "
        + (
            "只在触发价、止损、仓位和历史验证同时成立时执行 |"
            if allow_new_positions else
            f"{buy_candidate_count} 个策略候选被组合容量/可用资金闸门阻断 |"
        ),
        f"| 4 | 支撑/反弹观察 | {support_count} | 到达关键线只代表值得盯盘，重新站回后再确认 |",
        f"| 5 | 暂不操作 | {hold_count} | 记录缺失条件，等待下一次触发 |",
        "",
        f"**估算账户权益**: {currency}{account_equity:,.2f}\n",
    ])

    risk_markdown = _portfolio_risk_snapshot_markdown(portfolio_risk)
    if risk_markdown:
        lines.append(risk_markdown)

    def _clean_plan_md(md: str) -> str:
        """清理 signal_check.py 生成的 markdown，去掉重复标题并降低嵌套层级。"""
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
        # 组合报告已有汇总视图，不为每只股票重复附带完整策略状态长表。
        compact_lines = []
        skipping_repeated_section = False
        for line in md.splitlines():
            if re.match(r"^#{2,6}\s+.*全策略信号状态", line.strip()):
                skipping_repeated_section = True
                continue
            if skipping_repeated_section and re.match(r"^#{2,4}\s+", line.strip()):
                skipping_repeated_section = False
            if not skipping_repeated_section and not line.startswith("*以上方案由系统自动生成"):
                compact_lines.append(line)
        md = "\n".join(compact_lines).strip()
        demoted = []
        for line in md.splitlines():
            if line.startswith("### "):
                demoted.append("##### " + line[4:])
            elif line.startswith("## "):
                demoted.append("##### " + line[3:])
            else:
                demoted.append(line)
        return "\n".join(demoted).strip()

    trigger_plan = _build_conditional_trigger_plan(
        all_data, mode, currency, account_equity,
        allow_new_positions=allow_new_positions,
    )
    if trigger_plan:
        lines.append(trigger_plan)

    quality_overview = _build_data_quality_overview(all_data)
    if quality_overview:
        lines.append(quality_overview)

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

    # ── 组合风控覆盖：策略未触发 sell，但持仓本身已经触及风险边界 ──
    if risk_stocks:
        lines.append(f"### 🟠 持仓风控提示（{len(risk_stocks)} 只）\n")
        lines.append("| 股票 | 现价 | 动作 | 浮盈/亏 | 仓位占比 | 触发原因 |")
        lines.append("|------|------:|------|------:|------:|------|")
        for rs in risk_stocks:
            lines.append(
                f"| {rs['name']}（{rs['code']}） | {currency}{rs['price']:.2f} | "
                f"{rs['action']} | {rs['pnl_pct']:+.1%} | {rs['weight']:.1%} | "
                f"{rs['reason']} |"
            )
        lines.append("")
        lines.append("> 这些是组合层风控覆盖规则，不等同于单个策略发出的卖出信号；执行前仍应复核实时价格、税费和个人风险承受能力。\n")

    # ── 关键支撑候选：人眼常看的 MA120/半年线触碰，不再静默归为观望 ──
    if support_stocks:
        lines.append(f"### 🟡 关键支撑/反弹候选（{len(support_stocks)} 只）\n")
        lines.append("| 股票 | 现价 | 状态 | 关键线 | 触发/确认条件 |")
        lines.append("|------|------:|------|------:|------|")
        for ss in support_stocks:
            lines.append(
                f"| {ss['name']}（{ss['code']}） | {currency}{ss['price']:.2f} | "
                f"{ss['action']} | MA120={currency}{ss['ma120']:.2f} | {ss['trigger']} |"
            )
        lines.append("")
        lines.append("> 支撑候选只说明价格到达值得盯盘的位置；若未重新站回关键线，应按待确认处理，不等同于立即满仓买入。\n")

    # ── 有买入信号的股票：直接嵌入各自的保守/激进方案 ──
    if buy_stocks:
        heading = (
            "有买入信号的股票"
            if allow_new_positions else
            "买入信号候选（组合风控禁止执行）"
        )
        lines.append(f"### 🟢 {heading}（{len(buy_stocks)} 只）\n")
        for bs in buy_stocks:
            lines.append(f"#### {bs['name']}（{bs['code']}）— 现价 {currency}{bs['price']:.2f}\n")
            plan_md = bs["op_plan"]
            if not allow_new_positions:
                lines.append(
                    "> 策略条件成立，但当前新增容量或可用资金为0；"
                    "本次不得买入/加仓，待释放风险容量后重新分析。\n"
                )
            elif isinstance(plan_md, str) and plan_md.strip():
                lines.append(_clean_plan_md(plan_md))
            else:
                lines.append(f"> {bs['buy_count']} 个策略发出买入信号，但系统未生成详细操作方案。\n")
            lines.append("")

    # ── 无信号的股票：嵌入候选策略 + 触发条件 ──
    if hold_stocks:
        lines.append(f"### ⚪ 无买入信号的股票（{len(hold_stocks)} 只）\n")
        lines.append("| 股票 | 类型 | 现价 | 最接近策略 | 状态 | 还缺什么 |")
        lines.append("|------|------|------:|------|:---:|------|")
        for hs in hold_stocks:
            top = hs.get("top_candidate") or {}
            typ = "持仓" if hs.get("is_holding") else "关注"
            lines.append(
                f"| {hs['name']}（{hs['code']}） | {typ} | {currency}{hs['price']:.2f} | "
                f"{top.get('name', '—')} | {top.get('audit', '—')} | {top.get('reason', '当前无可用候选条件')} |"
            )
        lines.append("")

    # ── 组合级仓位分配建议 ──
    lines.append(
        f"**组合信号统计**: {sell_count} 只持仓有策略退出/减仓信号，"
        f"{risk_count} 只触发组合风控提示，{support_count} 只触及关键支撑候选，"
        f"{buy_candidate_count}/{total} 只股票有买入候选，"
        f"其中 {buy_count} 只当前允许执行\n"
    )

    if buy_stocks:
        lines.append(f"**可用资金**: {currency}{available:,.2f}\n")

        sizing_rows = []
        for bs in buy_stocks:
            current_value = float(bs.get("position_value", 0.0) or 0.0)
            current_pct = current_value / account_equity if account_equity > 0 else 0.0
            correlated_exposure = sum(
                weight
                for code, weight in portfolio_risk.get("weights", {}).items()
                if code != bs["code"]
                and portfolio_risk.get("correlations", {}).get((bs["code"], code), 0.0) >= 0.75
            )
            correlation_cap = max(current_pct, 0.35 - correlated_exposure)
            target_pct = min(
                bs.get("target_pct", 0.0),
                0.25,
                correlation_cap,
            )
            target_value = account_equity * target_pct
            need_cash = max(target_value - current_value, 0.0)
            sizing_rows.append((
                bs, target_pct, current_value, need_cash,
                correlated_exposure > 0,
            ))

        total_need = sum(row[3] for row in sizing_rows)
        risk_capacity_cash = account_equity * portfolio_risk.get(
            "new_position_capacity_pct", 0.0
        )
        deployable_cash = min(float(available or 0.0), risk_capacity_cash)
        scale = min(1.0, deployable_cash / total_need) if total_need > 0 and deployable_cash > 0 else 0.0

        if total_need <= 0:
            if any(row[4] and row[2] <= 0 for row in sizing_rows):
                lines.append("> 高相关持仓已达到组合上限，本次买入信号不新增风险敞口。\n")
            else:
                lines.append("> 当前有买入信号的股票已达到或超过策略目标仓位，不建议继续加仓。\n")
        elif available > 0:
            lines.append("**资金分配建议**（按策略目标仓位，已扣除现有持仓市值）：\n")
            lines.append("| 股票 | 目标仓位 | 当前市值 | 建议新增金额 | 说明 |")
            lines.append("|------|------:|------:|------:|------|")
            for bs, target_pct, current_value, need_cash, correlation_limited in sizing_rows:
                actual_cash = need_cash * scale
                note = "资金充足" if scale >= 0.999 else f"可用资金不足，按 {scale:.0%} 缩放"
                if deployable_cash < float(available or 0.0) and scale < 0.999:
                    note = f"组合风险容量限制，按 {scale:.0%} 缩放"
                if correlation_limited:
                    note += "；已应用高相关持仓上限"
                if need_cash <= 0:
                    note = (
                        "高相关持仓已达到组合上限，禁止新增"
                        if correlation_limited and current_value <= 0
                        else "当前持仓已达到或超过目标仓位"
                    )
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

        if market:
            cached = Database().get_stock(code)
            if cached and cached.name and cached.name != code:
                return {"code": code, "name": cached.name, "market": market}

        if not market:
            # 尝试模糊搜索
            a_fallback = search_a_stock_fallback(code)
            if a_fallback:
                return a_fallback[0]
            contains_chinese = any("\u4e00" <= char <= "\u9fff" for char in code)
            results = (
                search_a_stock(code) or search_us_stock_online(code)
                if contains_chinese else
                search_us_stock_online(code) or search_a_stock(code)
            )
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
            fallback = search_a_stock_fallback(code)
            if fallback:
                return fallback[0]
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

    # ======================== 历史预测评估 ========================

    def build_historical_evaluation_panel(self, market: str = "US") -> str:
        """生成 Tab3 历史预测评估面板。"""
        market = market or "US"
        try:
            self.db.batch_verify_expired()
        except Exception as e:
            logger.warning(f"预测追踪验证失败: {e}")
        try:
            self.db.verify_due_forecasts()
            self.db.verify_due_trade_plans()
            self.db.verify_due_intraday_trade_plans()
        except Exception as e:
            logger.warning(f"新版独立预测验证失败: {e}")
        try:
            self.db.batch_verify_research_observations()
        except Exception as e:
            logger.warning(f"研究员观察验证失败: {e}")

        codes: list[tuple[str, str]] = [(f"PORTFOLIO_{market}", "组合整体")]
        seen = {codes[0][0]}
        for item in self.db.list_holdings(market) + self.db.list_watchlist(market):
            if item.code not in seen:
                label = item.name or item.code
                codes.append((item.code, f"{label}（{item.code}）"))
                seen.add(item.code)
        # Tab1 产生的预测也属于市场历史，不应因未录入 Tab3 持仓而消失。
        for code in self.db.list_prediction_codes(market):
            if code not in seen:
                stock = self.db.get_stock(code)
                label = stock.name if stock and stock.name else code
                codes.append((code, f"{label}（{code}）"))
                seen.add(code)

        grouped_forecasts: dict[tuple[str, int], list] = {}
        for forecast in self.db.get_forecasts(market=market, limit=5000):
            grouped_forecasts.setdefault((forecast.code, forecast.horizon), []).append(forecast)
        forecast_rows = []
        for (code, horizon), items in sorted(grouped_forecasts.items()):
            verified = [item for item in items if item.status == "verified"]
            stock = self.db.get_stock(code)
            label = f"{stock.name}（{code}）" if stock and stock.name else code
            count = len(verified)
            metrics = self.db.get_forecast_metrics(
                market=market, code=code, horizon=horizon,
            )
            forecast_rows.append({
                "label": label,
                "horizon": horizon,
                "verified": count,
                "pending": sum(item.status == "pending" for item in items),
                "unsupported": sum(item.status == "unsupported" for item in items),
                "accuracy": float(metrics.get("accuracy", 0.0)),
                "brier_score": float(metrics.get("brier_score", 0.0)),
                "log_loss": float(metrics.get("log_loss", 0.0)),
                "ece": float(metrics.get("ece", 0.0)),
                "interval_coverage": float(metrics.get("interval_coverage", 0.0)),
            })

        prediction_rows: list[dict] = []
        exit_rows: list[dict] = []
        for code, label in codes:
            panel = self.db.get_prediction_evaluation_panel(code)
            overall = panel.get("overall") or {}
            count = int(overall.get("count", 0) or 0)
            status_counts = self.db.get_prediction_status_counts(code)
            pending = int(status_counts.get("pending", 0) or 0)
            unsupported = int(status_counts.get("unsupported", 0) or 0)
            if count <= 0 and pending <= 0 and unsupported <= 0:
                continue
            prediction_rows.append({
                "label": label,
                "count": count,
                "pending": pending,
                "unsupported": unsupported,
                "accuracy": float(overall.get("accuracy", 0.0) or 0.0),
                "avg_return": float(overall.get("avg_return", 0.0) or 0.0),
                "expectancy": overall.get("expectancy", "insufficient"),
            })
            for exit_row in panel.get("exit_reviews") or []:
                exit_rows.append({"label": label, **exit_row})

        observation_rows = self.db.get_research_observation_overview(market=market, limit=12)
        plan_rows = self.db.get_trade_plan_metrics(market=market)
        forecast_summary = self.db.get_forecast_metrics(market=market)
        raw_joint_rows = self.db.get_joint_oof_runs(market=market, limit=200)
        joint_rows = []
        seen_joint_codes = set()
        for row in raw_joint_rows:
            code = str(row.get("code") or "")
            if code in seen_joint_codes:
                continue
            seen_joint_codes.add(code)
            row = self.db.get_joint_oof_health(code) or row
            stock = self.db.get_stock(code)
            row["label"] = f"{stock.name}（{code}）" if stock and stock.name else code
            joint_rows.append(row)
        minute_rows = self.db.get_intraday_evidence_overview(market)
        for row in minute_rows:
            code = str(row.get("code") or "")
            stock = self.db.get_stock(code)
            row["label"] = f"{stock.name}（{code}）" if stock and stock.name else code
        context_rows = self.db.get_feature_context_overview(market)
        for row in context_rows:
            code = str(row.get("code") or "")
            stock = self.db.get_stock(code)
            row["label"] = f"{stock.name}（{code}）" if stock and stock.name else code
        return _build_historical_evaluation_markdown(
            forecast_rows, plan_rows, prediction_rows, observation_rows, market,
            exit_rows=exit_rows,
            forecast_summary=forecast_summary,
            joint_rows=joint_rows,
            minute_rows=minute_rows,
            context_rows=context_rows,
        )

    def generate_forecast_calibration_chart(self, market: str = "US") -> str:
        """Render a reliability diagram for verified forecasts in one market."""
        metrics = self.db.get_forecast_metrics(market=market or "US")
        bins = metrics.get("calibration_bins") or []
        try:
            import os
            from config.settings import Settings

            chart_dir = Settings().chart_dir
            os.environ.setdefault("MPLCONFIGDIR", chart_dir)
            os.environ.setdefault("XDG_CACHE_HOME", chart_dir)
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            figure, axis = plt.subplots(figsize=(6.8, 3.6), dpi=140)
            axis.plot([0, 1], [0, 1], linestyle="--", color="#7b8794", label="Ideal")
            if bins:
                x = [float(item["mean_confidence"]) for item in bins]
                y = [float(item["accuracy"]) for item in bins]
                counts = [int(item["count"]) for item in bins]
                axis.plot(x, y, marker="o", linewidth=2, color="#1976d2", label="Observed")
                for px, py, count in zip(x, y, counts):
                    axis.annotate(
                        f"n={count}", (px, py), xytext=(5, 5),
                        textcoords="offset points", fontsize=8,
                    )
            else:
                axis.text(
                    0.5, 0.18,
                    "No verified samples yet\nObserved curve appears after target dates",
                    ha="center", va="center", color="#52606d", fontsize=11,
                    bbox={
                        "boxstyle": "round,pad=0.6", "facecolor": "#f4f7fa",
                        "edgecolor": "#c8d2dc",
                    },
                )
            axis.set(xlim=(0, 1), ylim=(0, 1), xlabel="Mean predicted confidence", ylabel="Observed accuracy")
            axis.set_title(f"{'US' if market == 'US' else 'A-share'} forecast calibration")
            axis.grid(alpha=0.2)
            axis.legend(loc="upper left")
            figure.tight_layout()
            path = os.path.join(chart_dir, f"forecast_calibration_{market}.png")
            figure.savefig(path, bbox_inches="tight")
            plt.close(figure)
            return path
        except Exception as exc:
            logger.warning(f"预测校准图生成失败: {exc}")
            return ""

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
        try:
            self.db.batch_verify_research_observations()
        except Exception as e:
            logger.warning(f"研究员观察验证失败: {e}")

        start, end = get_backtest_dates(period)
        all_codes = [h.code for h in holdings] + [w.code for w in watchlist]

        # Step 1: 拉取所有股票的价格数据
        if on_progress:
            on_progress(f"正在获取 {len(all_codes)} 只股票的 K 线数据...")

        holdings_data = []
        watchlist_data = []
        price_frames: dict[str, pd.DataFrame] = {}
        listing_date_map: dict[str, str] = {}
        fundamental_map: dict[str, dict | None] = {}
        news_items_map: dict[str, list] = {}
        quote_map: dict[str, dict] = {}
        quote_quality_map: dict[str, dict] = {}
        optimization_jobs: list[dict] = []

        should_fetch_quote = _should_fetch_realtime_quote(market, mode)
        if should_fetch_quote:
            if on_progress:
                on_progress(f"正在获取当前报价（0/{len(all_codes)}）...")
            from concurrent.futures import ThreadPoolExecutor, as_completed
            shared_fetcher = None if market == "US" and mode == "pre" else get_stock_fetcher(market)
            prefetched_quotes: dict[str, dict] = {}
            if shared_fetcher is not None and hasattr(shared_fetcher, "fetch_quotes"):
                prefetched_quotes = shared_fetcher.fetch_quotes(all_codes)
            # Nasdaq is a public endpoint; two workers avoid burst traffic.
            quote_workers = 2 if market == "US" and mode == "pre" else 1
            with ThreadPoolExecutor(max_workers=min(quote_workers, max(len(all_codes), 1))) as executor:
                futures = {
                    executor.submit(
                        _fetch_portfolio_realtime_quote, code, market,
                        shared_fetcher, mode, prefetched_quotes.get(code.upper()),
                        bool(shared_fetcher is not None and hasattr(shared_fetcher, "fetch_quotes")),
                    ): code
                    for code in all_codes
                }
                completed_quotes = 0
                for future in as_completed(futures):
                    code = futures[future]
                    completed_quotes += 1
                    if on_progress:
                        on_progress(
                            f"正在获取当前报价 {code}（{completed_quotes}/{len(all_codes)}）..."
                        )
                    try:
                        quote_data = future.result()
                        if quote_data and quote_data.get("price", 0) > 0:
                            quote_map[code] = quote_data
                    except Exception as e:
                        logger.warning(f"{code} 报价并发任务失败: {e}")
            quote_quality_map = {
                code: _evaluate_realtime_quote_quality(quote_map.get(code), mode)
                for code in all_codes
            }
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

        # K线与基本面彼此独立，有限并发预取，后续权益估算和逐股分析直接复用。
        if on_progress:
            on_progress(f"正在并行预取 K线、基本面和最新新闻（0/{len(all_codes)}）...")

        name_by_code = {
            item.code: item.name
            for item in list(holdings) + list(watchlist)
        }

        def _preload_stock(code: str):
            listing_date = resolve_listing_date(
                code, market, db=self.db, name=name_by_code.get(code, code)
            )
            frame = fetch_cached_prices(
                code, market, start, end, db=self.db, min_records=20,
                listing_date=listing_date,
            )
            fundamental = None
            try:
                from alpha.fundamental import get_fundamental_data
                fundamental = get_fundamental_data(
                    name_by_code.get(code, code), code, market
                )
            except Exception as e:
                logger.warning(f"{code} 基本面预取失败: {e}")
            news_items = []
            try:
                news_items = fetch_stock_news_items(
                    code=code,
                    name=name_by_code.get(code, code),
                    market=market,
                    mode=mode,
                    db=self.db,
                    limit=8,
                )
            except Exception as e:
                logger.warning(f"{code} 最新新闻预取失败: {e}")
            return frame, fundamental, listing_date, news_items

        from concurrent.futures import ThreadPoolExecutor, as_completed
        # Finnhub profile/fundamental/news share provider quotas. Two workers
        # keep first-run refreshes bounded while preserving useful parallelism.
        with ThreadPoolExecutor(max_workers=min(2, max(len(all_codes), 1))) as executor:
            futures = {executor.submit(_preload_stock, code): code for code in all_codes}
            completed_preloads = 0
            for future in as_completed(futures):
                code = futures[future]
                completed_preloads += 1
                if on_progress:
                    on_progress(
                        f"正在并行预取 K线、基本面和最新新闻（{completed_preloads}/{len(all_codes)}）{code}"
                    )
                try:
                    frame, fundamental, listing_date, news_items = future.result()
                    if frame is not None:
                        price_frames[code] = frame
                    fundamental_map[code] = fundamental
                    listing_date_map[code] = listing_date
                    news_items_map[code] = news_items
                except Exception as e:
                    logger.warning(f"{code} 数据预取任务失败: {e}")

        # SQLite 单例连接或外部 API 在并发阶段偶发失败时，进入权益计算前
        # 串行补齐整包数据，避免只补到 K 线而丢失基本面/新闻。
        incomplete_codes = [
            code for code in all_codes
            if code not in price_frames
            or code not in fundamental_map
            or code not in news_items_map
        ]
        for code in incomplete_codes:
            if on_progress:
                on_progress(f"正在补齐 {code} 的 K线、基本面和新闻...")
            try:
                frame, fundamental, listing_date, news_items = _preload_stock(code)
                if frame is not None:
                    price_frames[code] = frame
                fundamental_map[code] = fundamental
                listing_date_map[code] = listing_date
                news_items_map[code] = news_items
                logger.info(f"{code} 并发预取失败后已串行补齐")
            except Exception as e:
                logger.warning(f"{code} 串行补齐仍失败: {e}")

        if on_progress:
            on_progress("正在批量分析新增新闻情绪...")
        analyze_and_store_news(news_items_map, db=self.db)

        # 先获取所有价格并估算市场当前账户权益。组合页有真实余额和持仓，
        # 信号仓位应按这个权益换算，而不是 Tab1 的 10 万参考账户。
        cash_balance = float(balance.us_balance if market == "US" else balance.a_balance)
        account_equity, frozen_mark_prices = _estimate_account_equity(
            holdings, cash_balance, price_frames, quote_map, quote_quality_map
        )

        # 0 是真实账户状态，不得替换为仿真的 10 万元。回测可以使用
        # 独立参考本金，但当前下单信号必须看到真实的 0 权益。
        account_equity = max(account_equity, 0.0)
        backtest_capital = account_equity if account_equity > 0 else 100000.0

        for i, code in enumerate(all_codes):
            is_holding = i < len(holdings)
            if on_progress:
                on_progress(f"正在分析 {code}（{i+1}/{len(all_codes)}）...")

            try:
                # 获取价格（复用公共缓存+增量更新函数）
                df = price_frames.get(code)
                if df is None:
                    if on_progress:
                        on_progress(f"正在分析 {code}（{i+1}/{len(all_codes)}）：获取K线...")
                    df = fetch_cached_prices(code, market, start, end,
                                             db=self.db, min_records=20,
                                             listing_date=listing_date_map.get(code, ""))
                    if df is not None:
                        price_frames[code] = df
                if df is None:
                    logger.warning(f"{code} 数据不足（<20条），跳过")
                    continue

                # 当前操作价：盘中/盘前优先用实时报价；否则用 K 线最后收盘价。
                latest_date = str(df["date"].iloc[-1].strftime("%Y-%m-%d"))
                latest_close = float(df["close"].iloc[-1])
                quote_data = quote_map.get(code)
                quote_quality = quote_quality_map.get(code) or {
                    "required": False, "issues": [], "warnings": []
                }
                quote_usable = bool(
                    quote_data and quote_data.get("price", 0) > 0
                    and (not quote_quality.get("required") or quote_quality.get("fresh"))
                )
                if quote_usable:
                    current_price = float(quote_data["price"])
                    price_date = _format_quote_time(quote_data.get("timestamp", 0))
                    price_source = f"{quote_data.get('source', '实时报价')}（{price_date}）"
                else:
                    current_price = latest_close if latest_close > 0 else None
                    price_date = latest_date
                    price_source = (
                        f"K线收盘价（{latest_date}）；当前时段报价未通过逐股新鲜度检查"
                        if quote_quality.get("required")
                        else f"K线收盘价（{latest_date}）"
                    )
                if is_holding and code in frozen_mark_prices:
                    # 权益、持仓市值和仓位比例必须使用同一批冻结价格。
                    current_price = frozen_mark_prices[code]
                position_value = 0.0
                if is_holding and i < len(holdings) and current_price:
                    position_value = float(holdings[i].shares or 0) * float(current_price)

                # 使用 Tab3 本轮独立刷新并完成情感分析的新闻，不依赖 Tab1。
                news_df = news_items_to_df(news_items_map.get(code, []))

                fundamental_data = fundamental_map.get(code)

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
                        # 持仓没有建仓日期时，无法证明历史高点发生在持仓之后。
                        # 只使用当前已确认价格和成本，避免买入前的高点误触发移动止盈。
                        highest_close = max(float(current_price or 0.0), cost_price)
                        current_position = Position(
                            shares=shares,
                            avg_cost=cost_price,
                            entry_date="",
                            entry_price=cost_price,
                            highest_close=max(highest_close, cost_price),
                            stop_loss=cost_price * 0.92 if cost_price > 0 else 0.0,
                        )

                # 跑量化管道（跳过度参数，加速）
                if on_progress:
                    on_progress(f"正在分析 {code}（{i+1}/{len(all_codes)}）：策略回测与信号检查...")
                from services.forecast_service import get_forecast_configs
                result = run_pipeline(
                    df, news_df=news_df, market=market,
                    initial_capital=backtest_capital,
                    account_equity=account_equity,
                    current_position=current_position,
                    current_price=current_price,
                    current_bar=quote_data if quote_usable else None,
                    w_tech=w_tech, w_news=w_news,
                    fundamental_data=fundamental_data,
                    skip_param_tuning=True,
                    stock_code=code,
                    expand_pool=False,
                    realtime_quote_quality=quote_quality,
                    listing_date=listing_date_map.get(code, ""),
                    requested_history_start=start,
                    validation_mode=mode,
                    forecast_configs=get_forecast_configs(self.db, market, code),
                )
                optimization_jobs.append({
                    "stock_code": code,
                    "market": market,
                    "df": result.df,
                    "strategy_keys": result.active_strategies,
                    "initial_capital": backtest_capital,
                    "news_df": news_df,
                })

                # 提取 Alpha 最新得分
                decision_df = result.decision_df if result.decision_df is not None else result.df
                latest_score = None
                if "Final_Score" in decision_df.columns and not decision_df["Final_Score"].dropna().empty:
                    latest_score = float(decision_df["Final_Score"].dropna().iloc[-1])

                # 提取技术面摘要
                tech_summary = summarize_technical(decision_df, name=code)
                technical_marker = _latest_technical_marker(decision_df)

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
                    "reference_date": latest_date,
                    "price_source": price_source,
                    "technical": tech_summary,
                    "technical_marker": technical_marker,
                    "backtest": result.backtest,
                    "alpha_score": latest_score,
                    "news_summary": news_summary,
                    "_context_news_df": news_df,
                    "_context_fundamental_data": fundamental_data,
                    "market_regime": regime_label,
                    "market_regime_key": market_regime,
                    "rank_ic_info": rank_ic_info,
                    "fund_info": fund_info,
                    "benchmark_return": benchmark_return,
                    "regime_adapt_info": regime_adapt_info,
                    "data_quality": getattr(result, "data_quality", None),
                    # 新架构字段
                    "operation_plan": getattr(result, "operation_plan", None),
                    "signal_check": getattr(result, "signal_check", None),
                    "forecasts": getattr(result, "forecasts", None) or [],
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
                if on_progress:
                    on_progress(f"{code} 分析完成（{i+1}/{len(all_codes)}）")

            except Exception as e:
                logger.error(f"分析 {code} 失败: {e}")
                continue

        if not holdings_data and not watchlist_data:
            raise RuntimeError("所有股票的数据获取均失败，无法生成报告。")

        # Step 1.8: 构建组合级操作方案汇总
        portfolio_plan = _build_portfolio_operation_summary(
            holdings_data, watchlist_data, market, balance, account_equity,
            mode=mode, price_frames=price_frames,
        )

        all_items = holdings_data + watchlist_data
        all_health = []
        all_candidates = []
        try:
            for obj in all_items:
                item = obj.get("holding") or obj.get("watch_item")
                if item:
                    h = self.db.get_strategy_health_report(item.code)
                    for entry in h:
                        entry["stock_code"] = item.code
                    all_health.extend(h)
                    all_candidates.extend(
                        self.db.get_strategy_param_candidates(item.code)
                    )
        except Exception as e:
            logger.warning(f"组合策略健康度读取失败: {e}")

        # Step 2: 生成报告（代码方案在前，LLM 翻译在后）
        if on_progress:
            on_progress("正在生成综合持仓报告，LLM 解读可能需要 1-3 分钟，请稍候...")

        balance_dict = {
            "us_balance": balance.us_balance,
            "a_balance": balance.a_balance,
        }

        # 先拼代码生成章节（操作方案 → LLM 解读之前展示）
        portfolio_code = f"PORTFOLIO_{market}"
        market_label = "美股" if market == "US" else "A股"
        report_content = ""

        portfolio_forecast_section = ""
        try:
            from report.prompts import build_forecast_section
            portfolio_forecasts = [
                forecast
                for data in all_items
                for forecast in (data.get("forecasts") or [])
            ]
            portfolio_forecast_section = build_forecast_section(
                portfolio_forecasts,
                title="## 组合独立市场预测（代码生成）",
            )
            report_content += portfolio_forecast_section + "\n"
        except Exception as e:
            logger.warning(f"组合独立预测章节构建失败: {e}")

        try:
            from report.prompts import build_trust_hard_summary
            scoped_data_quality = []
            scoped_signals = []
            for data in all_items:
                item = data.get("holding") or data.get("watch_item")
                stock_code = item.code if item else ""
                if data.get("data_quality"):
                    quality = dict(data["data_quality"])
                    quality["_stock_code"] = stock_code
                    scoped_data_quality.append(quality)
                for signal in data.get("signal_check") or []:
                    scoped_signal = dict(signal)
                    scoped_signal["_stock_code"] = stock_code
                    scoped_signals.append(scoped_signal)
            trust_summary = build_trust_hard_summary(
                data_quality_reports=scoped_data_quality,
                audit_reports=[
                    {"summary": d.get("strategy_audit")}
                    for d in all_items
                    if d.get("strategy_audit")
                ],
                signal_checks=scoped_signals,
                prediction_stats=None,
                evaluation_panel=None,
                forecast_metrics=self.db.get_forecast_metrics(market=market),
                health_reports=all_health,
                scope=f"{market_label}组合：{len(holdings)}只持仓 + {len(watchlist)}只关注",
            )
            report_content += trust_summary + "\n"
        except Exception as e:
            logger.warning(f"组合可信度硬摘要构建失败: {e}")

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
            portfolio_operation_plan=(
                portfolio_forecast_section + "\n" + (portfolio_plan or "")
            ),
        )
        report_content += llm_report

        # 2c. 研究员观察候选池：LLM 可提出观察，系统负责确认/降级/反驳
        research_observations = []
        try:
            from services.research_observations import (
                build_research_confirmation_markdown,
                build_research_history_markdown,
                confirm_research_observations,
                apply_history_feedback,
            )
            research_observations = confirm_research_observations(
                holdings_data=holdings_data,
                watchlist_data=watchlist_data,
                llm_report=llm_report,
            )
            research_observations = apply_history_feedback(
                research_observations,
                lambda code, pattern: self.db.get_research_observation_stats(
                    code=code, pattern_type=pattern
                ),
            )
            research_section = build_research_confirmation_markdown(research_observations)
            if research_section:
                report_content += "\n\n" + research_section
            history_section = build_research_history_markdown(
                research_observations,
                lambda code, pattern: self.db.get_research_observation_stats(
                    code=code, pattern_type=pattern
                ),
            )
            if history_section:
                report_content += "\n" + history_section
        except Exception as e:
            logger.warning(f"研究员观察候选池构建失败: {e}")

        # 3c. 策略健康度追踪（持续优化闭环 — 汇总所有持仓+关注）
        try:
            if all_health or all_candidates:
                from report.prompts import build_strategy_health_section
                health_section = build_strategy_health_section(
                    all_health[:20], all_candidates[:20]
                )
                if health_section:
                    report_content += health_section
        except Exception as e:
            logger.warning(f"组合策略健康度构建失败: {e}")

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

        try:
            from services.forecast_service import persist_forecasts_and_plans
            for item in holdings_data + watchlist_data:
                obj = item.get("holding") or item.get("watch_item")
                if not obj:
                    continue
                persist_forecasts_and_plans(
                    self.db,
                    forecasts=item.get("forecasts") or [],
                    signals=item.get("signal_check") or [],
                    code=obj.code,
                    market=market,
                    mode=mode,
                    reference_date=str(item.get("reference_date") or "")[:10],
                    news_data=item.get("_context_news_df"),
                    fundamental_data=item.get("_context_fundamental_data"),
                    account_snapshot={
                        "account_equity": account_equity,
                        "cash": float(balance.us_balance if market == "US" else balance.a_balance),
                        "shares": float(getattr(obj, "shares", 0) or 0),
                        "cost_price": float(getattr(obj, "cost_price", 0) or 0),
                    },
                )
                self.db.verify_due_forecasts(code=obj.code)
                self.db.verify_due_trade_plans(code=obj.code)
                self.db.verify_due_intraday_trade_plans(code=obj.code)
            if mode == "intraday":
                from services.intraday_data_service import schedule_intraday_capture
                schedule_intraday_capture(all_codes, market)
        except Exception as e:
            logger.warning(f"Tab3 新版预测/交易方案入库失败: {e}")

        # Tab3 也要按单股+策略记录本次真实信号，否则策略健康度
        # 只会学到 Tab1，且无法评估组合报告中的减仓/退出建议。
        try:
            from services.analysis_service import AnalysisService
            for item in holdings_data + watchlist_data:
                obj = item.get("holding") or item.get("watch_item")
                if not obj:
                    continue
                signals = AnalysisService._prediction_signals(
                    SimpleNamespace(signal_check=item.get("signal_check") or [])
                )
                for signal in signals:
                    AnalysisService._save_prediction(
                        code=obj.code,
                        market=market,
                        mode=mode,
                        report_id=report_id,
                        reference_date=str(item.get("reference_date") or "")[:10],
                        direction=str(signal["direction"]),
                        final_score=float(item.get("alpha_score") or 0.0),
                        predicted_price=float(item.get("current_price") or 0.0),
                        conservative_entry=float(signal.get("entry_price", 0.0) or 0.0),
                        stop_loss=float(signal.get("stop_loss", 0.0) or 0.0),
                        take_profit=float(signal.get("take_profit", 0.0) or 0.0),
                        strategy_name=str(signal.get("strategy_name") or ""),
                        signal_action=str(signal.get("action") or ""),
                        market_regime=str(item.get("market_regime_key") or ""),
                    )
        except Exception as e:
            logger.warning(f"Tab3 逐策略预测写入失败: {e}")

        # 形态/观察记录入库：用于后续 1/3/5/10 日表现验证和风控官自我升级
        try:
            if research_observations:
                from services.research_observations import observations_to_logs
                logs = observations_to_logs(
                    research_observations,
                    market=market,
                    mode=mode,
                    report_id=report_id,
                    observed_at=report.create_time,
                )
                for log in logs:
                    self.db.insert_research_observation(log)
        except Exception as e:
            logger.warning(f"研究员观察记录入库失败: {e}")

        # 预测追踪：写入组合预测
        try:
            import json
            from services.analysis_service import _extract_direction
            direction = _extract_direction(report_content, 0.0)  # 组合无 Final_Score，默认 neutral
            from data.models import PredictionLog
            from datetime import datetime as _dt
            pred = PredictionLog(
                code=portfolio_code, market=market, mode=mode,
                report_id=report_id,
                predict_time=_dt.now().isoformat(),
                reference_date=max(
                    (
                        str(frame["date"].iloc[-1])[:10]
                        for frame in price_frames.values()
                        if frame is not None and not frame.empty and "date" in frame.columns
                    ),
                    default="",
                ),
                direction=direction,
                predicted_price=account_equity,
                verify_after_days=7,
                key_reason=f"组合分析：{len(holdings)}只持仓+{len(watchlist)}只关注",
                market_regime="portfolio",
                portfolio_snapshot=json.dumps({
                    "equity": account_equity,
                    "cash": float(
                        balance.us_balance if market == "US" else balance.a_balance
                    ),
                    "holdings": [
                        {
                            "code": h.code,
                            "shares": float(h.shares or 0.0),
                        }
                        for h in holdings
                        if float(h.shares or 0.0) > 0
                    ],
                }, ensure_ascii=False, sort_keys=True),
            )
            from services.analysis_service import LEGACY_PREDICTION_WRITES_ENABLED
            if LEGACY_PREDICTION_WRITES_ENABLED:
                self.db.insert_prediction(pred)
        except Exception as e:
            logger.warning(f"组合预测写入失败: {e}")

        # 依赖本次逐股预测写入的真实统计必须最后生成；旧逻辑在写入前只查
        # PORTFOLIO_US，并把“存在记录”硬编码成 1，导致组合报告严重少计。
        try:
            from report.prompts import (
                build_forecast_tracking_section,
                build_prediction_footer,
            )
            verified_forecasts = [
                forecast
                for stock_code in all_codes
                for forecast in self.db.get_forecasts(
                    code=stock_code, status="verified", limit=5,
                )
            ]
            report_content += build_forecast_tracking_section(
                verified_forecasts,
                self.db.get_forecast_metrics(market=market),
                title="## 组合成分股独立预测验证",
            )
            component_stats = self.db.get_prediction_stats_for_codes(
                all_codes, mode=mode,
            )
            component_validated = self.db.get_validated_predictions_for_codes(
                all_codes, mode=mode, limit=5,
            )
            pending_count = self.db.count_unverified_predictions_for_codes(
                all_codes, mode=mode,
            )
            tracking_mode_label = {
                "pre": "盘前", "intraday": "盘中", "eod": "盘后",
            }.get(mode, mode)
            if (
                int(getattr(component_stats, "total_predictions", 0) or 0) > 0
                or component_validated
                or pending_count > 0
            ):
                report_content += build_prediction_footer(
                    portfolio_code,
                    component_stats,
                    component_validated,
                    unverified_count=pending_count,
                    scope_label=f"当前组合成分股，{tracking_mode_label}模式",
                )
            self.db.update_report_content(report_id, report_content)
        except Exception as e:
            logger.warning(f"组合预测 footer 构建失败: {e}")

        try:
            from services.optimization_scheduler import schedule_deep_optimization
            submitted = 0
            for job in optimization_jobs:
                if schedule_deep_optimization(**job):
                    submitted += 1
                if submitted >= 3:
                    break
            if submitted:
                logger.info(f"Tab3 已提交 {submitted} 个后台深度优化任务")
        except Exception as e:
            logger.warning(f"组合后台参数优化调度失败（非致命）: {e}")

        return {
            "report_content": report_content,
            "report_id": report_id,
        }
