"""
信号检查 + 策略排序 + 操作方案代码生成 — Phase 4 + 5

CLAUDE.md 新架构 ④⑤⑥ 层的核心实现：

  ④ 信号检查层: 每个 PASS/CONDITIONAL 策略 → generate_orders() → 是否发信号？
  ⑤ 策略排序层: 多维评分（审计判定 + 验证夏普 + 信号置信度 + 行情适配）
  ⑥ 操作方案层: Top 2-3 策略信号 → 保守/激进双方案 → 代码生成的 Markdown

核心原则:
  - 操作方案由代码生成，LLM 只负责解读
  - 所有价位来自策略 Order 对象（策略自己算出来的），不编造
  - 保守方案 = 最稳健策略，激进方案 = 最高收益策略
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
import numpy as np

from strategies.base import BaseExecutionStrategy, StrategyContext, Position, Order
from backtest.engine import BacktestResult

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class SignalResult:
    """单个策略变体的信号检查结果。"""
    variant_label: str             # "A_v1", "B", ...
    strategy_name: str             # 策略可读名
    base_key: str                  # "A", "B", ...
    signal: str                    # "buy" | "sell" | "no_signal"
    entry_price: float = 0.0       # 建议入场价
    stop_loss: float = 0.0         # 止损价
    take_profit: float = 0.0       # 止盈价（如有）
    position_pct: float = 0.0      # 建议仓位比例
    reason: str = ""               # 入场理由（来自 Order.reason）
    audit_verdict: str = ""        # PASS / CONDITIONAL
    test_sharpe: float = 0.0       # 验证期夏普
    rank_score: float = 0.0        # 综合排序分
    no_signal_reason: str = ""     # 无信号时：不满足的条件

    def to_dict(self) -> dict:
        return {
            "key": self.base_key,
            "variant": self.variant_label,
            "name": self.strategy_name,
            "signal": self.signal,
            "no_signal_reason": self.no_signal_reason,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "position_pct": self.position_pct,
            "reason": self.reason,
            "audit": self.audit_verdict,
            "test_sharpe": round(self.test_sharpe, 4),
            "rank_score": round(self.rank_score, 2),
        }


@dataclass
class OperationPlan:
    """操作方案（保守 + 激进 双方案）。"""
    conservative: dict | None = None   # {entry, stop_loss, position_pct, reason, source}
    aggressive: dict | None = None
    market_bias: str = "neutral"       # "bullish" | "bearish" | "neutral"
    account_equity: float = 100000.0
    equity_is_reference: bool = True
    markdown: str = ""


# ══════════════════════════════════════════════════════════════════
# ④ 信号检查层
# ══════════════════════════════════════════════════════════════════

def check_signals(
    df: pd.DataFrame,
    variants: list,          # list[StrategyVariant]
    market: str,
    initial_capital: float = 100000.0,
    account_equity: float | None = None,
    current_position: Position | None = None,
    current_price: float | None = None,
) -> list[SignalResult]:
    """
    对每个策略变体检查当前是否满足入场条件。

    调用 generate_orders(df, context) 模拟当前状态：
      - 如果返回 buy Order → 策略正在发信号
      - 如果返回空列表 → 当前不满足入场条件

    account_equity 为用户真实账户权益；未传入时使用 initial_capital 作为模型参考账户。
    current_position 为用户当前持仓；未传入时按空仓检查入场信号。
    """
    if df.empty or len(df) < 20:
        return []

    last = df.iloc[-1]
    date_str = str(last.get("date", ""))[:10]
    last_close = float(last.get("close", 0))
    signal_price = current_price if current_price and current_price > 0 else last_close

    # 计算市场波动率中位数
    med_vol = _compute_market_volatility(df)
    sizing_equity = account_equity if account_equity and account_equity > 0 else initial_capital

    results: list[SignalResult] = []
    for v in variants:
        try:
            context = StrategyContext(
                date=date_str,
                equity=sizing_equity,
                cash=sizing_equity,
                position=current_position or Position(),
                market=market,
                cooldown_until=-1,     # 无冷却
                holding_days=0,
                market_median_volatility=med_vol,
            )
            orders = v.strategy.generate_orders(df, context)
            buy_orders = [o for o in orders if o.action == "buy" and o.shares > 0]
            sell_orders = [o for o in orders if o.action == "sell" and o.shares > 0]
            signal = "buy" if buy_orders else ("sell" if sell_orders else "no_signal")

            sr = SignalResult(
                variant_label=v.variant_label,
                strategy_name=v.strategy.name,
                base_key=v.base_key,
                signal=signal,
            )

            if buy_orders:
                o = buy_orders[0]
                sr.entry_price = signal_price
                sr.stop_loss = o.stop_loss if o.stop_loss > 0 else signal_price * 0.92
                sr.take_profit = o.take_profit
                sr.position_pct = _derive_order_position_pct(o, signal_price, sizing_equity)
                sr.reason = o.reason or "策略入场条件满足"
            elif sell_orders:
                o = sell_orders[0]
                sr.entry_price = signal_price
                sr.position_pct = _derive_order_position_pct(o, signal_price, sizing_equity)
                sr.reason = o.reason or "策略退出条件满足"
            else:
                # 诊断为什么不满足
                sr.no_signal_reason = _diagnose_no_signal(df, v)

            results.append(sr)
        except Exception as e:
            logger.debug(f"信号检查失败 {v.variant_label}: {e}")
            results.append(SignalResult(
                variant_label=v.variant_label,
                strategy_name=v.strategy.name,
                base_key=v.base_key,
                signal="no_signal",
                reason=f"检查失败: {e}",
            ))

    buy_count = sum(1 for r in results if r.signal == "buy")
    logger.info(f"信号检查: {len(results)} 策略, {buy_count} 个发出买入信号")
    return results


def _compute_market_volatility(df: pd.DataFrame) -> float:
    """计算近期波动率中位数（用于策略上下文）。"""
    if len(df) < 20:
        return 0.02
    close = df["close"].astype(float)
    returns = close.pct_change().dropna()
    if len(returns) < 10:
        return 0.02
    return float(returns.iloc[-20:].std() * np.sqrt(252))


def _derive_position_pct(reason: str, strategy) -> float:
    """从策略理由中推导建议仓位比例。"""
    name = strategy.name.lower()
    if "mean" in name or "reversion" in name or "均值回归" in name:
        return 0.30
    if "ma60" in name or "中长期" in name or "trend rider" in name or "满仓" in name:
        return 0.70
    if "momentum" in name or "动量" in name:
        return 0.50
    return 0.40


def _derive_order_position_pct(order: Order, reference_price: float, equity: float) -> float:
    """Derive position sizing from the actual strategy order."""
    if reference_price <= 0 or equity <= 0 or order.shares <= 0:
        return 0.0
    return float(np.clip(order.shares * reference_price / equity, 0.0, 1.0))


def _diagnose_no_signal(df: pd.DataFrame, variant) -> str:
    """诊断策略为什么不发买入信号，给出可执行的触发条件。"""
    if len(df) < 20:
        return "数据不足（<20条K线）"

    from strategies.base import compute_percentile_score
    last = df.iloc[-1]
    score_col = "Final_Score"
    if score_col not in df.columns:
        return "缺少 Final_Score 列"

    try:
        pct_series = compute_percentile_score(df)
        current_pct = float(pct_series.iloc[-1]) if not pct_series.empty and pd.notna(pct_series.iloc[-1]) else 0
        current_score = float(df[score_col].iloc[-1]) if pd.notna(df[score_col].iloc[-1]) else 0
        close = float(last.get("close", 0))
        # 估计触发所需 Score（基于滚动252日分位分布）
        scores = df[score_col].dropna()
        if len(scores) >= 60:
            target_score = float(scores.quantile(0.65)) if current_pct < 0.65 else None
        else:
            target_score = None
    except Exception:
        return "计算百分位失败"

    # 从变体参数取，如果没有则从策略实例取（默认策略 params={}）
    params = getattr(variant, "params", {}) or {}
    strategy = getattr(variant, "strategy", None)
    entry_pct = params.get("entry_pct") or (getattr(strategy, "entry_pct", None) if strategy else None)
    exit_pct = params.get("exit_pct") or (getattr(strategy, "exit_pct", None) if strategy else None)
    key = variant.base_key if hasattr(variant, "base_key") else ""
    parts = []

    # ── 百分位阈值型（A/D/E/F/C/G）──
    if entry_pct is not None and current_pct < entry_pct:
        gap = entry_pct - current_pct
        target_info = f"（需Score≈{target_score:+.3f}以上）" if target_score else ""
        parts.append(f"Score百分位{current_pct:.0%}→需≥{entry_pct:.0%}{target_info}，还差约{gap:.0%}")

    # ── 均值回归型（B）──
    if key == "B":
        if current_pct > (entry_pct or 0.20):
            parts.append(f"Score百分位{current_pct:.0%}>超卖阈值{entry_pct or 0.20:.0%}，需等Score继续走弱进入超卖区")

    # ── MA60 趋势型（H/O）──
    if key in ("H", "O"):
        ma60 = last.get("ma_60")
        ma20 = last.get("ma_20")
        if ma60 and pd.notna(ma60) and close <= float(ma60):
            gap_price = float(ma60) - close
            parts.append(f"收盘价{close:.2f}<MA60({float(ma60):.2f})，需涨{gap_price:+.2f}站上MA60")
        if ma20 and ma60 and pd.notna(ma20) and pd.notna(ma60) and float(ma20) <= float(ma60):
            gap_ma = float(ma60) - float(ma20)
            parts.append(f"MA20({float(ma20):.2f})≤MA60({float(ma60):.2f})，需MA20上行{gap_ma:+.2f}形成金叉")

    # ── MA 交叉型（G）──
    if key == "G":
        ma5 = last.get("ma_5")
        ma20 = last.get("ma_20")
        if ma5 and ma20 and pd.notna(ma5) and pd.notna(ma20) and float(ma5) <= float(ma20):
            gap = float(ma20) - float(ma5)
            parts.append(f"MA5({float(ma5):.2f})≤MA20({float(ma20):.2f})，需MA5上行{gap:+.2f}金叉")

    # ── Final_Score 方向 ──
    if current_score < -0.05 and (not parts or entry_pct):
        parts.append(f"Final_Score={current_score:+.3f}偏空，关注Score转正信号")

    if not parts:
        parts.append(f"Score百分位={current_pct:.0%}, Final_Score={current_score:+.3f}")

    return "；".join(parts[:3])


# ══════════════════════════════════════════════════════════════════
# ⑤ 策略排序层
# ══════════════════════════════════════════════════════════════════

def rank_signals(
    signals: list[SignalResult],
    audit_entries: list | None = None,  # list[StrategyAuditEntry]
    backtest_results: dict | None = None,
    health_data: list[dict] | None = None,  # 策略健康度数据（持续优化闭环）
) -> list[SignalResult]:
    """
    多维度综合排序。

    评分权重:
      1. 审计判定: PASS=40分, CONDITIONAL=20分, 其他=0分 (40%)
      2. 验证期夏普: 归一化到 0-30 分 (30%)
      3. 是否有买入信号: 有=20分, 无=0分 (20%)
      4. 行情适配 + 健康度: 完全匹配=10分, 部分=5分 (10%)
      5. 健康度惩罚: 降级策略 -15分, 观察策略 -5分

    返回排序后的信号列表（附带 rank_score）。
    """
    if not signals:
        return []

    # 构建 lookup map
    audit_map: dict[str, any] = {}
    if audit_entries:
        for e in audit_entries:
            audit_map[getattr(e, "strategy_key", "")] = e
            audit_map[e.strategy_name] = e

    bt_map: dict[str, BacktestResult] = backtest_results or {}

    for s in signals:
        score = 0.0

        # 1. 审计判定 (40%)
        e = audit_map.get(s.variant_label) or audit_map.get(s.strategy_name)
        if e:
            s.audit_verdict = getattr(e, "verdict", "")
            s.test_sharpe = getattr(e, "test_sharpe", 0.0)
            if s.audit_verdict == "PASS":
                score += 40.0
            elif s.audit_verdict == "CONDITIONAL":
                score += 20.0
        else:
            score += 10.0  # 无审计数据，给基本分

        # 2. 验证夏普 (30%)
        test_sharpe = s.test_sharpe
        if test_sharpe > 2.0:
            score += 30.0
        elif test_sharpe > 1.0:
            score += 20.0
        elif test_sharpe > 0.5:
            score += 10.0
        elif test_sharpe > 0:
            score += 5.0
        # 负夏普不加分

        # 3. 当前信号 (20%)
        if s.signal in ("buy", "sell"):
            score += 20.0

        # 4. 信号置信度 (10%) — 根据入场理由中的信息量
        if s.reason and len(s.reason) > 20:
            score += 10.0
        elif s.reason:
            score += 5.0

        # 5. 健康度惩罚（持续优化闭环）— 基于 prediction_log 实际表现
        if health_data:
            for h in health_data:
                if h["strategy_name"] == s.strategy_name:
                    if h.get("action") == "demote":
                        score -= 15.0
                        s.reason = f"[健康度降级] {s.reason}"
                    elif h.get("action") == "watch":
                        score -= 5.0
                    break

        s.rank_score = round(score, 1)

    # 排序
    return sorted(signals, key=lambda x: x.rank_score, reverse=True)


# ══════════════════════════════════════════════════════════════════
# ⑥ 操作方案生成层
# ══════════════════════════════════════════════════════════════════

def generate_operation_plan(
    ranked_signals: list[SignalResult],
    current_price: float,
    market_bias: str = "neutral",
    df: pd.DataFrame | None = None,
    account_equity: float | None = None,
) -> OperationPlan:
    """
    从排序后的信号中选 Top 2-3 策略，生成保守 + 激进双方案。

    保守方案 (🛡️):
      - 选 audit=PASS 中排名最高的策略
      - 止损给足（策略默认止损 × 1.0）
      - 仓位 30-40%

    激进方案 (🚀):
      - 选 rank_score 最高的策略（即使 audit=CONDITIONAL）
      - 止损可适度放宽
      - 仓位 50-70%

    Args:
        ranked_signals: rank_signals() 排序后的信号列表
        current_price: 当前价格
        market_bias: 市场方向偏好多/空/中性

    Returns:
        OperationPlan 含两套方案
    """
    sizing_equity = account_equity if account_equity and account_equity > 0 else 100000.0
    equity_is_reference = not (account_equity and account_equity > 0)

    if not ranked_signals:
        return OperationPlan(market_bias=market_bias, account_equity=sizing_equity,
                             equity_is_reference=equity_is_reference)

    sell_signals = [s for s in ranked_signals if s.signal == "sell"]
    if sell_signals:
        plan = OperationPlan(market_bias=market_bias, account_equity=sizing_equity,
                             equity_is_reference=equity_is_reference)
        plan.markdown = _build_sell_signal_markdown(sell_signals, current_price, sizing_equity,
                                                    equity_is_reference)
        return plan

    buy_signals = [s for s in ranked_signals if s.signal == "buy"]
    if not buy_signals:
        plan = OperationPlan(market_bias=market_bias, account_equity=sizing_equity,
                             equity_is_reference=equity_is_reference)
        plan.markdown = _build_no_signal_markdown(ranked_signals, market_bias, df)
        return plan

    # ── 保守方案 ──
    conservative = None
    pass_signals = [s for s in buy_signals if s.audit_verdict == "PASS"]
    cons_signal = pass_signals[0] if pass_signals else (
        buy_signals[0] if buy_signals else None)

    if cons_signal:
        entry = cons_signal.entry_price or current_price
        stop = cons_signal.stop_loss or entry * 0.92
        position_cap = 0.25 if market_bias == "bearish" else 0.40
        conservative = {
            "entry": entry,
            "stop_loss": stop,
            "position_pct": min(cons_signal.position_pct, position_cap),
            "signal_strength": _signal_strength(cons_signal),
            "max_loss_amount": _max_loss_amount(sizing_equity, min(cons_signal.position_pct, position_cap), entry, stop),
            "invalidation": _invalidation_conditions(entry, stop, market_bias),
            "reason": cons_signal.reason or "策略入场条件满足",
            "source_strategies": [cons_signal.strategy_name],
        }

    # ── 激进方案 ──
    aggressive = None
    agg_signal = buy_signals[0] if buy_signals else (
        ranked_signals[0] if ranked_signals else None)

    if agg_signal and agg_signal != cons_signal:
        entry = agg_signal.entry_price or current_price
        stop = agg_signal.stop_loss or entry * 0.90
        position_cap = 0.35 if market_bias == "bearish" else 0.70
        aggressive = {
            "entry": entry,
            "stop_loss": stop,
            "position_pct": min(agg_signal.position_pct * 1.25, position_cap),
            "signal_strength": _signal_strength(agg_signal),
            "max_loss_amount": _max_loss_amount(sizing_equity, min(agg_signal.position_pct * 1.25, position_cap), entry, stop),
            "invalidation": _invalidation_conditions(entry, stop, market_bias),
            "reason": agg_signal.reason or "策略入场条件满足",
            "source_strategies": [agg_signal.strategy_name],
        }
    elif agg_signal and not aggressive:
        # 只有一个信号 → 激进方案 = 同一策略但适度放大仓位，不改写策略止损
        entry = agg_signal.entry_price or current_price
        stop = agg_signal.stop_loss or entry * 0.92
        position_cap = 0.30 if market_bias == "bearish" else 0.60
        aggressive = {
            "entry": entry,
            "stop_loss": stop,
            "position_pct": min(agg_signal.position_pct * 1.25, position_cap),
            "signal_strength": _signal_strength(agg_signal),
            "max_loss_amount": _max_loss_amount(sizing_equity, min(agg_signal.position_pct * 1.25, position_cap), entry, stop),
            "invalidation": _invalidation_conditions(entry, stop, market_bias),
            "reason": (agg_signal.reason or "策略入场条件满足") + "（同一信号，仓位略高）",
            "source_strategies": [agg_signal.strategy_name],
        }

    plan = OperationPlan(
        conservative=conservative,
        aggressive=aggressive,
        market_bias=market_bias,
        account_equity=sizing_equity,
        equity_is_reference=equity_is_reference,
    )
    plan.markdown = _build_plan_markdown(plan, ranked_signals, current_price)
    return plan


# ══════════════════════════════════════════════════════════════════
# Markdown 格式化
# ══════════════════════════════════════════════════════════════════

def _build_no_signal_markdown(ranked_signals, market_bias, df=None) -> str:
    """当没有买入信号时，输出 Top 3 候选 + 关键价位 + 保守/激进建议。"""
    bias_str = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}
    bias_label = bias_str.get(market_bias, market_bias)
    lines = [
        "\n---\n",
        "## 🎯 系统操作方案（代码生成）\n",
        f"> 🔒 当前无策略发出买入信号 | 市场方向: **{bias_label}** | 有效窗口: **5 个交易日**\n",
    ]

    # ── 关键价位速查 ──
    if df is not None and len(df) > 0:
        last = df.iloc[-1]
        close = float(last.get("close", 0))
        lines.append("### 📍 关键价位速查\n")
        prices = []
        for col, label in [("ma_5", "MA5"), ("ma_10", "MA10"), ("ma_20", "MA20"), ("ma_60", "MA60")]:
            v = last.get(col)
            if v and pd.notna(v) and float(v) > 0:
                gap = (close - float(v)) / float(v) * 100
                prices.append(f"{label}=${float(v):.2f}({'上' if gap>0 else '下'}{abs(gap):.1f}%)")
        if prices:
            lines.append(" | ".join(prices))
        rsi = last.get("rsi")
        if rsi and pd.notna(rsi):
            lines.append(f" | RSI={float(rsi):.0f}")
        lines.append("")
        lines.append("")

    # ── Top 3 候选策略 ──
    top3 = ranked_signals[:3]
    if top3:
        lines.append("### 🔍 Top 3 候选策略（最接近触发）\n")
        for i, s in enumerate(top3):
            v_emoji = {"PASS": "✅", "CONDITIONAL": "⚠️"}.get(s.audit_verdict, "")
            lines.append(f"**{i+1}. {s.strategy_name[:25]}** {v_emoji} (夏普{s.test_sharpe:.2f}, 评分{s.rank_score:.0f})")
            if s.no_signal_reason:
                lines.append(f"   需满足: {s.no_signal_reason[:130]}")
            lines.append("")

    # ── 保守建议 ──
    lines.append("### 🛡️ 保守建议\n")
    if top3:
        lines.append(f"等待 **{top3[0].strategy_name[:20]}** 条件满足后再入场。")
    lines.append("当前所有策略均未触发。保持观望，等策略信号确认后操作。若 5 个交易日内条件未满足，方案自动失效。\n")

    # ── 激进建议 ──
    lines.append("### 🚀 激进建议\n")
    closest = top3[0] if top3 else None
    if closest:
        lines.append(f"**{closest.strategy_name[:20]}** 最接近触发。")
        if closest.no_signal_reason:
            lines.append(f"还需: {closest.no_signal_reason[:120]}")
    lines.append("如需抢先入场，可等上述条件部分满足后轻仓试探。不建议在条件未满足时盲目入场。\n")
    lines.append("### 📡 全策略信号状态\n")
    lines.append("| 排名 | 策略 | 信号 | 审计 | 夏普 | 评分 | 不满足的条件 |")
    lines.append("|:---:|------|:---:|:---:|:---:|:---:|------|")
    for i, s in enumerate(ranked_signals[:10]):
        rank = i + 1
        sig_emoji = "🔴" if s.signal == "no_signal" else "🟢"
        audit_emoji = {"PASS": "✅", "CONDITIONAL": "⚠️", "": "—"}.get(s.audit_verdict, "—")
        reason = s.no_signal_reason[:55] if s.no_signal_reason else "—"
        lines.append(
            f"| {rank} | {s.strategy_name[:20]} "
            f"| {sig_emoji} "
            f"| {audit_emoji} "
            f"| {s.test_sharpe:.2f} "
            f"| {s.rank_score:.0f} "
            f"| {reason} |"
        )
    lines.append("")
    lines.append("*以上方案由系统自动生成，具体执行请结合个人风险偏好。*\n")
    return "\n".join(lines)


def _build_sell_signal_markdown(
    sell_signals: list[SignalResult],
    current_price: float,
    account_equity: float,
    equity_is_reference: bool,
) -> str:
    """生成持仓退出/减仓信号 Markdown。"""
    equity_label = "参考账户权益" if equity_is_reference else "账户权益"
    lines = [
        "\n---\n",
        "## 🎯 系统操作方案（代码生成）\n",
        "> ⚠️ 以下方案由系统基于策略审计和实时信号**自动生成**，非 LLM 建议。\n",
        "### 🔴 持仓退出/减仓信号\n",
        f"> 仓位金额按 {equity_label} ${account_equity:,.2f} 估算。\n",
        "| 策略 | 当前价 | 持仓占权益 | 估算持仓金额 | 触发原因 |",
        "|------|------:|------:|------:|------|",
    ]

    for s in sell_signals[:5]:
        reason = s.reason or "策略退出条件满足"
        position_value = account_equity * s.position_pct
        lines.append(
            f"| {s.strategy_name[:24]} | ${current_price:.2f} | "
            f"{s.position_pct*100:.1f}% | ${position_value:,.0f} | {reason[:100]} |"
        )

    lines.extend([
        "",
        "> 执行含义：这些是已持仓场景下的退出信号；如用户没有实际持仓，应忽略卖出指令。",
        "",
    ])
    return "\n".join(lines)


def _build_plan_markdown(
    plan: OperationPlan,
    ranked_signals: list[SignalResult],
    current_price: float,
) -> str:
    """构建操作方案 Markdown（代码生成，不是 LLM）。"""
    lines = [
        "\n---\n",
        "## 🎯 系统操作方案（代码生成）\n",
        f"> ⚠️ 以下方案由系统基于策略审计和实时信号**自动生成**，非 LLM 建议。\n",
        f"> ⏱ 有效窗口: **5 个交易日** | 若到期未触发入场条件，下次分析时重新评估。\n",
    ]

    # ── 市场偏向 ──
    bias_emoji = {"bullish": "📈 偏多", "bearish": "📉 偏空", "neutral": "📊 中性"}
    lines.append(f"**当前市场方向判断**: {bias_emoji.get(plan.market_bias, plan.market_bias)}\n")
    equity_label = "参考账户权益" if plan.equity_is_reference else "账户权益"
    lines.append(f"**风控口径**: {equity_label} ${plan.account_equity:,.2f}\n")

    # ── 保守方案 ──
    if plan.conservative:
        c = plan.conservative
        lines.append("### 🛡️ 保守方案\n")
        lines.append("| 项目 | 数值 | 理由 |")
        lines.append("|------|------|------|")
        lines.append(f"| 入场价 | **${c['entry']:.2f}** | 当前收盘价附近，等回调/确认后入场 |")
        c_loss = _loss_pct(c["entry"], c["stop_loss"])
        c_account_risk = _account_risk_pct(c["position_pct"], c_loss)
        lines.append(f"| 止损价 | **${c['stop_loss']:.2f}** | 策略止损，单价风险 {c_loss:.1f}% |")
        lines.append(f"| 信号强度 | **{c['signal_strength']}** | 基于审计结论、验证夏普和综合排名 |")
        lines.append(f"| 仓位 | **{c['position_pct']*100:.0f}%** 账户权益 | 来自策略订单股数，按保守上限裁剪 |")
        lines.append(f"| 账户风险 | **{c_account_risk:.2f}%** 净值 | 若触发止损，按仓位估算的组合层面亏损 |")
        lines.append(f"| 最大亏损 | **${c['max_loss_amount']:,.0f}** | 仓位金额 × 单价风险，未计跳空和滑点 |")
        lines.append(f"| 失效条件 | {c['invalidation']} | 任一条件触发即放弃/重算方案 |")
        lines.append(f"| 来源策略 | {', '.join(c['source_strategies'])} | 审计 PASS，验证夏普稳定 |")
        lines.append(f"| 入场逻辑 | {c['reason'][:100]} | — |")
        lines.append("")

    # ── 激进方案 ──
    if plan.aggressive:
        a = plan.aggressive
        lines.append("### 🚀 激进方案\n")
        lines.append("| 项目 | 数值 | 理由 |")
        lines.append("|------|------|------|")
        lines.append(f"| 入场价 | **${a['entry']:.2f}** | 当前收盘价，不等回调直接入场 |")
        a_loss = _loss_pct(a["entry"], a["stop_loss"])
        a_account_risk = _account_risk_pct(a["position_pct"], a_loss)
        lines.append(f"| 止损价 | **${a['stop_loss']:.2f}** | 策略止损，单价风险 {a_loss:.1f}% |")
        lines.append(f"| 信号强度 | **{a['signal_strength']}** | 基于审计结论、验证夏普和综合排名 |")
        lines.append(f"| 仓位 | **{a['position_pct']*100:.0f}%** 账户权益 | 来自策略订单股数，激进上限裁剪 |")
        lines.append(f"| 账户风险 | **{a_account_risk:.2f}%** 净值 | 若触发止损，按仓位估算的组合层面亏损 |")
        lines.append(f"| 最大亏损 | **${a['max_loss_amount']:,.0f}** | 仓位金额 × 单价风险，未计跳空和滑点 |")
        lines.append(f"| 失效条件 | {a['invalidation']} | 任一条件触发即放弃/重算方案 |")
        lines.append(f"| 来源策略 | {', '.join(a['source_strategies'])} | 当前排名最高信号 |")
        lines.append(f"| 入场逻辑 | {a['reason'][:100]} | — |")
        lines.append("")

    # ── 两方案对比 ──
    if plan.conservative and plan.aggressive:
        c, a = plan.conservative, plan.aggressive
        lines.append("### 📊 方案对比\n")
        lines.append("| 维度 | 🛡️ 保守 | 🚀 激进 |")
        lines.append("|------|:------:|:------:|")
        lines.append(f"| 入场价 | ${c['entry']:.2f} | ${a['entry']:.2f} |")
        lines.append(f"| 止损 | ${c['stop_loss']:.2f} (风险{_loss_pct(c['entry'], c['stop_loss']):.1f}%) | ${a['stop_loss']:.2f} (风险{_loss_pct(a['entry'], a['stop_loss']):.1f}%) |")
        lines.append(f"| 仓位 | {c['position_pct']*100:.0f}% | {a['position_pct']*100:.0f}% |")
        lines.append(f"| 账户风险 | {_account_risk_pct(c['position_pct'], _loss_pct(c['entry'], c['stop_loss'])):.2f}% | {_account_risk_pct(a['position_pct'], _loss_pct(a['entry'], a['stop_loss'])):.2f}% |")
        lines.append(f"| 最大亏损 | ${c['max_loss_amount']:,.0f} | ${a['max_loss_amount']:,.0f} |")
        lines.append(f"| 风险收益比 | 保守 | 激进 |")
        lines.append("")

    # ── 信号详情表 ──
    if ranked_signals:
        lines.append("### 📡 全策略信号状态\n")
        lines.append("| 排名 | 策略 | 信号 | 审计 | 夏普 | 评分 | 不满足的条件 |")
        lines.append("|:---:|------|:---:|:---:|:---:|:---:|------|")
        for i, s in enumerate(ranked_signals[:10]):
            rank = i + 1
            sig_emoji = "🟢" if s.signal == "buy" else ("🔴" if s.signal == "sell" else "⚪")
            audit_emoji = {"PASS": "✅", "CONDITIONAL": "⚠️", "": "—"}.get(s.audit_verdict, "—")
            reason = (
                s.reason[:50] if s.signal == "sell" and s.reason else
                s.no_signal_reason[:50] if s.no_signal_reason else
                ("—" if s.signal == "buy" else "")
            )
            lines.append(
                f"| {rank} | {s.strategy_name[:20]} "
                f"| {sig_emoji} "
                f"| {audit_emoji} "
                f"| {s.test_sharpe:.2f} "
                f"| {s.rank_score:.0f} "
                f"| {reason} |"
            )
        lines.append("")

    lines.append("*以上方案由系统自动生成，具体执行请结合个人风险偏好。*\n")
    return "\n".join(lines)


def _loss_pct(entry: float, stop_loss: float) -> float:
    """Return downside from entry to stop as a positive percent."""
    if entry <= 0 or stop_loss <= 0:
        return 0.0
    return max((entry - stop_loss) / entry * 100, 0.0)


def _signal_strength(signal: SignalResult) -> str:
    """Human-readable signal strength."""
    if signal.audit_verdict == "PASS" and signal.test_sharpe >= 1.0 and signal.rank_score >= 70:
        return "强"
    if signal.audit_verdict in ("PASS", "CONDITIONAL") and signal.rank_score >= 45:
        return "中"
    return "弱"


def _max_loss_amount(account_equity: float, position_pct: float, entry: float, stop_loss: float) -> float:
    """Max planned loss before gap/slippage."""
    return account_equity * max(position_pct, 0.0) * _loss_pct(entry, stop_loss) / 100


def _invalidation_conditions(entry: float, stop_loss: float, market_bias: str) -> str:
    """Return concise invalidation rules for a generated trade plan."""
    parts = [
        "5个交易日未触发/未成交",
        f"收盘价跌破止损 ${stop_loss:.2f}",
        "策略信号消失或降级为 FAIL",
    ]
    if market_bias == "bearish":
        parts.append("大盘偏空继续恶化")
    elif market_bias == "bullish":
        parts.append("价格冲高超过计划入场价 3% 仍未成交")
    return "；".join(parts)


def _account_risk_pct(position_pct: float, loss_pct: float) -> float:
    """Return portfolio-level risk percent for a position and stop distance."""
    if position_pct <= 0 or loss_pct <= 0:
        return 0.0
    return position_pct * loss_pct


# ══════════════════════════════════════════════════════════════════
# 主入口函数（供 pipeline 调用）
# ══════════════════════════════════════════════════════════════════

def run_signal_check(
    df: pd.DataFrame,
    variants: list,          # list[StrategyVariant] — PASS + CONDITIONAL
    market: str,
    audit_entries: list | None = None,
    backtest_results: dict | None = None,
    current_price: float | None = None,
    final_score: float = 0.0,
    account_equity: float | None = None,
    current_position: Position | None = None,
    health_data: list[dict] | None = None,  # 策略健康度数据
) -> tuple[list[SignalResult], OperationPlan | None]:
    """
    一步完成：信号检查 → 排序 → 操作方案生成。

    Returns:
        (ranked_signals, operation_plan)
    """
    if not variants:
        return [], None

    # ④ 信号检查
    signals = check_signals(
        df, variants, market,
        account_equity=account_equity,
        current_position=current_position,
        current_price=current_price,
    )

    # ⑤ 策略排序（含健康度数据）
    ranked = rank_signals(signals, audit_entries, backtest_results, health_data)

    # ⑥ 操作方案生成
    price = current_price or (
        float(df["close"].iloc[-1]) if "close" in df.columns and len(df) > 0 else 0.0
    )
    bias = "bullish" if final_score > 0.05 else ("bearish" if final_score < -0.05 else "neutral")
    plan = generate_operation_plan(ranked, price, bias, df, account_equity=account_equity)

    return ranked, plan
