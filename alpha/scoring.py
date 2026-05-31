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

# 扩展权重（含基本面）
W_TECH_EXT = 0.35
W_STYLE = 0.15
W_FUND = 0.25
W_NEWS_EXT = 0.25

# 7 个独立技术指标
INDICATOR_COLUMNS = ["rsi", "dif", "macd_bar", "bb_pct", "k", "d", "j"]


def compute_technical_normalized(
    df: pd.DataFrame, window: int = 60, validate: bool = True,
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
        validation = validate_factors(result)
        d_count = sum(1 for v in validation.values() if v["grade"] == "D")
        if d_count:
            logger.info(f"  [Alpha] {d_count} 个因子 D 级剔除")
        indicator_values = {col: result[f"{col}_norm"] for col in available}
        result["Tech_Normalized_Score"] = apply_factor_weights(indicator_values, validation)
    else:
        result["Tech_Normalized_Score"] = result[normalized_cols].mean(axis=1)

    logger.info(f"  [Alpha] 技术面得分有效值: {result['Tech_Normalized_Score'].notna().sum()}/{len(result)}")
    return result


def align_finbert_scores(price_df, news_df, date_col="date", score_col="finbert_score"):
    if news_df is None or news_df.empty:
        logger.info("  [Alpha] 无新闻数据，FinBERT 得分全部置 0")
        return pd.Series(0.0, index=price_df.index, name="FinBERT_Score")
    if date_col not in news_df.columns or score_col not in news_df.columns:
        logger.info("  [Alpha] 新闻 DataFrame 缺少必要列，FinBERT 得分置 0")
        return pd.Series(0.0, index=price_df.index, name="FinBERT_Score")

    news_scores = news_df[[date_col, score_col]].copy()
    news_scores[date_col] = pd.to_datetime(news_scores[date_col]).dt.strftime("%Y-%m-%d")
    if date_col in price_df.columns:
        price_dates = pd.to_datetime(price_df[date_col]).dt.strftime("%Y-%m-%d")
    else:
        price_dates = pd.to_datetime(pd.Series(price_df.index)).dt.strftime("%Y-%m-%d")

    score_map = dict(zip(news_scores[date_col], news_scores[score_col]))
    result = pd.Series(
        [score_map.get(d, 0.0) for d in price_dates],
        index=price_df.index, name="FinBERT_Score",
    )
    logger.info(f"  [Alpha] 新闻对齐：{(result != 0.0).sum()}/{len(result)} 天有新闻数据")
    return result


def calc_final_score(
    df: pd.DataFrame,
    news_df: pd.DataFrame | None = None,
    w_tech: float = DEFAULT_W_TECH,
    w_news: float = DEFAULT_W_NEWS,
    validate: bool = True,
    fundamental_data: dict | None = None,
) -> pd.DataFrame:
    result = df.copy()

    if "Tech_Normalized_Score" not in result.columns:
        result = compute_technical_normalized(result, validate=validate)

    finbert_series = align_finbert_scores(result, news_df)
    tech_score = result["Tech_Normalized_Score"].fillna(0.0)
    finbert_score = finbert_series.fillna(0.0)

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

        logger.info(f"  [Alpha] 合成(扩展): tech={W_TECH_EXT} style={W_STYLE} fund={W_FUND} news={W_NEWS_EXT}")
        logger.info(f"  [Alpha] style={style_score:.3f} fund={fund_score:.3f}")
        result["Style_Score"] = style_score
        result["Fundamental_Score"] = fund_score
        result["Final_Score"] = (
            W_TECH_EXT * tech_score + W_STYLE * style_score
            + W_FUND * fund_score + W_NEWS_EXT * finbert_score
        ).clip(-1.0, 1.0)
    else:
        if abs(w_tech + w_news - 1.0) > 1e-10:
            raise ValueError(f"权重约束不满足: {w_tech}+{w_news}≠1.0")
        logger.info(f"  [Alpha] 合成 Final_Score (w_tech={w_tech}, w_news={w_news})")
        result["Final_Score"] = (w_tech * tech_score + w_news * finbert_score).clip(-1.0, 1.0)

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
