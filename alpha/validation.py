"""
因子有效性检验 — 单股时序 IC + 滚动 IR。

对 Alpha 模型的 7 个技术指标逐个做有效性检验：
  - ts-IC: 因子序列与 N 日远期收益的 Spearman 秩相关
  - 滚动 IR: 60 日滚动 IC 的 mean/std（衡量因子稳定性）

分档 → 动态调权：
  A 级 (|IC|≥0.10 + |IR|≥1.0) → 权重 ×1.0（精品因子）
  B 级 (|IC|≥0.06 + |IR|≥0.5) → 权重 ×1.0（通过）
  C 级 (仅一项通过)             → 权重 ×0.5（弱有效）
  D 级 (都不达标)               → 剔除 ×0.0
  ? 级 (样本不足)               → 原权重保留

V2 更新：
  - 支持按分析模式选择前瞻窗口（eod→5日, intraday/pre→1日）
  - 内存缓存避免同一股票相同数据重复计算

源自 stock-analyst 的 factor_validation 设计。
"""

import logging
import hashlib

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 内存缓存：避免同一股票相同数据重复计算 ──
_validation_cache: dict[str, dict] = {}
_CACHE_MAX_SIZE = 32  # 最多缓存 32 只股票的检验结果

# 阈值
IC_THRESHOLD = 0.06
IR_THRESHOLD = 0.5
IC_GREAT = 0.10
IR_GREAT = 1.0

# 评级 → 权重乘数
GRADE_MULTIPLIER = {"A": 1.0, "B": 1.0, "C": 0.5, "D": 0.0, "?": 1.0}

# 7 个指标列名（与 alpha/scoring.py 一致）
INDICATOR_COLUMNS = ["rsi", "dif", "macd_bar", "bb_pct", "k", "d", "j"]


# 分析模式 → 默认前瞻窗口映射
MODE_FWD_DAYS = {"eod": 5, "intraday": 1, "pre": 1}


def validate_factors(
    df: pd.DataFrame,
    close_col: str = "close",
    fwd_days: int | None = None,
    mode: str = "eod",
    rolling_window: int = 60,
    min_samples: int = 100,
) -> dict[str, dict]:
    """
    对 7 个技术指标逐个做时序 IC/IR 检验，返回评级和调权信息。

    Args:
        df: 包含原始指标列 + close 列的 DataFrame（需有足够行数）
        close_col: 收盘价列名
        fwd_days: 远期收益窗口（显式指定时优先，否则由 mode 决定）
        mode: 分析模式 "eod"→5日, "intraday"/"pre"→1日前瞻
        rolling_window: IC 滚动窗口
        min_samples: 最少样本数

    Returns:
        {指标名: {IC, IR, grade, multiplier, direction_correct, samples}}
    """
    if fwd_days is None:
        fwd_days = MODE_FWD_DAYS.get(mode, 5)

    if close_col not in df.columns:
        logger.warning("因子检验: 缺少 close 列，无法计算远期收益")
        return _all_unknown()

    # ── 缓存检查 ──
    cache_key = _make_cache_key(df, close_col, fwd_days)
    if cache_key in _validation_cache:
        logger.debug(f"因子检验缓存命中 ({cache_key[:12]}...)，跳过重复计算")
        return _validation_cache[cache_key]

    close = df[close_col].astype(float)
    fwd_ret = close.shift(-fwd_days) / close - 1

    results = {}
    for col in INDICATOR_COLUMNS:
        if col not in df.columns:
            results[col] = _unknown_result()
            continue

        factor = df[col].astype(float)

        # 对齐：去掉 NaN
        valid = pd.DataFrame({"factor": factor, "fwd_ret": fwd_ret}).dropna()
        n = len(valid)
        if n < min_samples:
            logger.debug(f"  因子 {col} 样本不足: {n} < {min_samples}")
            results[col] = _unknown_result(min_samples=n)
            continue

        # 时序 IC：Spearman 秩相关
        ic = _spearman_rank_ic(valid["factor"], valid["fwd_ret"])
        abs_ic = abs(ic)

        # 滚动 IR
        ir = _compute_rolling_ir(valid["factor"], valid["fwd_ret"], rolling_window)
        abs_ir = abs(ir)

        # 方向正确性
        direction_correct = _check_direction(valid["factor"], valid["fwd_ret"])

        # 评级
        grade = _grade(abs_ic, abs_ir)
        multiplier = GRADE_MULTIPLIER[grade]

        results[col] = {
            "IC": round(ic, 4),
            "IR": round(ir, 4),
            "grade": grade,
            "multiplier": multiplier,
            "direction_correct": direction_correct,
            "samples": n,
        }

        logger.info(
            f"  因子 {col:<12} IC={ic:+.4f}  IR={ir:+.4f}  "
            f"评级={grade}  multiplier={multiplier}  samples={n}"
        )

    # ── 存入缓存（LRU 淘汰） ──
    if len(_validation_cache) >= _CACHE_MAX_SIZE:
        # 淘汰最早的一个 key
        oldest = next(iter(_validation_cache))
        del _validation_cache[oldest]
    _validation_cache[cache_key] = results

    return results


def apply_factor_weights(
    indicator_values: dict[str, pd.Series],
    validation: dict[str, dict],
    regime_weights: dict[str, float] | None = None,
    prediction_reliability: float = 1.0,
) -> pd.Series:
    """
    应用因子检验结果 + 市场状态权重，加权合成技术面得分。

    综合三层权重：
      1. 检验权重（multiplier）：D 级剔除 ×0.0，C 级半权 ×0.5，A/B 级全权 ×1.0
      2. 方向正确性降权：仅 direction_correct 明确为 False 时 multiplier /= 2
      3. 预测可靠性降权：prediction_reliability < 0.5 → 所有 multiplier *= reliability
      2. 市场状态权重（regime）：趋势市 MACD/ADX 高权重，震荡市 RSI/KDJ 高权重

    Args:
        indicator_values: {列名: 归一化后的 Series（已 tanh）}
        validation: validate_factors 返回的检验结果
        regime_weights: 市场状态权重 {col: weight}，None 则等权

    Returns:
        加权平均后的 Tech_Normalized_Score
    """
    if regime_weights is None:
        n = len(indicator_values)
        regime_weights = {col: 1.0 / n for col in indicator_values}

    weighted = []
    total_weight = 0.0
    # 可靠性不能同时乘到所有权重后再归一化，否则会数学上完全抵消。
    # 先按因子质量得到加权平均，再把最终信号向 0 收缩。
    reliability_mult = float(np.clip(prediction_reliability, 0.0, 1.0))
    for col, series in indicator_values.items():
        v = validation.get(col, {})
        mult = v.get("multiplier", 1.0)
        if mult == 0.0:
            logger.debug(f"  因子 {col} D 级剔除，不参与打分")
            continue
        # 方向性错误的因子降权
        if v.get("direction_correct") is False:
            mult = max(mult / 2, 0.25)
            logger.debug(f"  因子 {col} 方向错误，降权至 {mult:.2f}")
        rw = regime_weights.get(col, 1.0 / len(indicator_values))
        combined_weight = mult * rw
        weighted.append(series * combined_weight)
        total_weight += combined_weight

    if not weighted or total_weight == 0:
        logger.warning("所有因子均被剔除，Tech_Normalized_Score 置 0")
        return pd.Series(0.0, index=next(iter(indicator_values.values())).index)

    # 加权平均
    result = pd.concat(weighted, axis=1).sum(axis=1) / total_weight
    return result * reliability_mult


# ── 内部函数 ──


def _make_cache_key(df: pd.DataFrame, close_col: str, fwd_days: int) -> str:
    """生成因子检验的缓存键（基于 close 列的数据指纹 + 前瞻天数）。"""
    close = df[close_col].astype(float)
    # 用 close 列的关键统计量 + 尾值做轻量指纹
    fingerprint = f"{close.mean():.6f}_{close.std():.6f}_{close.iloc[-1]:.4f}_{len(close)}"
    hash_hex = hashlib.md5(fingerprint.encode()).hexdigest()[:12]
    return f"{hash_hex}_fwd{fwd_days}"


def _all_unknown() -> dict:
    return {col: _unknown_result() for col in INDICATOR_COLUMNS}


def _unknown_result(min_samples: int = 0) -> dict:
    return {
        "IC": 0.0, "IR": 0.0, "grade": "?",
        "multiplier": 1.0, "direction_correct": None,
        "samples": min_samples,
    }


def factor_validation_coverage(
    validation: dict[str, dict],
    factor_names: list[str] | None = None,
) -> float:
    """返回已有足够样本完成 IC/IR 检验的因子比例。"""
    names = factor_names or list(validation)
    if not names:
        return 0.0
    known = sum(
        1 for name in names
        if validation.get(name, {}).get("grade", "?") != "?"
    )
    return known / len(names)


def _grade(abs_ic: float, abs_ir: float) -> str:
    if abs_ic >= IC_GREAT and abs_ir >= IR_GREAT:
        return "A"
    if abs_ic >= IC_THRESHOLD and abs_ir >= IR_THRESHOLD:
        return "B"
    if abs_ic >= IC_THRESHOLD or abs_ir >= IR_THRESHOLD:
        return "C"
    return "D"


def _spearman_rank_ic(a: pd.Series, b: pd.Series) -> float:
    """手动 Spearman 秩相关（不依赖 scipy）。"""
    a_rank = a.rank()
    b_rank = b.rank()
    n = len(a_rank)
    if n < 3:
        return 0.0
    mean_a, mean_b = a_rank.mean(), b_rank.mean()
    num = ((a_rank - mean_a) * (b_rank - mean_b)).sum()
    den = np.sqrt(((a_rank - mean_a) ** 2).sum() * ((b_rank - mean_b) ** 2).sum())
    return float(num / den) if den != 0 else 0.0


def _compute_rolling_ir(
    factor: pd.Series, fwd_ret: pd.Series, window: int = 60,
) -> float:
    """滚动 IC 的 mean/std（IR = Information Ratio）。"""
    n = len(factor)
    ics = []
    for i in range(window, n):
        f_win = factor.iloc[i - window:i]
        r_win = fwd_ret.iloc[i - window:i]
        ic = _spearman_rank_ic(f_win, r_win)
        ics.append(ic)
    if not ics:
        return 0.0
    ic_series = pd.Series(ics)
    mean_ic = ic_series.mean()
    std_ic = ic_series.std()
    return float(mean_ic / std_ic) if std_ic and std_ic > 0 else 0.0


def _check_direction(factor: pd.Series, fwd_ret: pd.Series) -> bool:
    """高因子值是否对应高收益（方向正确性检查）。"""
    median = factor.median()
    high_ret = fwd_ret[factor >= median].mean()
    low_ret = fwd_ret[factor < median].mean()
    return bool(high_ret > low_ret)
