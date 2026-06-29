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

# 扩展权重（含基本面）— 默认（震荡市/过渡期）
W_TECH_EXT = 0.35
W_STYLE = 0.15
W_FUND = 0.25
W_NEWS_EXT = 0.25

# 行情自适应权重：趋势市中弱化估值因子（强趋势下 PE/PB 分位参考价值降低），
# 震荡市中恢复默认权重（估值锚定更关键）。
# 所有权重和 = 1.0（不含盘口因子 10%）。
_REGIME_WEIGHT_MAP = {
    # 强趋势+高波动：趋势跟踪 > 估值，风格因子仅 5%
    "trending_volatile": {"tech": 0.40, "style": 0.05, "fund": 0.30, "news": 0.25},
    # 弱趋势/慢涨：估值参考价值中等
    "trending_steady":  {"tech": 0.38, "style": 0.10, "fund": 0.27, "news": 0.25},
    "trending":         {"tech": 0.38, "style": 0.10, "fund": 0.27, "news": 0.25},
    # 震荡市/过渡期：默认权重，估值因子恢复 15%
    "ranging":          {"tech": 0.35, "style": 0.15, "fund": 0.25, "news": 0.25},
    "transitional":     {"tech": 0.35, "style": 0.15, "fund": 0.25, "news": 0.25},
}

# 7 个独立技术指标
INDICATOR_COLUMNS = ["rsi", "dif", "macd_bar", "bb_pct", "k", "d", "j"]


def score_futures_factor(
    nq_change_pct: float,
    es_change_pct: float,
) -> float:
    """
    将盘前期指涨跌幅映射为宏观情绪因子得分 [-1, +1]。

    仅用期货涨跌幅判断情绪（tanh 映射），不再使用 5 分钟 K 线走势
    （TickFlow 不支持期货分钟 K 线）。

    Returns:
        宏观情绪得分，正值偏暖、负值偏冷
    """
    import numpy as np

    # 涨跌幅 → 情绪得分（tanh 映射，1% ≈ +0.29, 2% ≈ +0.54）
    avg_change = (nq_change_pct + es_change_pct) / 2 if nq_change_pct and es_change_pct else nq_change_pct or es_change_pct
    combined = float(np.tanh(avg_change * 30))
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
    基于 ADX + 波动率 + 长期趋势判断市场状态，返回 (regime, weights)。

    改进：使用近 20 日 ADX 均值替代单点值，避免短期盘整导致误判震荡。
    同时加入长期趋势辅助判断（价 vs MA60），防止大涨后的横盘被误分类。

    Returns:
        regime: "trending_volatile" / "trending_steady" / "ranging" / "transitional" / "unknown"
        weights: {indicator_col: weight}

    Regime 说明：
      - trending_volatile: 强趋势 + 高波动（ATR/price > 5%）— 适合突破策略
      - trending_steady:   弱趋势/慢涨（ATR/price ≤ 5%）— 适合均线跟随
      - ranging:           震荡 — 适合均值回归、布林
      - transitional:      过渡期 — 等权保守
      - unknown:           数据不足
    """
    adx_val, adx_mean = _estimate_adx(df, return_mean=True)
    if adx_val is None:
        logger.debug("  [Regime] ADX 不可用 → unknown")
        return "unknown", EQUAL_WEIGHTS

    # 长期趋势辅助判断：如果全周期涨幅显著且价格在 MA60 之上，至少视为慢涨
    long_term_trending = False
    if len(df) >= 60:
        close = df["close"].astype(float)
        ma60 = close.rolling(60).mean()
        latest_close = float(close.iloc[-1])
        latest_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else 0
        total_return = (latest_close - float(close.iloc[0])) / float(close.iloc[0]) if float(close.iloc[0]) > 0 else 0
        # 总涨幅 > 50% 且价格在 MA60 之上 = 客观处于上升趋势中
        if total_return > 0.50 and latest_close > latest_ma60 > 0:
            long_term_trending = True

    # 使用近 20 日均值作为主判断（避免单点 ADX 受短期盘整影响）
    adx_judge = adx_mean if adx_mean is not None else adx_val

    # 计算波动率子类型
    atr_pct = _estimate_atr_pct(df)

    if adx_judge > adx_threshold_high:
        if atr_pct is not None and atr_pct > 0.05:
            logger.info(f"  [Regime] ADX={adx_val:.1f}(均值{adx_mean:.1f}) ATR%={atr_pct:.1%} → 强趋势+高波")
            return "trending_volatile", TRENDING_WEIGHTS
        else:
            atr_str = f"{atr_pct:.1%}" if atr_pct is not None else "?"
            logger.info(f"  [Regime] ADX={adx_val:.1f}(均值{adx_mean:.1f}) ATR%={atr_str} → 慢涨/弱趋势")
            return "trending_steady", TRENDING_WEIGHTS
    elif adx_judge < adx_threshold_low:
        # 即使短期 ADX 低，若长期趋势明确向上，仍视为慢涨而非震荡
        if long_term_trending:
            logger.info(f"  [Regime] ADX={adx_val:.1f}(均值{adx_mean:.1f}) < {adx_threshold_low}"
                        f" 但长期趋势向上 → 慢涨/弱趋势")
            return "trending_steady", TRENDING_WEIGHTS
        logger.info(f"  [Regime] ADX={adx_val:.1f}(均值{adx_mean:.1f}) < {adx_threshold_low} → 震荡市")
        return "ranging", RANGING_WEIGHTS
    else:
        # 过渡期但长期趋势向上 → 升级为慢涨
        if long_term_trending:
            logger.info(f"  [Regime] ADX={adx_val:.1f}(均值{adx_mean:.1f}) 过渡期 + 长期向上 → 慢涨/弱趋势")
            return "trending_steady", TRENDING_WEIGHTS
        logger.info(f"  [Regime] ADX={adx_val:.1f}(均值{adx_mean:.1f}) 在 [{adx_threshold_low}, {adx_threshold_high}] → 过渡期")
        return "transitional", EQUAL_WEIGHTS


def _estimate_adx(df: pd.DataFrame, return_mean: bool = False) -> float | None | tuple[float | None, float | None]:
    """估算最近一期 ADX 值（优先使用已计算的 ADX 列，否则用 ta 库计算）。

    Args:
        df: K 线 DataFrame
        return_mean: True 时同时返回 (最新值, 近20日均值)
    """
    import numpy as np
    # 优先查找已存在的 ADX 列
    for col_name in ["adx", "ADX"]:
        if col_name in df.columns:
            val = df[col_name].dropna()
            if len(val) > 0:
                latest = float(val.iloc[-1])
                if return_mean:
                    mean_val = float(val.iloc[-20:].mean()) if len(val) >= 5 else latest
                    return latest, mean_val
                return latest
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
                    latest = float(val.iloc[-1])
                    if return_mean:
                        mean_val = float(val.iloc[-20:].mean()) if len(val) >= 5 else latest
                        return latest, mean_val
                    return latest
    except Exception:
        pass
    if return_mean:
        return None, None
    return None


def _estimate_atr_pct(df: pd.DataFrame, window: int = 20) -> float | None:
    """估算 ATR 占股价的比例（%）。"""
    try:
        if all(c in df.columns for c in ["high", "low", "close"]):
            import ta
            atr = ta.volatility.AverageTrueRange(
                high=df["high"].astype(float),
                low=df["low"].astype(float),
                close=df["close"].astype(float),
                window=window,
            ).average_true_range()
            if not atr.empty:
                last_atr = float(atr.dropna().iloc[-1])
                last_close = float(df["close"].dropna().iloc[-1])
                if last_close > 0:
                    return last_atr / last_close
    except Exception:
        pass
    return None


def compute_technical_normalized(
    df: pd.DataFrame, window: int = 60, validate: bool = True,
    validation_mode: str = "eod",
    prediction_reliability: float = 1.0,
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

    # 历史分数必须保持因果性。完整样本的 IC/IR 可以用来评估“今天”应使用
    # 哪些因子，但不能用它回写过去每一天的权重。
    result["Tech_Normalized_Score"] = result[normalized_cols].mean(axis=1)
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
        adjusted = apply_factor_weights(
            indicator_values, validation, regime_weights,
            prediction_reliability=prediction_reliability,
        )
        if len(result) > 0 and pd.notna(adjusted.iloc[-1]):
            result.loc[result.index[-1], "Tech_Normalized_Score"] = float(adjusted.iloc[-1])

    logger.info(f"  [Alpha] 技术面得分有效值: {result['Tech_Normalized_Score'].notna().sum()}/{len(result)}")
    return result


def align_finbert_scores(price_df, news_df, date_col="date", score_col="finbert_score",
                         half_life_days: float = 1.0):
    """将新闻情感得分按日期对齐到价格 DataFrame，含时间衰减加权。

    策略：
      1. 按日期聚合同日新闻。
      2. 每个交易日只能使用当日或之前已发布的最近新闻。
      3. 无新闻日不做无限前向填充，而是按日历日距离持续半衰。

    这确保每个交易日都有 FinBERT_Score，同时近期新闻比旧新闻更有影响力。
    """
    if news_df is None or news_df.empty:
        logger.info("  [Alpha] 无新闻数据，FinBERT 得分全部置 0")
        return pd.Series(0.0, index=price_df.index, name="FinBERT_Score")
    if date_col not in news_df.columns or score_col not in news_df.columns:
        logger.info("  [Alpha] 新闻 DataFrame 缺少必要列，FinBERT 得分置 0")
        return pd.Series(0.0, index=price_df.index, name="FinBERT_Score")

    news_scores = news_df[[date_col, score_col]].copy()
    news_scores[date_col] = pd.to_datetime(news_scores[date_col], errors="coerce")
    news_scores[score_col] = pd.to_numeric(news_scores[score_col], errors="coerce")
    news_scores = news_scores.dropna(subset=[date_col, score_col])
    if news_scores.empty:
        return pd.Series(0.0, index=price_df.index, name="FinBERT_Score")

    if date_col in price_df.columns:
        price_dates_ts = pd.to_datetime(price_df[date_col], errors="coerce")
    else:
        price_dates_ts = pd.to_datetime(pd.Series(price_df.index), errors="coerce")
    if half_life_days <= 0:
        raise ValueError("half_life_days 必须大于 0")

    news_scores["news_day"] = news_scores[date_col].dt.normalize()
    grouped = (
        news_scores.groupby("news_day", as_index=False)[score_col]
        .mean()
        .sort_values("news_day")
    )
    price_points = pd.DataFrame({
        "price_day": price_dates_ts.dt.normalize(),
        "_row_order": np.arange(len(price_df)),
    }).dropna(subset=["price_day"]).sort_values("price_day")
    if price_points.empty:
        return pd.Series(0.0, index=price_df.index, name="FinBERT_Score")
    aligned = pd.merge_asof(
        price_points,
        grouped.rename(columns={"news_day": "matched_news_day"}),
        left_on="price_day",
        right_on="matched_news_day",
        direction="backward",
    )
    days_since = (aligned["price_day"] - aligned["matched_news_day"]).dt.days
    decay_factor = np.log(2) / half_life_days
    aligned[score_col] = (
        pd.to_numeric(aligned[score_col], errors="coerce").fillna(0.0)
        * np.exp(-decay_factor * days_since.fillna(0).clip(lower=0))
    )
    aligned = aligned.sort_values("_row_order")
    values = np.zeros(len(price_df), dtype=float)
    values[aligned["_row_order"].astype(int).to_numpy()] = aligned[score_col].to_numpy()
    filled = pd.Series(values, index=price_df.index, name="FinBERT_Score")

    non_zero = (filled != 0.0).sum()
    logger.info(
        f"  [Alpha] 新闻对齐（时间衰减 half_life={half_life_days}d）："
        f"{non_zero}/{len(filled)} 天有可用情绪残留"
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
    market_regime: str = "",
    prediction_reliability: float = 1.0,
) -> pd.DataFrame:
    result = df.copy()

    if "Tech_Normalized_Score" not in result.columns:
        result = compute_technical_normalized(result, validate=validate,
                                               validation_mode=validation_mode,
                                               prediction_reliability=1.0)

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

        # ── 行情自适应权重：趋势市弱化估值因子 ──
        rw = _REGIME_WEIGHT_MAP.get(market_regime, _REGIME_WEIGHT_MAP["ranging"])
        w_tech_ext = rw["tech"]
        w_style = rw["style"]
        w_fund = rw["fund"]
        w_news_ext = rw["news"]

        style_score = score_style_factor(
            style.get("pe_percentile", 0.5), style.get("pb_percentile", 0.5),
            fund.get("ev_ebitda", 0))
        fund_score = score_fundamental_factor(
            fund.get("roe", 0), fund.get("gross_margin", 0),
            fund.get("debt_ratio", 0), fund.get("net_profit_yoy", 0),
            fund.get("revenue_yoy", 0))

        logger.info(
            f"  [Alpha] 合成(扩展, regime={market_regime}): "
            f"tech={w_tech_ext} style={w_style} fund={w_fund} news={w_news_ext} "
            f"depth={W_DEPTH if depth_available else 0}"
        )
        logger.info(f"  [Alpha] style={style_score:.3f} fund={fund_score:.3f}")
        result["Style_Score"] = style_score
        result["Fundamental_Score"] = fund_score
        # 基本面/估值和盘口都是“当前可得”的单点信息，不能写进整段历史
        # 回测分数，否则会把今天的财务状态和盘口用于过去交易。
        historical_base = w_tech * tech_score + w_news * finbert_score
        result["Final_Score"] = historical_base.clip(-1.0, 1.0)
        if len(result) > 0:
            latest_base = (
                w_tech_ext * float(tech_score.iloc[-1])
                + w_style * style_score
                + w_fund * fund_score
                + w_news_ext * float(finbert_score.iloc[-1])
            )
            if depth_available:
                latest_base = (1.0 - W_DEPTH) * latest_base + depth_adj
            result.loc[result.index[-1], "Final_Score"] = float(np.clip(latest_base, -1.0, 1.0))
    else:
        if abs(w_tech + w_news - 1.0) > 1e-10:
            raise ValueError(f"权重约束不满足: {w_tech}+{w_news}≠1.0")
        logger.info(f"  [Alpha] 合成 Final_Score (w_tech={w_tech}, w_news={w_news}, depth={W_DEPTH if depth_available else 0})")
        base = w_tech * tech_score + w_news * finbert_score
        result["Final_Score"] = base.clip(-1.0, 1.0)
        if depth_available and len(result) > 0:
            latest_base = (1.0 - W_DEPTH) * float(base.iloc[-1]) + depth_adj
            result.loc[result.index[-1], "Final_Score"] = float(np.clip(latest_base, -1.0, 1.0))

    # 预测可靠性是“当前已知”的状态，只能收缩最新一个交易信号，
    # 不能回写整段历史因子并影响回测。
    if len(result) > 0:
        reliability = float(np.clip(prediction_reliability, 0.0, 1.0))
        latest_idx = result.index[-1]
        result.loc[latest_idx, "Final_Score"] = float(
            np.clip(float(result.loc[latest_idx, "Final_Score"]) * reliability, -1.0, 1.0)
        )

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
