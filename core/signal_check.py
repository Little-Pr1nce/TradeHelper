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
    strategy_family: str = ""      # 同逻辑家族只允许一个代表进入执行方案
    entry_price: float = 0.0       # 建议入场价
    stop_loss: float = 0.0         # 止损价
    take_profit: float = 0.0       # 止盈价（如有）
    take_profit_mode: str = "none" # fixed / dynamic / conditional / none
    take_profit_rule: str = ""     # 动态公式或条件退出说明
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
            "strategy_family": self.strategy_family,
            "signal": self.signal,
            "no_signal_reason": self.no_signal_reason,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "take_profit_mode": self.take_profit_mode,
            "take_profit_rule": self.take_profit_rule,
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
    # None 表示 Tab1 没有真实账户，可使用参考资金；显式传入 0 表示
    # Tab3 真实账户无可用权益，不得悄然替换为 10 万元。
    sizing_equity = initial_capital if account_equity is None else max(float(account_equity), 0.0)

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
            if account_equity is not None and sizing_equity <= 0 and buy_orders:
                buy_orders = []
            signal = "buy" if buy_orders else ("sell" if sell_orders else "no_signal")

            sr = SignalResult(
                variant_label=v.variant_label,
                strategy_name=v.strategy.name,
                base_key=v.base_key,
                signal=signal,
                strategy_family=(
                    getattr(v.strategy, "strategy_family", "")
                    or v.strategy.__class__.__name__
                ),
            )
            if decision:
                sr.execution_level = getattr(decision, "execution_level", "")
                sr.trigger_price = float(getattr(decision, "trigger_price", 0) or 0)
                sr.invalidation = getattr(decision, "invalidation", "") or ""
                sr.take_profit_mode = getattr(decision, "take_profit_mode", "none") or "none"
                sr.take_profit_rule = getattr(decision, "take_profit_rule", "") or ""

            if buy_orders:
                o = buy_orders[0]
                sr.entry_price = signal_price
                sr.stop_loss = o.stop_loss if o.stop_loss > 0 else signal_price * 0.92
                sr.take_profit = (
                    float(o.take_profit)
                    if np.isfinite(o.take_profit) and o.take_profit > signal_price
                    else 0.0
                )
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
                    native_missing = v.strategy.diagnose_no_signal(df, context)
                    sr.no_signal_reason = "；".join(native_missing[:3])

            if account_equity is not None and sizing_equity <= 0 and not sell_orders:
                sr.signal = "no_signal"
                sr.position_pct = 0.0
                sr.execution_level = "D"
                sr.no_signal_reason = "真实账户权益为0，禁止新开仓；请先录入可用现金或有效持仓"

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
        raw_volume = quote.get("volume")
        try:
            parsed_volume = float(raw_volume) if raw_volume is not None else np.nan
        except (TypeError, ValueError):
            parsed_volume = np.nan
        synthetic["volume"] = parsed_volume if parsed_volume >= 0 else np.nan
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
        # IC/IR 权重只应用到最新一行，使用历史证据筛选当前因子，
        # 不会回写历史分数造成未来函数。
        snap = compute_technical_normalized(snap, validate=True)

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


def _structured_signal_confidence_score(signal: SignalResult) -> float:
    """按可验证的结构化交易字段计分，不使用理由文本长度。"""
    score = 0.0
    if signal.execution_level in ("A", "B", "C", "D"):
        score += 1.0
    if signal.trigger_price > 0:
        score += 1.0
    if signal.invalidation:
        score += 1.0

    if signal.signal == "buy":
        if signal.entry_price > 0:
            score += 2.0
        if 0 < signal.stop_loss < signal.entry_price:
            score += 3.0
        if signal.take_profit > signal.entry_price:
            score += 2.0
        elif signal.take_profit_mode in ("dynamic", "conditional") and signal.take_profit_rule:
            score += 1.0
    elif signal.signal == "sell":
        if signal.entry_price > 0:
            score += 3.0
        if signal.position_pct > 0:
            score += 3.0
        # 卖出是已有持仓的风险处置，不强制要求多头止盈目标。
        score += 1.0
    else:
        if signal.no_signal_reason:
            score += 3.0
        if signal.entry_price > 0:
            score += 2.0
        if signal.stop_loss > 0:
            score += 1.0
    return min(score, 10.0)


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
        health_matched = False

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

        # 4. 计划完整度 (10%)：只看可验证的价格、止损、止盈、失效条件。
        score += _structured_signal_confidence_score(s)

        # 5. 健康度惩罚（持续优化闭环）— 基于 prediction_log 实际表现
        if health_data:
            for h in health_data:
                action_matches = (h.get("signal_action") or s.signal) == s.signal
                if (action_matches
                        and h["strategy_name"] in (s.strategy_name, s.base_key, s.variant_label)):
                    health_matched = True
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

        # A 级买入必须有当前股票+策略的真实历史正期望证据。
        # 无样本不是“已验证”，最高只能小仓验证；风险退出信号不受此限制。
        if s.signal == "buy" and s.execution_level == "A" and not health_matched:
            s.execution_level = "B"
            s.reason = f"[历史样本不足，A级降为B级小仓验证] {s.reason}"
            s.position_pct *= 0.5
            score -= 5.0

        s.rank_score = round(score, 1)

    # 排序
    return sorted(signals, key=lambda x: x.rank_score, reverse=True)


# ══════════════════════════════════════════════════════════════════
# ⑥ 操作方案生成层
# ══════════════════════════════════════════════════════════════════

def _dynamic_plan_position(
    proposed_pct: float,
    profile: str,
    market_bias: str,
    df: pd.DataFrame | None,
    account_equity: float,
    current_position: Position | None,
    entry: float,
    stop_loss: float,
    market: str,
) -> tuple[float, str]:
    """用风险预算、波动率、集中度和流动性共同限制新增仓位。"""
    if proposed_pct <= 0 or account_equity <= 0 or entry <= 0:
        return 0.0, "无有效资金或策略建议仓位"

    conservative = profile == "conservative"
    if conservative:
        base_cap = 0.25 if market_bias == "bearish" else 0.35
        account_risk_budget = 0.01
        adv_participation = 0.01
    else:
        base_cap = 0.35 if market_bias == "bearish" else 0.50
        account_risk_budget = 0.02
        adv_participation = 0.02

    annualized_vol = 0.0
    liquidity_cap = 1.0
    if df is not None and not df.empty and "close" in df.columns:
        close = pd.to_numeric(df["close"], errors="coerce")
        returns = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        if len(returns) >= 10:
            annualized_vol = float(returns.iloc[-20:].std() * np.sqrt(252))
        if "volume" in df.columns:
            volume = pd.to_numeric(df["volume"], errors="coerce")
            dollar_volume = (close * volume).where(volume > 0).dropna()
            if not dollar_volume.empty:
                median_adv = float(dollar_volume.iloc[-20:].median())
                if median_adv > 0:
                    liquidity_cap = median_adv * adv_participation / account_equity

    volatility_scale = 1.0
    if annualized_vol > 0.30:
        volatility_scale = float(np.clip(0.30 / annualized_vol, 0.35, 1.0))
    volatility_cap = base_cap * volatility_scale

    rules = get_market_rules(market)
    stop_risk_pct = max(entry - stop_loss, 0.0) / entry if stop_loss > 0 else 0.0
    unit_risk_pct = stop_risk_pct + rules.round_trip_cost_pct
    risk_cap = account_risk_budget / unit_risk_pct if unit_risk_pct > 0 else base_cap

    existing_pct = 0.0
    if current_position and current_position.shares > 0:
        existing_pct = current_position.shares * entry / account_equity

    total_cap = max(min(volatility_cap, risk_cap, liquidity_cap, 1.0), 0.0)
    remaining_cap = max(total_cap - existing_pct, 0.0)
    final_pct = float(np.clip(min(proposed_pct, remaining_cap), 0.0, 1.0))
    details = [
        f"单笔净值风险≤{account_risk_budget:.0%}",
        f"总仓位上限{total_cap:.1%}",
    ]
    if annualized_vol > 0:
        details.append(f"近20日年化波动{annualized_vol:.0%}")
    if existing_pct > 0:
        details.append(f"已有仓位{existing_pct:.1%}")
    if liquidity_cap < 1.0:
        details.append(f"成交额约束{liquidity_cap:.1%}")
    return final_pct, "；".join(details)

def generate_operation_plan(
    ranked_signals: list[SignalResult],
    current_price: float,
    market_bias: str = "neutral",
    df: pd.DataFrame | None = None,
    account_equity: float | None = None,
    data_quality: dict | None = None,
    market: str = "US",
    current_position: Position | None = None,
) -> OperationPlan:
    """
    从排序后的信号中选 Top 2-3 策略，生成保守 + 激进双方案。

    保守方案:
      - 选 audit=PASS 中排名最高的策略
      - 使用策略原始止损
      - 单笔账户风险预算不超过 1%

    激进方案:
      - 选 rank_score 最高的策略（即使 audit=CONDITIONAL）
      - 单笔账户风险预算不超过 2%

    两套方案还会同时受到近期波动率、已有持仓集中度和日均成交额约束。

    Args:
        ranked_signals: rank_signals() 排序后的信号列表
        current_price: 当前价格
        market_bias: 市场方向偏好多/空/中性

    Returns:
        OperationPlan 含两套方案
    """
    equity_is_reference = account_equity is None
    sizing_equity = 100000.0 if equity_is_reference else max(float(account_equity), 0.0)

    if not ranked_signals:
        return OperationPlan(market_bias=market_bias, account_equity=sizing_equity,
                             equity_is_reference=equity_is_reference)

    raw_sell_signals = [s for s in ranked_signals if s.signal == "sell"]
    sell_signals = select_actionable_sell_signals(raw_sell_signals)
    if sell_signals:
        plan = OperationPlan(market_bias=market_bias, account_equity=sizing_equity,
                             equity_is_reference=equity_is_reference)
        plan.markdown = _build_sell_signal_markdown(sell_signals, current_price, sizing_equity,
                                                    equity_is_reference)
        plan.markdown = _prepend_data_quality(plan.markdown, data_quality)
        return plan

    buy_signals = select_signal_family_representatives(
        [s for s in ranked_signals if s.signal == "buy"]
    )
    if not buy_signals:
        plan = OperationPlan(market_bias=market_bias, account_equity=sizing_equity,
                             equity_is_reference=equity_is_reference)
        plan.markdown = _build_no_signal_markdown(ranked_signals, market_bias, df)
        if raw_sell_signals and current_position and current_position.shares > 0:
            names = "、".join(s.strategy_name for s in raw_sell_signals[:3])
            plan.markdown = (
                "### 🟡 持仓策略分歧\n\n"
                f"{names} 出现退出信号，但尚未得到 Q/R/S 持仓风控策略或至少两个独立策略的共同确认。"
                "本次不把单一策略退出升级为整只持仓卖出；保留为止损/复核提醒。\n\n"
                + plan.markdown
            )
        if not equity_is_reference and sizing_equity <= 0:
            plan.markdown = (
                "> **账户资金硬约束：真实账户权益为0，本次禁止新开仓或加仓。**\n\n"
                + plan.markdown
            )
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
        position_pct, cap_reason = _dynamic_plan_position(
            cons_signal.position_pct, "conservative", market_bias, df,
            sizing_equity, current_position, entry, stop, market,
        )
        conservative = {
            "entry": entry,
            "stop_loss": stop,
            "take_profit": cons_signal.take_profit,
            "take_profit_mode": cons_signal.take_profit_mode,
            "take_profit_rule": cons_signal.take_profit_rule,
            "position_pct": position_pct,
            "position_cap_reason": cap_reason,
            "signal_strength": _signal_strength(cons_signal),
            "max_loss_amount": _max_loss_amount(sizing_equity, position_pct, entry, stop, market),
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
        position_pct, cap_reason = _dynamic_plan_position(
            agg_signal.position_pct * 1.25, "aggressive", market_bias, df,
            sizing_equity, current_position, entry, stop, market,
        )
        aggressive = {
            "entry": entry,
            "stop_loss": stop,
            "take_profit": agg_signal.take_profit,
            "take_profit_mode": agg_signal.take_profit_mode,
            "take_profit_rule": agg_signal.take_profit_rule,
            "position_pct": position_pct,
            "position_cap_reason": cap_reason,
            "signal_strength": _signal_strength(agg_signal),
            "max_loss_amount": _max_loss_amount(sizing_equity, position_pct, entry, stop, market),
            "invalidation": _invalidation_conditions(entry, stop, market_bias),
            "reason": agg_signal.reason or "策略入场条件满足",
            "source_strategies": [agg_signal.strategy_name],
        }
    elif agg_signal and not aggressive:
        # 只有一个信号 → 激进方案 = 同一策略但适度放大仓位，不改写策略止损
        entry = agg_signal.entry_price or current_price
        stop = agg_signal.stop_loss or entry * 0.92
        position_pct, cap_reason = _dynamic_plan_position(
            agg_signal.position_pct * 1.25, "aggressive", market_bias, df,
            sizing_equity, current_position, entry, stop, market,
        )
        aggressive = {
            "entry": entry,
            "stop_loss": stop,
            "take_profit": agg_signal.take_profit,
            "take_profit_mode": agg_signal.take_profit_mode,
            "take_profit_rule": agg_signal.take_profit_rule,
            "position_pct": position_pct,
            "position_cap_reason": cap_reason,
            "signal_strength": _signal_strength(agg_signal),
            "max_loss_amount": _max_loss_amount(sizing_equity, position_pct, entry, stop, market),
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


def select_actionable_sell_signals(signals: list) -> list:
    """只把持仓专用风控或多策略共识提升为组合级退出动作。"""
    sells = [s for s in signals if _signal_value(s, "signal") == "sell"]
    if not sells:
        return []
    dedicated = [
        s for s in sells
        if str(_signal_value(s, "base_key") or "").upper() in {"Q", "R", "S"}
        and str(_signal_value(s, "execution_level") or "C").upper() != "D"
    ]
    if dedicated:
        return sells
    independent = {
        str(
            _signal_value(s, "strategy_family")
            or _signal_value(s, "base_key")
            or _signal_value(s, "strategy_name")
            or ""
        )
        for s in sells
        if str(_signal_value(s, "execution_level") or "C").upper() != "D"
    }
    return sells if len(independent) >= 2 else []


def select_signal_family_representatives(signals: list) -> list:
    """排序结果中每个策略家族只保留最高分代表进入执行方案。"""
    selected = []
    seen = set()
    for signal in signals:
        family = str(
            _signal_value(signal, "strategy_family")
            or _signal_value(signal, "base_key")
            or _signal_value(signal, "strategy_name")
            or ""
        )
        if family in seen:
            continue
        seen.add(family)
        selected.append(signal)
    return selected


def _signal_value(signal, key: str):
    if isinstance(signal, dict):
        return signal.get(key)
    return getattr(signal, key, None)


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
    same_execution_price = bool(
        plan.conservative and plan.aggressive
        and abs(plan.conservative["entry"] - plan.aggressive["entry"]) < 1e-9
    )
    same_source_strategy = bool(
        same_execution_price
        and plan.conservative.get("source_strategies") == plan.aggressive.get("source_strategies")
    )
    if same_execution_price:
        source_note = "同一已触发策略" if same_source_strategy else "两项已触发策略给出了相同价格"
        lines.append(
            f"> 两套方案因{source_note}，因此触发价相同；"
            "保守/激进的差异仅在仓位、账户风险预算和最大亏损，不虚构第二个价格。\n"
        )

    if plan.conservative:
        c = plan.conservative
        lines.append("### 🛡️ 保守方案\n")
        lines.append("| 项目 | 数值 | 理由 |")
        lines.append("|------|------|------|")
        entry_reason = (
            "同一策略触发价；保守性由较低仓位和1%账户风险预算体现"
            if same_execution_price else "较稳健策略给出的可执行触发价"
        )
        lines.append(f"| 入场价 | **${c['entry']:.2f}** | {entry_reason} |")
        c_loss = _loss_pct(c["entry"], c["stop_loss"])
        c_account_risk = (
            c["max_loss_amount"] / plan.account_equity * 100
            if plan.account_equity > 0 else 0.0
        )
        lines.append(f"| 止损价 | **${c['stop_loss']:.2f}** | 策略止损，单价风险 {c_loss:.1f}% |")
        lines.append(
            f"| 止盈计划 | **{_take_profit_text(c['take_profit'], c['take_profit_mode'], c['take_profit_rule'])}** "
            "| 固定目标可计算风险收益比；动态/条件退出只展示真实规则 |"
        )
        lines.append(f"| 信号强度 | **{c['signal_strength']}** | 基于审计结论、验证夏普和综合排名 |")
        lines.append(f"| 仓位 | **{c['position_pct']*100:.1f}%** 账户权益 | {c['position_cap_reason']} |")
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
        entry_reason = (
            "同一策略触发价；激进性由较高仓位和2%账户风险预算体现"
            if same_execution_price else "当前排名最高策略给出的可执行触发价"
        )
        lines.append(f"| 入场价 | **${a['entry']:.2f}** | {entry_reason} |")
        a_loss = _loss_pct(a["entry"], a["stop_loss"])
        a_account_risk = (
            a["max_loss_amount"] / plan.account_equity * 100
            if plan.account_equity > 0 else 0.0
        )
        lines.append(f"| 止损价 | **${a['stop_loss']:.2f}** | 策略止损，单价风险 {a_loss:.1f}% |")
        lines.append(
            f"| 止盈计划 | **{_take_profit_text(a['take_profit'], a['take_profit_mode'], a['take_profit_rule'])}** "
            "| 固定目标可计算风险收益比；动态/条件退出只展示真实规则 |"
        )
        lines.append(f"| 信号强度 | **{a['signal_strength']}** | 基于审计结论、验证夏普和综合排名 |")
        lines.append(f"| 仓位 | **{a['position_pct']*100:.1f}%** 账户权益 | {a['position_cap_reason']} |")
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
        lines.append(
            f"| 止盈计划 | {_take_profit_text(c['take_profit'], c['take_profit_mode'], c['take_profit_rule'])} | "
            f"{_take_profit_text(a['take_profit'], a['take_profit_mode'], a['take_profit_rule'])} |"
        )
        lines.append(f"| 仓位 | {c['position_pct']*100:.1f}% | {a['position_pct']*100:.1f}% |")
        c_total_risk = c["max_loss_amount"] / plan.account_equity * 100 if plan.account_equity > 0 else 0.0
        a_total_risk = a["max_loss_amount"] / plan.account_equity * 100 if plan.account_equity > 0 else 0.0
        lines.append(f"| 账户风险 | {c_total_risk:.2f}% | {a_total_risk:.2f}% |")
        lines.append(f"| 最大亏损 | ${c['max_loss_amount']:,.0f} | ${a['max_loss_amount']:,.0f} |")
        lines.append(
            f"| 风险收益比 | {_risk_reward_text(c['entry'], c['stop_loss'], c['take_profit'], c['take_profit_mode'])} | "
            f"{_risk_reward_text(a['entry'], a['stop_loss'], a['take_profit'], a['take_profit_mode'])} |"
        )
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


def _take_profit_text(take_profit: float, mode: str = "none", rule: str = "") -> str:
    if take_profit and np.isfinite(take_profit) and take_profit > 0:
        return f"固定目标 ${take_profit:.2f}" + (f"；{rule}" if rule else "")
    clean_rule = str(rule or "").replace("|", "/")
    if mode == "dynamic":
        return f"动态止盈：{clean_rule or '随价格和波动率更新'}"
    if mode == "conditional":
        return f"条件止盈：{clean_rule or '满足策略退出条件时执行'}"
    return clean_rule or "无主动止盈，仅止损/时间退出"


def _risk_reward_text(
    entry: float, stop_loss: float, take_profit: float, mode: str = "none",
) -> str:
    risk = entry - stop_loss
    reward = take_profit - entry
    if entry <= 0 or risk <= 0 or not np.isfinite(take_profit) or reward <= 0:
        if mode == "dynamic":
            return "不可固定量化（动态止盈）"
        if mode == "conditional":
            return "不可固定量化（条件退出）"
        return "不可量化（无主动止盈）"
    return f"1:{reward / risk:.2f}"


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
    variants = [
        variant for variant in (variants or [])
        if getattr(getattr(variant, "strategy", None), "live_signal_enabled", True)
    ]
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
        current_position=current_position,
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
    from strategies import get_execution_strategy, get_overlay_strategy_keys

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
    for key in get_overlay_strategy_keys(
        has_position=bool(current_position and current_position.shares > 0)
    ):
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
    pipeline_result.decision_df = analysis_df
    return analysis_df
