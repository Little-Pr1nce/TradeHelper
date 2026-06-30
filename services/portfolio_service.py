"""
持仓管理与综合分析服务。

负责：
  1. 持仓/关注/余额的 CRUD
  2. 股票代码搜索（自动识别市场 + 返显名称）
  3. 持仓综合分析：遍历持仓+关注 → 跑量化管道 → LLM 生成综合报告
"""

import logging
import math
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pandas as pd

from config.settings import Settings
from core.pipeline import run_pipeline
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
    return {
        "required": required,
        "available": price > 0,
        "fresh": fresh,
        "ohlc_complete": ohlc_complete,
        "age_seconds": age_seconds,
        "issues": issues,
        "warnings": warnings,
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


def _build_trigger_plan_row(item: dict, account_equity: float, currency: str) -> dict:
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

    if is_holding and price > 0 and ma60 > 0 and ma120 > 0:
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
) -> str:
    """组合级条件触发计划：盘中/盘前/盘后都输出可执行条件。"""
    if not all_data:
        return ""
    lines = [
        f"### 📌 {_mode_trigger_label(mode)}\n",
        "| 股票 | 类型 | 当前价 | 保守方案 | 激进方案 | 买入/加仓触发 | 卖出/减仓触发 | 失效条件 |",
        "|------|------|------:|------|------|------|------|------|",
    ]
    for item in all_data:
        row = _build_trigger_plan_row(item, account_equity, currency)
        price_str = f"{currency}{row['price']:.2f}" if row["price"] > 0 else "—"
        if not row.get("price_reliable", True) and price_str != "—":
            price_str += "（待复核）"
        lines.append(
            f"| {row['symbol']} | {row['state']} | {price_str} | "
            f"{row['conservative']} | {row['aggressive']} | "
            f"{row['buy_trigger']} | {row['sell_trigger']} | {row['invalidation']} |"
        )
    lines.append("")
    lines.append("> 这张表是代码生成的条件触发计划：盘中报告用于当日盯盘执行；盘前/盘后报告用于下一交易日盘中确认执行。\n")
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
    }.get(pattern_type or "", pattern_type or "未知形态")


def _build_historical_evaluation_markdown(
    prediction_rows: list[dict],
    observation_rows: list[dict],
    market: str,
    exit_rows: list[dict] | None = None,
) -> str:
    """构建历史预测评估面板 Markdown，用于 Tab3 UI 和报告复用。"""
    market_label = "美股" if market == "US" else "A股"
    lines = [
        f"### 📈 {market_label}历史预测评估面板\n",
        "> 只统计已到验证窗口并完成验证的记录；样本不足时不能作为可执行依据。\n",
    ]

    if prediction_rows:
        lines.extend([
            "| 标的 | 验证次数 | 方向正确率 | 平均方向净收益 | 期望 |",
            "|------|------:|------:|------:|------|",
        ])
        for row in prediction_rows:
            lines.append(
                f"| {row.get('label', '')} "
                f"| {int(row.get('count', 0))} "
                f"| {float(row.get('accuracy', 0)):.0%} "
                f"| {float(row.get('avg_return', 0)):+.2%} "
                f"| {_expectancy_label(str(row.get('expectancy', 'insufficient')))} |"
            )
        lines.append("")
    else:
        lines.append("- 暂无已验证预测记录。继续生成报告后，系统会自动积累并验证。\n")

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
            "| 股票 | 形态 | 等级 | 样本 | 5日胜率 | 5日均值 | 10日均值 | 最大不利 | 期望 |",
            "|------|------|------|------:|------:|------:|------:|------:|------|",
        ])
        for row in observation_rows:
            symbol = f"{row.get('name') or row.get('code')}（{row.get('code')}）"
            lines.append(
                f"| {symbol} "
                f"| {_pattern_label(str(row.get('pattern_type', '')))} "
                f"| {row.get('execution_level', '') or '—'} "
                f"| {int(row.get('count', 0))} "
                f"| {float(row.get('win_rate_5d', 0)):.0%} "
                f"| {float(row.get('avg_return_5d', 0)):+.2%} "
                f"| {float(row.get('avg_return_10d', 0)):+.2%} "
                f"| {float(row.get('avg_adverse', 0)):+.2%} "
                f"| {_expectancy_label(str(row.get('expectancy', 'insufficient')))} |"
            )
        lines.append("")

    lines.append("> 面板用于回答“系统过去是否有效”。正期望且风险可控的建议才允许进入更高执行等级。")
    return "\n".join(lines)


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
                return _quote_payload(
                    price=float(ext_data["price"]),
                    timestamp=ext_data.get("timestamp", 0),
                    source="Nasdaq.com延伸时段",
                    session=session,
                    bar=ext_data,
                )

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

        if market == "US":
            ext_data = fetch_us_extended_quote(code)
            if ext_data and ext_data.get("price", 0) > 0:
                return _quote_payload(
                    price=float(ext_data["price"]),
                    timestamp=ext_data.get("timestamp", 0),
                    source="Nasdaq.com报价",
                    session=session,
                    bar=ext_data,
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
        sells = [s for s in sc_list if s.get("signal") == "sell"]
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
    buy_count = len(buy_stocks)
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
        f"| 3 | 买入/加仓 | {buy_count} | 只在触发价、止损、仓位和历史验证同时成立时执行 |",
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
        demoted = []
        for line in md.splitlines():
            if line.startswith("### "):
                demoted.append("##### " + line[4:])
            elif line.startswith("## "):
                demoted.append("##### " + line[3:])
            else:
                demoted.append(line)
        return "\n".join(demoted).strip()

    trigger_plan = _build_conditional_trigger_plan(all_data, mode, currency, account_equity)
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
        f"{buy_count}/{total} 只股票有买入信号\n"
    )

    if buy_stocks:
        available = (
            balance.us_balance if market == "US" else balance.a_balance
        )
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

    # ======================== 历史预测评估 ========================

    def build_historical_evaluation_panel(self, market: str = "US") -> str:
        """生成 Tab3 历史预测评估面板。"""
        market = market or "US"
        try:
            self.db.batch_verify_expired()
        except Exception as e:
            logger.warning(f"预测追踪验证失败: {e}")
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

        prediction_rows: list[dict] = []
        exit_rows: list[dict] = []
        for code, label in codes:
            panel = self.db.get_prediction_evaluation_panel(code)
            overall = panel.get("overall") or {}
            count = int(overall.get("count", 0) or 0)
            if count <= 0:
                continue
            prediction_rows.append({
                "label": label,
                "count": count,
                "accuracy": float(overall.get("accuracy", 0.0) or 0.0),
                "avg_return": float(overall.get("avg_return", 0.0) or 0.0),
                "expectancy": overall.get("expectancy", "insufficient"),
            })
            for exit_row in panel.get("exit_reviews") or []:
                exit_rows.append({"label": label, **exit_row})

        observation_rows = self.db.get_research_observation_overview(market=market, limit=12)
        return _build_historical_evaluation_markdown(
            prediction_rows, observation_rows, market, exit_rows=exit_rows
        )

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
            with ThreadPoolExecutor(max_workers=min(4, max(len(all_codes), 1))) as executor:
                futures = {
                    executor.submit(
                        _fetch_portfolio_realtime_quote, code, market,
                        get_stock_fetcher(market),
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
        with ThreadPoolExecutor(max_workers=min(4, max(len(all_codes), 1))) as executor:
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

        if on_progress:
            on_progress("正在批量分析新增新闻情绪...")
        analyze_and_store_news(news_items_map, db=self.db)

        # 先获取所有价格并估算市场当前账户权益。组合页有真实余额和持仓，
        # 信号仓位应按这个权益换算，而不是 Tab1 的 10 万参考账户。
        account_equity = float(balance.us_balance if market == "US" else balance.a_balance)
        for h in holdings:
            try:
                df_h = price_frames.get(h.code)
                holding_quote = quote_map.get(h.code) or {}
                holding_quote_quality = quote_quality_map.get(h.code) or {}
                holding_quote_usable = bool(
                    holding_quote.get("price", 0) > 0
                    and (
                        not holding_quote_quality.get("required")
                        or holding_quote_quality.get("fresh")
                    )
                )
                if df_h is not None:
                    price_frames[h.code] = df_h
                    latest_close = float(df_h["close"].iloc[-1])
                    mark_price = (
                        float(holding_quote["price"])
                        if holding_quote_usable
                        else latest_close if latest_close > 0
                        else float(h.cost_price or 0)
                    )
                else:
                    mark_price = (
                        float(holding_quote["price"])
                        if holding_quote_usable
                        else float(h.cost_price or 0)
                    )
                account_equity += float(h.shares or 0) * mark_price
            except Exception as e:
                logger.warning(f"{h.code} 组合权益估算失败，使用成本价: {e}")
                account_equity += float(h.shares or 0) * float(h.cost_price or 0)

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

        try:
            from report.prompts import build_trust_hard_summary
            portfolio_eval = self.db.get_prediction_evaluation_panel(portfolio_code)
            portfolio_stats = self.db.get_prediction_stats(portfolio_code)
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
                prediction_stats=portfolio_stats,
                evaluation_panel=portfolio_eval,
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
            portfolio_operation_plan=portfolio_plan,
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
            self.db.insert_prediction(pred)
        except Exception as e:
            logger.warning(f"组合预测写入失败: {e}")

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
