"""
多因子 Alpha 打分模型（纯函数模块）。

这是整个量化系统的信号源头。严格按以下数学公式实现：

  ┌─────────────────────────────────────────────────────┐
  │ 步骤 1：7 个技术指标各自做滚动 Z-Score（窗口 60）      │
  │         Z_i = (X_i - rolling_mean(X_i, 60))         │
  │                / rolling_std(X_i, 60)               │
  │                                                     │
  │ 步骤 2：tanh(Z_i) 映射至 [-1, +1]，消除极端值          │
  │                                                     │
  │ 步骤 3：等权平均 → Tech_Normalized_Score              │
  │                                                     │
  │ 步骤 4：按日期对齐 FinBERT 新闻得分（无新闻 = 0）       │
  │                                                     │
  │ 步骤 5：Final_Score = 0.6 × Tech + 0.4 × FinBERT    │
  │         Clip 至 [-1.0, +1.0]                        │
  └─────────────────────────────────────────────────────┘

纯函数约束：
  - 禁止任何时间判断、状态记忆或订单逻辑
  - 禁止访问数据库、全局配置或外部 API
  - 相同输入必须产生相同输出（可复现性）
"""

import logging

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# 默认权重：技术面 60% + 新闻面 40%，约束 w_tech + w_news = 1.0
DEFAULT_W_TECH = 0.6
DEFAULT_W_NEWS = 0.4

# 用于技术面归一化的 7 个独立指标列名
# 选择标准：代表不同维度（趋势/动量/波动/超买超卖），避免共线性
INDICATOR_COLUMNS = ["rsi", "dif", "macd_bar", "bb_pct", "k", "d", "j"]


def compute_technical_normalized(df: pd.DataFrame, window: int = 60) -> pd.DataFrame:
    """
    步骤 1-3：对 7 个技术指标做滚动 Z-Score → tanh 映射 → 等权合成。

    处理流程：
      ① 对每个指标列 X，计算滚动均值和标准差（窗口 = 60 个交易日）
      ② Z = (X - mean) / std，标准化为均值为 0、方差为 1 的分布
      ③ tanh(Z) 映射至 (-1, +1)，大值被渐进压缩，不会出现极端离群
      ④ 对同一天所有可用指标取均值 → Tech_Normalized_Score

    为什么用 tanh 而不是直接 clip？
      tanh 是平滑的非线性映射，保留了原始 Z-Score 的大小顺序，
      同时将 ±∞ 渐进压缩到 ±1，比硬 clip 更符合统计直觉。

    Args:
        df: 包含技术指标列的 DataFrame（来自 indicators.technical.calc_all_indicators）
        window: 滚动标准化窗口（默认 60，约一个季度的交易日数）

    Returns:
        添加了 Tech_Normalized_Score 列的 DataFrame
    """
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

        # ① 滚动 Z-Score（min_periods 保证早期数据也能计算，不会全是 NaN）
        roll_mean = series.rolling(window=window, min_periods=max(window // 4, 10)).mean()
        roll_std = series.rolling(window=window, min_periods=max(window // 4, 10)).std()
        z_score = ((series - roll_mean) / roll_std.replace(0, np.nan))

        # ② tanh 非线性压缩
        normalized = np.tanh(z_score)
        result[f"{col}_norm"] = normalized
        normalized_cols.append(f"{col}_norm")

    # ③ 等权平均 → 综合技术面得分
    result["Tech_Normalized_Score"] = result[normalized_cols].mean(axis=1)

    valid_count = result["Tech_Normalized_Score"].notna().sum()
    logger.info(f"  [Alpha] 技术面得分有效值: {valid_count}/{len(result)}")
    return result


def align_finbert_scores(
    price_df: pd.DataFrame,
    news_df: pd.DataFrame,
    date_col: str = "date",
    score_col: str = "finbert_score",
) -> pd.Series:
    """
    步骤 4：将 FinBERT 新闻情感得分按日期对齐到价格 DataFrame。

    关键规则（严禁违反）：
      - 当日有新闻 → 使用 FinBERT 得分
      - 当日无新闻 → 填 0（中性），表示「无信息即无偏向」
      - ❌ 严禁向前填充（ffill）：昨天的新闻不代表今天仍然有效
      - ❌ 严禁向后插值（bfill）：不能用明天的新闻解释今天的价格

    Args:
        price_df: 股价 DataFrame，必须有 date 列
        news_df: 新闻情感 DataFrame，含 date 列和 finbert_score 列（可选）
        date_col: 日期列名（默认 "date"）
        score_col: 情感得分列名（默认 "finbert_score"）

    Returns:
        与 price_df 等长的 FinBERT 得分 Series（缺失日期值为 0）
    """
    if news_df is None or news_df.empty:
        logger.info("  [Alpha] 无新闻数据，FinBERT 得分全部置 0")
        return pd.Series(0.0, index=price_df.index, name="FinBERT_Score")

    if date_col in news_df.columns and score_col in news_df.columns:
        # 统一日期格式为 "YYYY-MM-DD" 以便精确匹配
        news_scores = news_df[[date_col, score_col]].copy()
        news_scores[date_col] = pd.to_datetime(news_scores[date_col]).dt.strftime("%Y-%m-%d")

        if date_col in price_df.columns:
            price_dates = pd.to_datetime(price_df[date_col]).dt.strftime("%Y-%m-%d")
        else:
            price_dates = pd.Series(price_df.index, index=price_df.index)
            price_dates = pd.to_datetime(price_dates).dt.strftime("%Y-%m-%d")

        # 构建日期 → 得分的查找表
        score_map = dict(zip(news_scores[date_col], news_scores[score_col]))
        result = pd.Series(
            [score_map.get(d, 0.0) for d in price_dates],
            index=price_df.index,
            name="FinBERT_Score",
        )

        filled_days = (result != 0.0).sum()
        logger.info(f"  [Alpha] 新闻对齐：{filled_days}/{len(result)} 天有新闻数据")
    else:
        logger.info("  [Alpha] 新闻 DataFrame 缺少必要列，FinBERT 得分置 0")
        result = pd.Series(0.0, index=price_df.index, name="FinBERT_Score")

    return result


def calc_final_score(
    df: pd.DataFrame,
    news_df: pd.DataFrame | None = None,
    w_tech: float = DEFAULT_W_TECH,
    w_news: float = DEFAULT_W_NEWS,
) -> pd.DataFrame:
    """
    步骤 5：合成最终 Alpha 信号分数。

    公式：
      Final_Score = w_tech × Tech_Normalized_Score + w_news × FinBERT_Score
      Final_Score = clip(Final_Score, -1.0, +1.0)

    约束：
      w_tech + w_news = 1.0（所有权重必须归一化）

    解读：
      Final_Score >  0.6  → 强买入信号（策略 A 触发）
      Final_Score >  0.7  → 极强买入（策略 C 触发条件之一）
      Final_Score < -0.5  → 超卖信号（策略 B 触发条件之一）
      Final_Score 约 0.0  → 中性，无明确方向

    Args:
        df: 包含技术指标列的股价 DataFrame
        news_df: 新闻情感 DataFrame，可为 None
        w_tech: 技术面权重（默认 0.6）
        w_news: 新闻面权重（默认 0.4）

    Returns:
        添加了 Final_Score 和 FinBERT_Score 列的 DataFrame

    Raises:
        ValueError: 权重之和不等于 1.0
    """
    # 权重归一化校验
    if abs(w_tech + w_news - 1.0) > 1e-10:
        raise ValueError(
            f"权重约束不满足: w_tech({w_tech}) + w_news({w_news}) = {w_tech + w_news} ≠ 1.0"
        )

    logger.info(f"  [Alpha] 合成 Final_Score (w_tech={w_tech}, w_news={w_news})")
    result = df.copy()

    # 步骤 1-3：技术面归一化（如果尚未计算）
    if "Tech_Normalized_Score" not in result.columns:
        result = compute_technical_normalized(result)

    # 步骤 4：对齐 FinBERT 新闻得分
    finbert_series = align_finbert_scores(result, news_df)

    # 步骤 5：加权合成 + Clip
    tech_score = result["Tech_Normalized_Score"].fillna(0.0)
    finbert_score = finbert_series.fillna(0.0)

    result["FinBERT_Score"] = finbert_score.values
    result["Final_Score"] = (w_tech * tech_score + w_news * finbert_score).clip(-1.0, 1.0)

    # 打印得分分布（方便调试和监控）
    final_valid = result["Final_Score"].dropna()
    if len(final_valid) > 0:
        logger.info(
            f"  [Alpha] Final_Score 统计: mean={final_valid.mean():.3f}, "
            f"std={final_valid.std():.3f}, "
            f">0.6:{(final_valid > 0.6).mean()*100:.1f}%, "
            f"<-0.5:{(final_valid < -0.5).mean()*100:.1f}%, "
            f"最新值:{final_valid.iloc[-1]:.3f}"
        )

    return result
