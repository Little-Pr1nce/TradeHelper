"""
信号检查 + 策略排序 + 操作方案代码生成 — Phase 4 + 5

CLAUDE.md 新架构 ④⑤⑥ 层的核心实现：

  ④ 信号检查层: 每个 PASS/CONDITIONAL 策略 → generate_decision() → 统一转 Order
  ⑤ 策略排序层: 多维评分（审计判定 + 验证夏普 + 信号置信度 + 行情适配）
  ⑥ 操作方案层: Top 2-3 策略信号 → 保守/激进双方案 → 代码生成的 Markdown

核心原则:
  - 操作方案由代码生成，LLM 只负责解读
  - 所有价位来自策略 StrategyDecision/Order 对象（策略自己算出来的），不编造
  - 保守方案 = 最稳健策略，激进方案 = 最高收益策略
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
import numpy as np

from core.data_quality import data_quality_markdown
from backtest.engine import BacktestResult
from strategies.base import BaseExecutionStrategy, StrategyContext, Position, Order, decision_to_orders
from utils.market_rules import estimate_planned_loss_with_cost, get_market_rules

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
    execution_level: str = ""      # A/B/C/D: 可执行/小仓验证/仅观察/驳回
    trigger_price: float = 0.0     # 触发价（观察状态下尤其重要）
    invalidation: str = ""         # 失效条件

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
            "execution_level": self.execution_level,
            "trigger_price": round(self.trigger_price, 4),
            "invalidation": self.invalidation,
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
    current_bar: dict | None = None,
    data_quality: dict | None = None,
) -> list[SignalResult]:
    """
    对每个策略变体检查当前是否满足入场条件。

    调用 generate_decision(df, context) 获取策略语义决策，再统一转换为 Order：
      - 如果转换出 buy/sell Order → 策略正在发信号
      - 如果没有 Order → 当前不满足执行条件，但保留触发价/缺失条件

    account_equity 为用户真实账户权益；未传入时使用 initial_capital 作为模型参考账户。
    current_position 为用户当前持仓；未传入时按空仓检查入场信号。
    """
    if df.empty or len(df) < 20:
        return []

    df = _apply_current_price_snapshot(df, current_price, market, current_bar)
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
            decision = v.strategy.generate_decision(df, context)
            orders = decision_to_orders(decision, context)
            buy_orders = [o for o in orders if o.action == "buy" and o.shares > 0]
            sell_orders = [o for o in orders if o.action == "sell" and o.shares > 0]
            signal = "buy" if buy_orders else ("sell" if sell_orders else "no_signal")

            sr = SignalResult(
                variant_label=v.variant_label,
                strategy_name=v.strategy.name,
                base_key=v.base_key,
                signal=signal,
            )
            if decision:
                sr.execution_level = getattr(decision, "execution_level", "")
                sr.trigger_price = float(getattr(decision, "trigger_price", 0) or 0)
                sr.invalidation = getattr(decision, "invalidation", "") or ""

            if buy_orders:
                o = buy_orders[0]
                sr.entry_price = signal_price
                sr.stop_loss = o.stop_loss if o.stop_loss > 0 else signal_price * 0.92
                sr.take_profit = o.take_profit
                sr.position_pct = _derive_order_position_pct(o, signal_price, sizing_equity)
                sr.reason = o.reason or (getattr(decision, "reason", "") if decision else "") or "策略入场条件满足"
            elif sell_orders:
                o = sell_orders[0]
                sr.entry_price = signal_price
                sr.position_pct = _derive_order_position_pct(o, signal_price, sizing_equity)
                sr.reason = o.reason or (getattr(decision, "reason", "") if decision else "") or "策略退出条件满足"
            else:
                # 诊断为什么不满足
                if decision and (getattr(decision, "missing_conditions", None) or getattr(decision, "reason", "")):
                    missing = getattr(decision, "missing_conditions", None) or []
                    sr.no_signal_reason = "；".join(missing[:3]) if missing else getattr(decision, "reason", "")
                    sr.entry_price = float(getattr(decision, "trigger_price", 0) or 0)
                    sr.stop_loss = float(getattr(decision, "stop_loss", 0) or 0)
                    sr.reason = getattr(decision, "reason", "") or ""
                else:
                    sr.no_signal_reason = _diagnose_no_signal(df, v)

            _apply_data_quality_to_signal(sr, data_quality)
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


def _apply_current_price_snapshot(
    df: pd.DataFrame,
    current_price: float | None,
    market: str = "US",
    current_bar: dict | None = None,
) -> pd.DataFrame:
    """构建内存盘中 K 线并重算技术指标，不污染历史日 K。"""
    if current_price is None or current_price <= 0 or df is None or df.empty:
        return df
    try:
        price = float(current_price)
    except Exception:
        return df

    if float(df.attrs.get("realtime_snapshot_price", 0.0) or 0.0) == price:
        return df
    try:
        last_close = float(df["close"].iloc[-1])
        if not current_bar and last_close > 0 and abs(last_close - price) / last_close < 1e-10:
            return df
    except Exception:
        pass

    # 保留最后一个正式收盘日，追加一根仅用于本次计算的临时 K 线。
    # 只有实时价时无法可靠恢复当日 OHLC，因此使用同价 OHLC，避免虚构振幅。
    snap = df.copy().reset_index(drop=True)
    previous = snap.iloc[-1].copy()
    synthetic = previous.copy()
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai" if (market or "").upper() == "A" else "America/New_York")
        snapshot_date = datetime.now(tz).isoformat()
    except Exception:
        snapshot_date = datetime.now().isoformat()
    synthetic["date"] = snapshot_date
    quote = current_bar or {}
    bar_open = float(quote.get("open", price) or price)
    bar_high = float(quote.get("high", price) or price)
    bar_low = float(quote.get("low", price) or price)
    synthetic["open"] = bar_open
    synthetic["high"] = max(bar_high, price, bar_open)
    synthetic["low"] = min(bar_low, price, bar_open)
    synthetic["close"] = price
    if "volume" in synthetic.index:
        synthetic["volume"] = float(quote.get("volume", 0.0) or 0.0)
    snap = pd.concat([snap, synthetic.to_frame().T], ignore_index=True)
    for column in ("open", "high", "low", "close", "volume"):
        if column in snap.columns:
            snap[column] = pd.to_numeric(snap[column], errors="coerce")

    # 技术指标和技术 Alpha 必须随实时价一起更新。新闻、基本面、盘口等
    # 非价格残差沿用本次管道已经计算出的值，避免二次请求外部数据。
    try:
        from indicators.technical import calc_all_indicators
        from alpha.scoring import (
            DEFAULT_W_TECH,
            _REGIME_WEIGHT_MAP,
            compute_technical_normalized,
            detect_market_regime,
        )

        previous_final = float(previous.get("Final_Score", 0.0) or 0.0)
        previous_tech = float(previous.get("Tech_Normalized_Score", 0.0) or 0.0)
        snap = calc_all_indicators(snap)
        snap = compute_technical_normalized(snap, validate=False)

        if "Final_Score" in snap.columns:
            has_fundamental = all(
                col in snap.columns and pd.notna(previous.get(col))
                for col in ("Style_Score", "Fundamental_Score")
            )
            if has_fundamental:
                regime, _ = detect_market_regime(snap)
                tech_weight = _REGIME_WEIGHT_MAP.get(
                    regime, _REGIME_WEIGHT_MAP["ranging"]
                )["tech"]
            else:
                tech_weight = DEFAULT_W_TECH
            residual = previous_final - tech_weight * previous_tech
            latest_tech = snap["Tech_Normalized_Score"].iloc[-1]
            realtime_tech = float(latest_tech) if pd.notna(latest_tech) else 0.0
            snap.at[snap.index[-1], "Final_Score"] = float(
                np.clip(residual + tech_weight * realtime_tech, -1.0, 1.0)
            )
    except Exception as exc:
        logger.warning(f"盘中临时K线指标重算失败，退化为价格快照: {exc}")

    snap.attrs["realtime_snapshot_price"] = price
    snap.attrs["realtime_snapshot_only"] = True
    return snap


def _apply_data_quality_to_signal(sr: SignalResult, data_quality: dict | None):
    """数据质量闸门直接影响执行等级和仓位。"""
    if not data_quality:
        return
    status = data_quality.get("status", "ok")
    action = data_quality.get("action", "normal")
    multiplier = float(data_quality.get("max_position_multiplier", 1.0) or 0.0)
    issues = data_quality.get("issues") or []
    warnings = data_quality.get("warnings") or []
    reason = "；".join(str(x) for x in (issues or warnings)[:2])

    if status == "blocked" or action == "block":
        if sr.signal == "buy":
            sr.signal = "no_signal"
            sr.no_signal_reason = f"数据质量阻断，禁止新开仓：{reason}"
        elif sr.signal == "sell":
            sr.reason = f"[数据质量阻断，需人工复核价格后执行] {sr.reason}"
        else:
            sr.no_signal_reason = f"数据质量阻断：{reason}"
        sr.execution_level = "D"
        sr.position_pct = 0.0 if sr.signal != "sell" else sr.position_pct
        sr.invalidation = sr.invalidation or "数据质量恢复前不执行"
        return

    if status == "degraded":
        sr.position_pct *= multiplier
        if sr.execution_level == "A":
            sr.execution_level = "B"
        elif sr.execution_level == "B":
            sr.execution_level = "C"
        prefix = f"[数据质量降级，仓位按{multiplier:.0%}上限折减]"
        if sr.signal in ("buy", "sell"):
            sr.reason = f"{prefix} {sr.reason}"
        else:
            sr.no_signal_reason = f"{prefix} {sr.no_signal_reason or reason}"
        return

    if status == "watch":
        sr.position_pct *= multiplier
        if sr.execution_level == "A":
            sr.execution_level = "B"
        prefix = f"[数据质量观察，仓位按{multiplier:.0%}上限折减]"
        if sr.signal in ("buy", "sell"):
            sr.reason = f"{prefix} {sr.reason}"


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
        elif s.execution_level == "B":
            score += 8.0
        elif s.execution_level == "C":
            score += 3.0

        # 风控官等级加权
        if s.execution_level == "A":
            score += 15.0
        elif s.execution_level == "B":
            score += 10.0
        elif s.execution_level == "D":
            score -= 20.0

        # 4. 信号置信度 (10%) — 根据入场理由中的信息量
        if s.reason and len(s.reason) > 20:
            score += 10.0
        elif s.reason:
            score += 5.0

        # 5. 健康度惩罚（持续优化闭环）— 基于 prediction_log 实际表现
        if health_data:
            for h in health_data:
                if h["strategy_name"] in (s.strategy_name, s.base_key, s.variant_label):
                    risk_note = h.get("risk_note") or ""
                    sample_status = h.get("sample_status") or ""
                    lower = h.get("confidence_lower_95")
                    avg_return = h.get("avg_return")
                    evidence_parts = []
                    if h.get("total") is not None:
                        evidence_parts.append(f"样本{int(h.get('total') or 0)}次")
                    if h.get("accuracy") is not None:
                        evidence_parts.append(f"净盈利率{float(h.get('accuracy') or 0):.0%}")
                    if lower is not None:
                        evidence_parts.append(f"95%下界{float(lower or 0):.0%}")
                    if avg_return is not None:
                        evidence_parts.append(f"均值收益{float(avg_return or 0):+.2%}")
                    if risk_note:
                        evidence_parts.append(str(risk_note))
                    evidence = "，".join(evidence_parts)

                    if h.get("action") == "demote":
                        score -= 15.0
                        if s.signal == "buy":
                            s.no_signal_reason = (
                                "策略历史健康度为 demote：历史预测表现不支持执行，"
                                f"本次仅观察，不生成买入/加仓计划"
                                + (f"（{evidence}）" if evidence else "")
                            )
                            s.reason = f"[健康度降级，仅观察] {s.reason}"
                            s.signal = "no_signal"
                            s.execution_level = "C"
                            s.position_pct = 0.0
                        else:
                            s.reason = f"[健康度降级] {s.reason}"
                    elif h.get("action") == "watch":
                        score -= 5.0
                        if s.signal == "buy":
                            multiplier = 0.33 if sample_status == "insufficient" else 0.5
                            s.reason = (
                                f"[健康度观察，仓位按{multiplier:.0%}折减"
                                + (f"：{evidence}" if evidence else "")
                                + f"] {s.reason}"
                            )
                            s.position_pct *= multiplier
                            if s.execution_level == "A":
                                s.execution_level = "B"
                            elif not s.execution_level:
                                s.execution_level = "B"
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
    data_quality: dict | None = None,
    market: str = "US",
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
        plan.markdown = _prepend_data_quality(plan.markdown, data_quality)
        return plan

    buy_signals = [s for s in ranked_signals if s.signal == "buy"]
    if not buy_signals:
        plan = OperationPlan(market_bias=market_bias, account_equity=sizing_equity,
                             equity_is_reference=equity_is_reference)
        plan.markdown = _build_no_signal_markdown(ranked_signals, market_bias, df)
        plan.markdown = _prepend_data_quality(plan.markdown, data_quality)
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
            "max_loss_amount": _max_loss_amount(sizing_equity, min(cons_signal.position_pct, position_cap), entry, stop, market),
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
            "max_loss_amount": _max_loss_amount(sizing_equity, min(agg_signal.position_pct * 1.25, position_cap), entry, stop, market),
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
            "max_loss_amount": _max_loss_amount(sizing_equity, min(agg_signal.position_pct * 1.25, position_cap), entry, stop, market),
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
    plan.markdown = _prepend_data_quality(
        _build_plan_markdown(plan, ranked_signals, current_price, market),
        data_quality,
    )
    return plan


def _prepend_data_quality(markdown: str, data_quality: dict | None) -> str:
    quality_md = data_quality_markdown(data_quality)
    if not quality_md:
        return markdown
    return quality_md + "\n" + markdown


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
        for col, label in [
            ("ma_5", "MA5"),
            ("ma_10", "MA10"),
            ("ma_20", "MA20"),
            ("ma_60", "MA60"),
            ("ma_120", "MA120"),
        ]:
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
            level = f"，等级{s.execution_level}" if s.execution_level else ""
            trigger = f"，触发价≈{s.entry_price or s.trigger_price:.2f}" if (s.entry_price or s.trigger_price) else ""
            lines.append(
                f"**{i+1}. {s.strategy_name[:25]}** {v_emoji} "
                f"(夏普{s.test_sharpe:.2f}, 评分{s.rank_score:.0f}{level}{trigger})"
            )
            if s.no_signal_reason:
                lines.append(f"   需满足: {s.no_signal_reason[:130]}")
            if s.invalidation:
                lines.append(f"   失效: {s.invalidation[:130]}")
            lines.append("")

    # ── 保守建议 ──
    lines.append("### 🛡️ 保守建议\n")
    closest = top3[0] if top3 else None
    health_blocked = bool(
        closest
        and closest.position_pct <= 0
        and "历史健康度" in (closest.no_signal_reason or "")
    )
    if top3:
        lines.append(f"等待 **{top3[0].strategy_name[:20]}** 条件满足后再入场。")
    if health_blocked:
        lines.append("该候选策略因历史预测表现不支持执行，本次只记录观察，不允许买入/加仓。")
    else:
        lines.append("当前所有策略均未触发。保持观望，等策略信号确认后操作。若 5 个交易日内条件未满足，方案自动失效。")
    lines.append("")

    # ── 激进建议 ──
    lines.append("### 🚀 激进建议\n")
    if closest:
        lines.append(f"**{closest.strategy_name[:20]}** 最接近触发。")
        if closest.no_signal_reason:
            lines.append(f"还需: {closest.no_signal_reason[:120]}")
    if health_blocked:
        lines.append("历史健康度已降级，激进方案也不允许抢先试探；等待新样本改善或出现其他正期望策略。")
    else:
        lines.append("如需抢先入场，可等上述条件部分满足后轻仓试探。不建议在条件未满足时盲目入场。")
    lines.append("")
    lines.append("### 📡 全策略信号状态\n")
    lines.append("| 排名 | 策略 | 信号 | 等级 | 审计 | 夏普 | 评分 | 触发/缺失条件 |")
    lines.append("|:---:|------|:---:|:---:|:---:|:---:|:---:|------|")
    for i, s in enumerate(ranked_signals[:10]):
        rank = i + 1
        sig_emoji = "🔴" if s.signal == "no_signal" else "🟢"
        audit_emoji = {"PASS": "✅", "CONDITIONAL": "⚠️", "": "—"}.get(s.audit_verdict, "—")
        reason = s.no_signal_reason[:55] if s.no_signal_reason else "—"
        lines.append(
            f"| {rank} | {s.strategy_name[:20]} "
            f"| {sig_emoji} "
            f"| {s.execution_level or '—'} "
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
    market: str = "US",
) -> str:
    """构建操作方案 Markdown（代码生成，不是 LLM）。"""
    rules = get_market_rules(market)
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
    lines.append(
        f"**交易摩擦估算**: {market} 双边滑点/佣金/税费约 **{rules.round_trip_cost_pct:.2%}**，"
        "最大亏损已包含该估算，不含跳空。\n"
    )

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
        lines.append(f"| 最大亏损 | **${c['max_loss_amount']:,.0f}** | 仓位金额 × 单价风险 + 估算滑点/佣金/税费，不含跳空 |")
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
        lines.append(f"| 最大亏损 | **${a['max_loss_amount']:,.0f}** | 仓位金额 × 单价风险 + 估算滑点/佣金/税费，不含跳空 |")
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
        lines.append("| 排名 | 策略 | 信号 | 等级 | 审计 | 夏普 | 评分 | 触发/缺失条件 |")
        lines.append("|:---:|------|:---:|:---:|:---:|:---:|:---:|------|")
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
                f"| {s.execution_level or '—'} "
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


def _max_loss_amount(
    account_equity: float,
    position_pct: float,
    entry: float,
    stop_loss: float,
    market: str = "US",
) -> float:
    """Max planned loss including estimated trading friction, excluding gaps."""
    return estimate_planned_loss_with_cost(account_equity, position_pct, entry, stop_loss, market)


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
    current_bar: dict | None = None,
    final_score: float = 0.0,
    account_equity: float | None = None,
    current_position: Position | None = None,
    health_data: list[dict] | None = None,  # 策略健康度数据
    data_quality: dict | None = None,
) -> tuple[list[SignalResult], OperationPlan | None]:
    """
    一步完成：信号检查 → 排序 → 操作方案生成。

    Returns:
        (ranked_signals, operation_plan)
    """
    if not variants:
        return [], None

    analysis_df = _apply_current_price_snapshot(df, current_price, market, current_bar)

    # ④ 信号检查
    signals = check_signals(
        analysis_df, variants, market,
        account_equity=account_equity,
        current_position=current_position,
        current_price=current_price,
        current_bar=current_bar,
        data_quality=data_quality,
    )

    # ⑤ 策略排序（含健康度数据）
    ranked = rank_signals(signals, audit_entries, backtest_results, health_data)

    # ⑥ 操作方案生成
    price = current_price or (
        float(analysis_df["close"].iloc[-1])
        if "close" in analysis_df.columns and len(analysis_df) > 0 else 0.0
    )
    bias = "bullish" if final_score > 0.05 else ("bearish" if final_score < -0.05 else "neutral")
    plan = generate_operation_plan(
        ranked, price, bias, analysis_df,
        account_equity=account_equity,
        data_quality=data_quality,
        market=market,
    )

    return ranked, plan


def refresh_realtime_signal_plan(
    pipeline_result,
    *,
    market: str,
    current_quote: dict,
    account_equity: float | None = None,
    current_position: Position | None = None,
    stock_code: str = "",
) -> pd.DataFrame:
    """用实时 OHLC 重算当前决策，不重复执行回测和参数搜索。"""
    current_price = float(
        current_quote.get("latest", current_quote.get("price", 0.0)) or 0.0
    )
    if pipeline_result is None or current_price <= 0:
        return getattr(pipeline_result, "df", pd.DataFrame())

    from core.strategy_pool import StrategyVariant
    from strategies import get_execution_strategy

    variants = []
    pool = getattr(pipeline_result, "strategy_pool", None)
    if pool and (pool.pass_variants or pool.conditional_variants):
        variants.extend(pool.pass_variants + pool.conditional_variants)
    else:
        for key in getattr(pipeline_result, "active_strategies", []) or []:
            try:
                variants.append(StrategyVariant(
                    base_key=key,
                    variant_label=key,
                    strategy=get_execution_strategy(key),
                    params={},
                    is_default=True,
                ))
            except Exception:
                continue

    present_keys = {getattr(v, "base_key", "") for v in variants}
    for key in ("P", "Q", "R", "S", "T"):
        if key in present_keys:
            continue
        try:
            variants.append(StrategyVariant(
                base_key=key,
                variant_label=key,
                strategy=get_execution_strategy(key),
                params={},
                is_default=True,
            ))
        except Exception:
            continue

    analysis_df = _apply_current_price_snapshot(
        pipeline_result.df, current_price, market, current_quote
    )
    latest_score = 0.0
    if "Final_Score" in analysis_df.columns:
        valid_scores = analysis_df["Final_Score"].dropna()
        if not valid_scores.empty:
            latest_score = float(valid_scores.iloc[-1])

    health_data = []
    if stock_code:
        try:
            from data.database import Database
            health_data = Database().get_strategy_health_report(stock_code)
        except Exception as exc:
            logger.warning(f"实时策略健康度读取失败（非致命）: {exc}")

    audit = getattr(pipeline_result, "strategy_audit", None)
    audit_entries = getattr(audit, "entries", None)
    if not audit_entries and pool and getattr(pool, "audit_report", None):
        audit_entries = pool.audit_report.entries
    backtest_results = (
        pool.backtest_results
        if pool and getattr(pool, "backtest_results", None)
        else getattr(pipeline_result, "backtest", None)
    )
    ranked, plan = run_signal_check(
        analysis_df,
        variants,
        market,
        audit_entries=audit_entries,
        backtest_results=backtest_results,
        current_price=current_price,
        current_bar=current_quote,
        final_score=latest_score,
        account_equity=account_equity,
        current_position=current_position,
        health_data=health_data,
        data_quality=getattr(pipeline_result, "data_quality", None),
    )
    pipeline_result.signal_check = [item.to_dict() for item in ranked]
    pipeline_result.operation_plan = plan.markdown if plan else None
    return analysis_df
