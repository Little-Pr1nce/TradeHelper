"""
策略池审计引擎 — 时间切分验证

对已完成的全量回测结果，按时间将 trades 切成训练期（前 70%）和验证期（后 30%），
比较两个周期的绩效差异，输出每策略的 PASS / CONDITIONAL / FAIL 判定。

核心原则：
  - 不重新跑回测 — 直接用 BacktestResult 中的 trades 和 equity_curve
  - 训练期 = 策略拟合参数的时间段（样本内）
  - 验证期 = 策略从未见过的数据（样本外）
  - 验证期绩效显著差 → 过拟合 → 淘汰/降级
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
import numpy as np

from strategies import get_execution_strategy
from backtest.engine import BacktestResult
from backtest.analytics import _calc_sharpe, _calc_max_drawdown, _calc_trade_stats

logger = logging.getLogger(__name__)

# 年度交易天数
TRADING_DAYS_PER_YEAR = 252

# ── 判定阈值 ──
PASS_MIN_TRAIN_TRADES = 5         # 训练期最少交易次数
PASS_MIN_TEST_TRADES = 3          # 验证期最少交易次数
PASS_MIN_SHARPE = 1.0             # 验证期最低夏普
PASS_MAX_DRAWDOWN = 0.30          # 验证期最大回撤
PASS_MIN_WIN_RATE = 0.45          # 验证期最低胜率

CONDITIONAL_MIN_TRAIN_TRADES = 3
CONDITIONAL_MIN_TEST_TRADES = 1
CONDITIONAL_MIN_SHARPE = 0.5
CONDITIONAL_MAX_DRAWDOWN = 0.40

OVERFIT_SHARPE_DEGRADATION = 0.30  # 验证期夏普 < 训练期的 30% → 过拟合


@dataclass
class StrategyAuditEntry:
    """单个策略的审计条目。"""
    strategy_key: str = ""           # "A", "B", ...
    strategy_name: str = ""          # 可读名称（"百分位趋势跟踪"）
    suitable_regimes: list = field(default_factory=list)
    # 训练期（前 70% 时间段）
    train_trades: int = 0
    train_sharpe: float = 0.0
    train_return: float = 0.0
    train_drawdown: float = 0.0
    train_win_rate: float = 0.0
    # 验证期（后 30% 时间段，样本外）
    test_trades: int = 0
    test_sharpe: float = 0.0
    test_return: float = 0.0
    test_drawdown: float = 0.0
    test_win_rate: float = 0.0
    # 衰减指标
    sharpe_degradation: float = 0.0  # test_sharpe / train_sharpe
    return_degradation: float = 0.0  # test_return / train_return
    # 判定
    verdict: str = ""                # "PASS" | "CONDITIONAL" | "FAIL"
    overfit: bool = False            # 过拟合标记
    verdict_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "strategy_key": self.strategy_key,
            "strategy_name": self.strategy_name,
            "suitable_regimes": self.suitable_regimes,
            "train_trades": self.train_trades,
            "train_sharpe": round(self.train_sharpe, 4),
            "train_return": round(self.train_return, 4),
            "train_drawdown": round(self.train_drawdown, 4),
            "train_win_rate": round(self.train_win_rate, 4),
            "test_trades": self.test_trades,
            "test_sharpe": round(self.test_sharpe, 4),
            "test_return": round(self.test_return, 4),
            "test_drawdown": round(self.test_drawdown, 4),
            "test_win_rate": round(self.test_win_rate, 4),
            "sharpe_degradation": round(self.sharpe_degradation, 4),
            "return_degradation": round(self.return_degradation, 4),
            "verdict": self.verdict,
            "overfit": self.overfit,
            "verdict_reason": self.verdict_reason,
        }


@dataclass
class StrategyAuditReport:
    """一次完整的策略审计报告。"""
    stock_code: str = ""
    split_date: str = ""
    train_period: str = ""
    test_period: str = ""
    entries: list = field(default_factory=list)   # list[StrategyAuditEntry]
    summary: dict = field(default_factory=dict)    # {"pass": N, "conditional": N, "fail": N, "overfit": N}
    recommendations: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "stock_code": self.stock_code,
            "split_date": self.split_date,
            "train_period": self.train_period,
            "test_period": self.test_period,
            "entries": [e.to_dict() for e in self.entries],
            "summary": self.summary,
            "recommendations": self.recommendations,
        }


# ══════════════════════════════════════════════════════════════════
# 核心审计函数
# ══════════════════════════════════════════════════════════════════

def run_strategy_audit(
    df: pd.DataFrame,
    strategy_keys: list[str],
    backtest_results: dict[str, BacktestResult],
    initial_capital: float = 100000.0,
    split_ratio: float = 0.70,
) -> StrategyAuditReport:
    """
    对多策略回测结果做时间切分审计。

    Args:
        df: 含 date 列的完整 DataFrame（用于确定时间分割点）
        strategy_keys: 被审计的策略键列表（如 ["A", "B", "C"]），只审计活跃策略
        backtest_results: BacktestEngine.run_multi() 的输出，key=strategy.name
        initial_capital: 初始资金（用于从 trades 重建 equity curve）
        split_ratio: 训练期占比，默认 70%

    Returns:
        StrategyAuditReport 含每策略审计结论
    """
    if "date" not in df.columns or df.empty:
        logger.warning("DataFrame 无 date 列或为空，无法审计")
        return StrategyAuditReport()

    # ── 1. 确定时间分割点 ──
    split_idx = int(len(df) * split_ratio)
    split_date = str(df["date"].iloc[split_idx])[:10]
    train_start = str(df["date"].iloc[0])[:10]
    test_end = str(df["date"].iloc[-1])[:10]

    logger.info(f"策略审计: 数据范围 {train_start} → {test_end}, "
                f"分割点 {split_date} (前 {split_idx}/{len(df)} = {split_ratio:.0%})")

    # ── 2. 构建 key → strategy.name 映射 ──
    key_to_name: dict[str, str] = {}
    key_to_regimes: dict[str, list[str]] = {}
    for key in strategy_keys:
        try:
            s = get_execution_strategy(key)
            key_to_name[key] = s.name
            key_to_regimes[key] = list(s.suitable_regimes)
        except Exception:
            key_to_name[key] = key
            key_to_regimes[key] = []

    # ── 3. 逐策略审计 ──
    entries: list[StrategyAuditEntry] = []

    for key in strategy_keys:
        display_name = key_to_name.get(key, key)
        bt_result = backtest_results.get(display_name)
        if bt_result is None:
            logger.warning(f"策略 {key} ({display_name}) 无回测结果，跳过审计")
            continue

        entry = _audit_one_strategy(
            key=key,
            display_name=display_name,
            regimes=key_to_regimes.get(key, []),
            bt_result=bt_result,
            split_date=split_date,
            initial_capital=initial_capital,
        )
        entries.append(entry)

    # ── 4. 汇总 ──
    summary = {
        "pass": sum(1 for e in entries if e.verdict == "PASS"),
        "conditional": sum(1 for e in entries if e.verdict == "CONDITIONAL"),
        "fail": sum(1 for e in entries if e.verdict == "FAIL"),
        "overfit": sum(1 for e in entries if e.overfit),
    }

    recs: list[str] = []
    pass_keys = [e.strategy_key for e in entries if e.verdict == "PASS"]
    cond_keys = [e.strategy_key for e in entries if e.verdict == "CONDITIONAL"]
    fail_keys = [e.strategy_key for e in entries if e.verdict == "FAIL"]
    overfit_keys = [e.strategy_key for e in entries if e.overfit]

    if pass_keys:
        recs.append(f"✅ 可信任策略（{len(pass_keys)} 个）：{', '.join(pass_keys)} — 可在操作方案中优先使用")
    if cond_keys:
        recs.append(f"⚠️ 需观察策略（{len(cond_keys)} 个）：{', '.join(cond_keys)} — 参考使用，标注风险")
    if fail_keys:
        recs.append(f"❌ 不建议使用（{len(fail_keys)} 个）：{', '.join(fail_keys)} — 仅在全量回测表格展示")
    if overfit_keys:
        recs.append(f"🔴 疑似过拟合（{len(overfit_keys)} 个）：{', '.join(overfit_keys)} — 样本外衰减严重")

    if not pass_keys and not cond_keys:
        recs.append("⚠️ 当前行情下无可靠策略，建议观望为主。")

    logger.info(
        f"策略审计完成: PASS={summary['pass']}, CONDITIONAL={summary['conditional']}, "
        f"FAIL={summary['fail']}, OVERFIT={summary['overfit']}"
    )

    return StrategyAuditReport(
        stock_code="",
        split_date=split_date,
        train_period=f"{train_start} → {split_date}",
        test_period=f"{split_date} → {test_end}",
        entries=entries,
        summary=summary,
        recommendations=recs,
    )


# ══════════════════════════════════════════════════════════════════
# 单策略审计
# ══════════════════════════════════════════════════════════════════

def _audit_one_strategy(
    key: str,
    display_name: str,
    regimes: list[str],
    bt_result: BacktestResult,
    split_date: str,
    initial_capital: float,
) -> StrategyAuditEntry:
    """审计单个策略。"""

    equity_curve = bt_result.equity_curve or []
    trades = bt_result.trades or []

    # ── 按 split_date 切分 equity_curve ──
    train_equities = [e["equity"] for e in equity_curve if str(e.get("date", ""))[:10] < split_date]
    test_equities_full = [e for e in equity_curve if str(e.get("date", ""))[:10] >= split_date]

    # 验证期：把分割点前一天净值作为基准
    if train_equities:
        test_start_equity = train_equities[-1]
    elif test_equities_full:
        test_start_equity = test_equities_full[0]["equity"]
    else:
        test_start_equity = initial_capital

    test_equities = [test_start_equity] + [e["equity"] for e in test_equities_full]

    # ── 按 entry_date 切分 trades ──
    train_trades_list = [t for t in trades if str(t.get("entry_date", ""))[:10] < split_date]
    test_trades_list = [t for t in trades if str(t.get("entry_date", ""))[:10] >= split_date]

    # ── 计算训练期指标 ──
    train_metrics = _compute_segment_metrics(train_equities, train_trades_list)
    test_metrics = _compute_segment_metrics(test_equities, test_trades_list)

    # ── 衰减 ──
    sharpe_degradation = (
        test_metrics["sharpe"] / max(train_metrics["sharpe"], 0.01)
        if train_metrics["sharpe"] > 0 else 0.0
    )
    return_degradation = (
        test_metrics["return"] / max(train_metrics["return"] + 1, 0.01)
        if train_metrics["return"] > -1 else 0.0
    )

    # ── 判定 ──
    verdict, overfit, reason = _apply_verdict(
        train_trades=train_metrics["trades"],
        test_trades=test_metrics["trades"],
        train_sharpe=train_metrics["sharpe"],
        test_sharpe=test_metrics["sharpe"],
        test_drawdown=test_metrics["drawdown"],
        test_win_rate=test_metrics["win_rate"],
        sharpe_degradation=sharpe_degradation,
        return_degradation=return_degradation,
    )

    return StrategyAuditEntry(
        strategy_key=key,
        strategy_name=display_name,
        suitable_regimes=regimes,
        train_trades=train_metrics["trades"],
        train_sharpe=train_metrics["sharpe"],
        train_return=train_metrics["return"],
        train_drawdown=train_metrics["drawdown"],
        train_win_rate=train_metrics["win_rate"],
        test_trades=test_metrics["trades"],
        test_sharpe=test_metrics["sharpe"],
        test_return=test_metrics["return"],
        test_drawdown=test_metrics["drawdown"],
        test_win_rate=test_metrics["win_rate"],
        sharpe_degradation=sharpe_degradation,
        return_degradation=return_degradation,
        verdict=verdict,
        overfit=overfit,
        verdict_reason=reason,
    )


# ══════════════════════════════════════════════════════════════════
# 内部工具函数
# ══════════════════════════════════════════════════════════════════

def _compute_segment_metrics(
    equities: list[float],
    trades: list[dict],
) -> dict:
    """从一段净值曲线和交易列表计算绩效指标。"""
    if not equities or len(equities) < 2:
        return {
            "trades": len(trades),
            "sharpe": 0.0,
            "return": 0.0,
            "drawdown": 0.0,
            "win_rate": 0.0,
            "profit_loss_ratio": 0.0,
            "avg_holding_days": 0.0,
        }

    total_return = (equities[-1] - equities[0]) / equities[0] if equities[0] > 0 else 0.0
    max_dd = _calc_max_drawdown(equities)
    sharpe = _calc_sharpe(equities)
    win_rate, profit_loss_ratio, avg_holding = _calc_trade_stats(trades)

    return {
        "trades": len(trades),
        "sharpe": sharpe,
        "return": total_return,
        "drawdown": max_dd,
        "win_rate": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
        "avg_holding_days": avg_holding,
    }


def _apply_verdict(
    train_trades: int,
    test_trades: int,
    train_sharpe: float,
    test_sharpe: float,
    test_drawdown: float,
    test_win_rate: float,
    sharpe_degradation: float,
    return_degradation: float,
) -> tuple[str, bool, str]:
    """根据阈值给出策略判定。

    Returns:
        (verdict, overfit, reason)
    """
    reasons: list[str] = []

    # ── 过拟合检测 ──
    overfit = False
    if train_sharpe > 0.5 and sharpe_degradation < OVERFIT_SHARPE_DEGRADATION:
        overfit = True
        reasons.append(f"验证夏普({test_sharpe:.2f})不到训练({train_sharpe:.2f})的{OVERFIT_SHARPE_DEGRADATION:.0%}，疑似过拟合")

    # ── 判定层次 ──
    if (train_trades >= PASS_MIN_TRAIN_TRADES
            and test_trades >= PASS_MIN_TEST_TRADES
            and test_sharpe >= PASS_MIN_SHARPE
            and test_drawdown <= PASS_MAX_DRAWDOWN
            and test_win_rate >= PASS_MIN_WIN_RATE):
        verdict = "PASS"
        if overfit:
            reasons.append("虽有信号衰减但仍达标")

    elif (train_trades >= CONDITIONAL_MIN_TRAIN_TRADES
            and test_trades >= CONDITIONAL_MIN_TEST_TRADES
            and test_sharpe >= CONDITIONAL_MIN_SHARPE
            and test_drawdown <= CONDITIONAL_MAX_DRAWDOWN):
        verdict = "CONDITIONAL"
        reasons.append("部分指标未达 PASS 标准，当前行情下需谨慎参考")

    else:
        verdict = "FAIL"
        if train_trades < CONDITIONAL_MIN_TRAIN_TRADES:
            reasons.append(f"训练期交易不足（{train_trades}次 < {CONDITIONAL_MIN_TRAIN_TRADES}次），统计不显著")
        if test_trades < CONDITIONAL_MIN_TEST_TRADES:
            reasons.append(f"验证期无交易机会（{test_trades}次），当前行情不适用")
        if test_sharpe < CONDITIONAL_MIN_SHARPE:
            reasons.append(f"验证夏普偏低（{test_sharpe:.2f} < {CONDITIONAL_MIN_SHARPE}），风险调整后收益不足")
        if test_drawdown > CONDITIONAL_MAX_DRAWDOWN:
            reasons.append(f"验证回撤过大（{test_drawdown*100:.1f}% > {CONDITIONAL_MAX_DRAWDOWN*100:.0f}%）")
        if test_trades >= 3 and test_win_rate < PASS_MIN_WIN_RATE:
            reasons.append(f"验证胜率偏低（{test_win_rate*100:.1f}% < {PASS_MIN_WIN_RATE*100:.0f}%）")

    return verdict, overfit, "; ".join(reasons) if reasons else "—"


# ══════════════════════════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════════════════════════

def get_verdict_emoji(verdict: str) -> str:
    """获取判定对应的 emoji。"""
    return {"PASS": "✅", "CONDITIONAL": "⚠️", "FAIL": "❌"}.get(verdict, "❓")


def get_verdict_cn(verdict: str) -> str:
    """获取判定中文标签。"""
    return {"PASS": "通过", "CONDITIONAL": "有条件", "FAIL": "淘汰"}.get(verdict, verdict)
