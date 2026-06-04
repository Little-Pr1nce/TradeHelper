"""
多因子 Alpha 打分模型（纯函数模块）。

V2 更新：
  - 因子 IC/IR 有效性检验（D 级剔除、C 级半权）
  - 扩展权重支持基本面因子（风格 15% + 基本面 25%）
"""

import logging

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# 默认权重（无基本面）
DEFAULT_W_TECH = 0.6
DEFAULT_W_NEWS = 0.4

# 盘口因子权重（叠加于默认/扩展权重之上）
W_DEPTH = 0.10

# 扩展权重（含基本面）
W_TECH_EXT = 0.35
W_STYLE = 0.15
W_FUND = 0.25
W_NEWS_EXT = 0.25

# 7 个独立技术指标
INDICATOR_COLUMNS = ["rsi", "dif", "macd_bar", "bb_pct", "k", "d", "j"]


def score_futures_factor(
    nq_change_pct: float,
    es_change_pct: float,
    nq_kline_trend: float = 0.0,
    es_kline_trend: float = 0.0,
) -> float:
    """
    将盘前期指数据映射为宏观情绪因子得分 [-1, +1]。

    Args:
        nq_change_pct: 纳指期货涨跌幅（小数，如 0.008 = +0.8%）
        es_change_pct: 标普期货涨跌幅（小数）
        nq_kline_trend: NQ 5分钟K线趋势得分（阳线占比映射到 [-1,+1]）
        es_kline_trend: ES 5分钟K线趋势得分

    Returns:
        宏观情绪得分，正值偏暖、负值偏冷
    """
    import numpy as np

    # 涨跌幅 → 情绪得分（tanh 映射，1% ≈ +0.29, 2% ≈ +0.54）
    avg_change = (nq_change_pct + es_change_pct) / 2 if nq_change_pct and es_change_pct else nq_change_pct or es_change_pct
    macro_score = float(np.tanh(avg_change * 30))

    # K线趋势得分：期货分时走势反映机构布局方向
    avg_trend = (nq_kline_trend + es_kline_trend) / 2 if nq_kline_trend and es_kline_trend else nq_kline_trend or es_kline_trend

    # 合成：涨跌幅 70% + 走势形态 30%
    combined = 0.7 * macro_score + 0.3 * avg_trend
    return round(float(np.clip(combined, -1.0, 1.0)), 4)


# ── 市场状态自适应权重 ──
# 趋势市权重：MACD、ADX 权重更高
TRENDING_WEIGHTS = {"rsi": 0.15, "dif": 0.25, "macd_bar": 0.25, "bb_pct": 0.10, "k": 0.10, "d": 0.05, "j": 0.10}
# 震荡市权重：RSI、KDJ、布林带权重更高
RANGING_WEIGHTS = {"rsi": 0.20, "dif": 0.10, "macd_bar": 0.10, "bb_pct": 0.20, "k": 0.15, "d": 0.10, "j": 0.15}
# 过渡期 / 默认：等权
EQUAL_WEIGHTS = {col: 1.0 / len(INDICATOR_COLUMNS) for col in INDICATOR_COLUMNS}


def detect_market_regime(df: pd.DataFrame, adx_threshold_high: float = 25,
                         adx_threshold_low: float = 20) -> tuple[str, dict[str, float]]:
    """
    基于 ADX 判断市场状态，返回 (regime, weights)。

    Returns:
        regime: "trending" / "ranging" / "transitional"
        weights: {indicator_col: weight}
    """
    adx_val = _estimate_adx(df)
    if adx_val is None:
        logger.debug("  [Regime] ADX 不可用，使用等权")
        return "transitional", EQUAL_WEIGHTS

    if adx_val > adx_threshold_high:
        logger.info(f"  [Regime] ADX={adx_val:.1f} > {adx_threshold_high} → 趋势市，MACD/ADX 高权重")
        return "trending", TRENDING_WEIGHTS
    elif adx_val < adx_threshold_low:
        logger.info(f"  [Regime] ADX={adx_val:.1f} < {adx_threshold_low} → 震荡市，RSI/KDJ/BB 高权重")
        return "ranging", RANGING_WEIGHTS
    else:
        logger.info(f"  [Regime] ADX={adx_val:.1f} 在 [{adx_threshold_low}, {adx_threshold_high}] → 过渡期等权")
        return "transitional", EQUAL_WEIGHTS


def _estimate_adx(df: pd.DataFrame) -> float | None:
    """估算最近一期的 ADX 值（优先使用已计算的 ADX 列，否则用 ta 库计算）。"""
    import numpy as np
    # 优先查找已存在的 ADX 列
    for col_name in ["adx", "ADX"]:
        if col_name in df.columns:
            val = df[col_name].dropna()
            if len(val) > 0:
                return float(val.iloc[-1])
    # 尝试用 ta 库计算
    try:
        import ta
        if all(c in df.columns for c in ["high", "low", "close"]):
            adx_series = ta.trend.ADXIndicator(
                high=df["high"].astype(float),
                low=df["low"].astype(float),
                close=df["close"].astype(float),
            ).adx()
            if not adx_series.empty:
                val = adx_series.dropna()
                if len(val) > 0:
                    return float(val.iloc[-1])
    except Exception:
        pass
    return None


def _compute_kline_trend(kline_list: list | None) -> float:
    """从 5 分钟 K 线列表计算走势得分（阳线占比映射到 [-1, +1]）。"""
    import numpy as np
    if not kline_list or len(kline_list) < 3:
        return 0.0
    up_bars = 0
    total = 0
    for bar in kline_list:
        if not isinstance(bar, dict):
            continue
        o = bar.get("o", bar.get("open", 0))
        c = bar.get("c", bar.get("close", 0))
        if not o or not c:
            continue
        total += 1
        if c >= o:
            up_bars += 1
    if total < 3:
        return 0.0
    ratio = up_bars / total
    # 映射：ratio=0.5 → 0, ratio=1.0 → +1, ratio=0 → -1
    return round(float(np.tanh((ratio - 0.5) * 4)), 4)


def compute_technical_normalized(
    df: pd.DataFrame, window: int = 60, validate: bool = True,
    validation_mode: str = "eod",
) -> pd.DataFrame:
    result = df.copy()
    available = [c for c in INDICATOR_COLUMNS if c in result.columns]
    if not available:
        logger.warning("无可用技术指标列，Tech_Normalized_Score 置为 0")
        result["Tech_Normalized_Score"] = 0.0
        return result

    logger.info(f"  [Alpha] 技术面归一化：{len(available)} 个指标，窗口={window}")
    normalized_cols = []
    for col in available:
        series = result[col].astype(float)
        roll_mean = series.rolling(window=window, min_periods=max(window // 4, 10)).mean()
        roll_std = series.rolling(window=window, min_periods=max(window // 4, 10)).std()
        normalized = np.tanh((series - roll_mean) / roll_std.replace(0, np.nan))
        result[f"{col}_norm"] = normalized
        normalized_cols.append(f"{col}_norm")

    if validate and "close" in result.columns and len(result) >= 100:
        logger.info("  [Alpha] 因子有效性检验 (IC/IR)...")
        from alpha.validation import validate_factors, apply_factor_weights

        # 市场状态自适应权重
        regime, regime_weights = detect_market_regime(result)

        validation = validate_factors(result, mode=validation_mode)
        d_count = sum(1 for v in validation.values() if v["grade"] == "D")
        if d_count:
            logger.info(f"  [Alpha] {d_count} 个因子 D 级剔除")
        indicator_values = {col: result[f"{col}_norm"] for col in available}
        result["Tech_Normalized_Score"] = apply_factor_weights(
            indicator_values, validation, regime_weights,
        )
    else:
        result["Tech_Normalized_Score"] = result[normalized_cols].mean(axis=1)

    logger.info(f"  [Alpha] 技术面得分有效值: {result['Tech_Normalized_Score'].notna().sum()}/{len(result)}")
    return result


def align_finbert_scores(price_df, news_df, date_col="date", score_col="finbert_score",
                         half_life_days: float = 1.0):
    """将新闻情感得分按日期对齐到价格 DataFrame，含时间衰减加权。

    策略：
      1. 按日期聚合：同一天多条新闻取加权平均，权重 = exp(-ln(2) * days_ago / half_life)
      2. 当天有新闻 → 用当天加权得分；当天无新闻 → 向前填充最近一天的得分。
      3. 半衰期默认 1 天，即 1 天前的新闻权重为今天的 50%。

    这确保每个交易日都有 FinBERT_Score，同时近期新闻比旧新闻更有影响力。
    """
    if news_df is None or news_df.empty:
        logger.info("  [Alpha] 无新闻数据，FinBERT 得分全部置 0")
        return pd.Series(0.0, index=price_df.index, name="FinBERT_Score")
    if date_col not in news_df.columns or score_col not in news_df.columns:
        logger.info("  [Alpha] 新闻 DataFrame 缺少必要列，FinBERT 得分置 0")
        return pd.Series(0.0, index=price_df.index, name="FinBERT_Score")

    news_scores = news_df[[date_col, score_col]].copy()
    news_scores[date_col] = pd.to_datetime(news_scores[date_col])

    # 确定参考日期（最新价格日期）
    if date_col in price_df.columns:
        price_dates_ts = pd.to_datetime(price_df[date_col])
    else:
        price_dates_ts = pd.to_datetime(pd.Series(price_df.index))
    ref_date = price_dates_ts.max()

    # 计算每条新闻的 days_ago 和衰减权重
    news_scores["days_ago"] = (ref_date - news_scores[date_col]).dt.days
    decay_factor = np.log(2) / half_life_days
    news_scores["weight"] = np.exp(-decay_factor * news_scores["days_ago"].clip(lower=0))

    # 按日期加权聚合
    news_scores["date_str"] = news_scores[date_col].dt.strftime("%Y-%m-%d")
    news_scores["weighted_score"] = news_scores[score_col] * news_scores["weight"]
    grouped = news_scores.groupby("date_str").agg(
        {"weighted_score": "sum", "weight": "sum"}
    ).reset_index()
    grouped[score_col] = grouped["weighted_score"] / grouped["weight"]

    price_dates = price_dates_ts.dt.strftime("%Y-%m-%d")

    # 构建日期 → 得分映射，然后对价格日期做前向填充
    score_map = dict(zip(grouped["date_str"], grouped[score_col]))
    raw = [score_map.get(d, np.nan) for d in price_dates]
    # 前向填充：NaN 用最近一个非 NaN 值填充；开头无数据则填 0
    filled = pd.Series(raw, index=price_df.index, name="FinBERT_Score").ffill().fillna(0.0)

    non_zero = (filled != 0.0).sum()
    avg_weight = news_scores["weight"].mean() if len(news_scores) > 0 else 1.0
    logger.info(
        f"  [Alpha] 新闻对齐（时间衰减 half_life={half_life_days}d, "
        f"平均权重={avg_weight:.3f}）：{non_zero}/{len(filled)} 天有新闻数据"
    )
    return filled


def calc_final_score(
    df: pd.DataFrame,
    news_df: pd.DataFrame | None = None,
    w_tech: float = DEFAULT_W_TECH,
    w_news: float = DEFAULT_W_NEWS,
    validate: bool = True,
    fundamental_data: dict | None = None,
    validation_mode: str = "eod",
    depth_score: float = 0.0,
    depth_available: bool = False,
) -> pd.DataFrame:
    result = df.copy()

    if "Tech_Normalized_Score" not in result.columns:
        result = compute_technical_normalized(result, validate=validate,
                                               validation_mode=validation_mode)

    finbert_series = align_finbert_scores(result, news_df)
    tech_score = result["Tech_Normalized_Score"].fillna(0.0)
    finbert_score = finbert_series.fillna(0.0)

    # 盘口因子：仅影响最新一条 Final_Score（单点实时信号）
    depth_adj = 0.0
    if depth_available and abs(depth_score) > 1e-6:
        depth_adj = W_DEPTH * depth_score
        logger.info(f"  [Alpha] 盘口因子: depth_score={depth_score:+.3f} → 调整量={depth_adj:+.4f}")

    if fundamental_data and fundamental_data.get("style_factors"):
        from alpha.fundamental import score_style_factor, score_fundamental_factor
        style = fundamental_data["style_factors"]
        fund = fundamental_data["fundamental_factors"]
        style_score = score_style_factor(
            style.get("pe_percentile", 0.5), style.get("pb_percentile", 0.5))
        fund_score = score_fundamental_factor(
            fund.get("roe", 0), fund.get("gross_margin", 0),
            fund.get("debt_ratio", 0), fund.get("net_profit_yoy", 0),
            fund.get("revenue_yoy", 0))

        logger.info(f"  [Alpha] 合成(扩展): tech={W_TECH_EXT} style={W_STYLE} fund={W_FUND} news={W_NEWS_EXT} depth={W_DEPTH if depth_available else 0}")
        logger.info(f"  [Alpha] style={style_score:.3f} fund={fund_score:.3f}")
        result["Style_Score"] = style_score
        result["Fundamental_Score"] = fund_score
        base = (
            (1.0 - W_DEPTH if depth_available else 1.0) * (
                W_TECH_EXT * tech_score + W_STYLE * style_score
                + W_FUND * fund_score + W_NEWS_EXT * finbert_score
            )
        )
        result["Final_Score"] = (base + depth_adj).clip(-1.0, 1.0)
    else:
        if abs(w_tech + w_news - 1.0) > 1e-10:
            raise ValueError(f"权重约束不满足: {w_tech}+{w_news}≠1.0")
        logger.info(f"  [Alpha] 合成 Final_Score (w_tech={w_tech}, w_news={w_news}, depth={W_DEPTH if depth_available else 0})")
        base = (1.0 - W_DEPTH if depth_available else 1.0) * (
            w_tech * tech_score + w_news * finbert_score
        )
        result["Final_Score"] = (base + depth_adj).clip(-1.0, 1.0)

    result["FinBERT_Score"] = finbert_score.values
    final_valid = result["Final_Score"].dropna()
    if len(final_valid) > 0:
        logger.info(
            f"  [Alpha] Final_Score 统计: mean={final_valid.mean():.3f}, "
            f"std={final_valid.std():.3f}, "
            f">0.6:{(final_valid>0.6).mean()*100:.1f}%, "
            f"<-0.5:{(final_valid<-0.5).mean()*100:.1f}%, "
            f"最新值:{final_valid.iloc[-1]:.3f}"
        )
    return result
