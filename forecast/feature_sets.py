"""冻结的 V2-3 特征白名单与确定性输入哈希。

预测层只从这里登记的名称取值。即使 FeatureSnapshot 里出现了新的
字段，也不能在未升级 FeatureSet 版本时“顺手”进入模型，避免线上
模型输入随数据源变化而漂移。
"""

from __future__ import annotations

import math

from contracts import FeatureSnapshot, FeatureStatus, stable_hash


TECHNICAL_CORE_V1 = (
    "closed.return_1", "closed.return_5", "closed.return_20", "closed.return_60",
    "closed.ma_distance_5", "closed.ma_distance_10", "closed.ma_distance_20", "closed.ma_distance_60", "closed.ma_distance_120",
    "closed.realized_vol_20", "closed.realized_vol_60", "closed.rsi_14", "closed.atr_pct_14",
    "closed.macd_dif_pct", "closed.macd_signal_pct", "closed.macd_hist_pct", "closed.bb_pct_20", "closed.bb_width_20",
    "closed.volume_ratio_20", "closed.gap_1", "closed.high_distance_20", "closed.high_distance_60", "closed.high_distance_252",
    "closed.low_distance_20", "closed.low_distance_60", "closed.low_distance_252", "closed.drawdown_252",
)
TREND_CORE_V1 = (
    "closed.return_5", "closed.return_20", "closed.return_60",
    "closed.ma_distance_10", "closed.ma_distance_20", "closed.ma_distance_60", "closed.ma_distance_120",
    "closed.macd_dif_pct", "closed.macd_hist_pct",
    "closed.high_distance_20", "closed.high_distance_60", "closed.high_distance_252",
    "closed.drawdown_252",
)
REVERSION_CORE_V1 = (
    "closed.return_1", "closed.return_5", "closed.return_20",
    "closed.rsi_14", "closed.bb_pct_20", "closed.bb_width_20",
    "closed.atr_pct_14", "closed.realized_vol_20", "closed.volume_ratio_20",
    "closed.gap_1", "closed.low_distance_20", "closed.high_distance_20",
)
NEWS_V1 = ("news.count_1d", "news.count_7d", "news.count_30d", "news.source_count_30d", "news.sentiment_weighted_1d", "news.sentiment_weighted_7d", "news.sentiment_change", "news.latest_age_hours", "news.scored_ratio_30d")
FUNDAMENTALS_V1 = ("fund.pe_ttm", "fund.pb_mrq", "fund.ps_ttm", "fund.roe", "fund.gross_margin", "fund.revenue_growth_yoy", "fund.net_profit_growth_yoy", "fund.debt_ratio")
CONTEXT_V1: tuple[str, ...] = ()
FEATURE_SET_VERSION = "forecast_feature_sets_v1"


def feature_names(feature_set_id: str, snapshot: FeatureSnapshot | None = None) -> tuple[str, ...]:
    """返回候选模型允许读取的原始字段，不返回 current 或 LLM 字段。"""
    if feature_set_id == "tech":
        return TECHNICAL_CORE_V1
    if feature_set_id == "trend":
        return TREND_CORE_V1
    if feature_set_id == "reversion":
        return REVERSION_CORE_V1
    if feature_set_id == "tech_news":
        return TECHNICAL_CORE_V1 + NEWS_V1
    if feature_set_id == "tech_fund":
        return TECHNICAL_CORE_V1 + FUNDAMENTALS_V1
    if feature_set_id == "full":
        # V2-2 尚无权威 context 数值合同。不能根据单个快照动态改变列集合；
        # 将来只有升级 CONTEXT_V1/FeatureSet 版本后才能加入模型。
        return TECHNICAL_CORE_V1 + NEWS_V1 + FUNDAMENTALS_V1 + CONTEXT_V1
    raise ValueError(f"unknown V2-3 feature set: {feature_set_id}")


def extract_feature_row(snapshot: FeatureSnapshot, feature_set_id: str) -> tuple[tuple[str, ...], tuple[float | None, ...]]:
    """把快照投影成白名单顺序的数值行；不可用事实保留为缺失值。"""
    names = feature_names(feature_set_id, snapshot)
    values = {item.name: item for item in snapshot.values}
    row: list[float | None] = []
    for name in names:
        item = values.get(name)
        if item is None or item.status is not FeatureStatus.AVAILABLE or not item.model_eligible or not isinstance(item.value, (int, float)) or isinstance(item.value, bool) or not math.isfinite(float(item.value)):
            row.append(None)
        else:
            row.append(float(item.value))
    return names, tuple(row)


def extension_coverage(samples: tuple, feature_set_id: str) -> float:
    """计算新闻/基本面扩展组的样本覆盖率，供候选准入而非填补缺失。"""
    if feature_set_id in {"tech", "trend", "reversion"}:
        return 1.0
    prefix = "news." if feature_set_id == "tech_news" else "fund." if feature_set_id == "tech_fund" else None
    if not samples:
        return 0.0
    if feature_set_id == "full":
        return min(extension_coverage(samples, "tech_news"), extension_coverage(samples, "tech_fund"))
    covered = 0
    for sample in samples:
        names, row = extract_feature_row(sample.feature_snapshot, feature_set_id)
        if any(name.startswith(prefix) and value is not None for name, value in zip(names, row)):
            covered += 1
    return covered / len(samples)


def model_input_hash(snapshot: FeatureSnapshot, origin_session_date, feature_set_id: str) -> str:
    """生成预测身份所需哈希；刻意排除请求时间和实时 current 特征。"""
    names = feature_names(feature_set_id, snapshot)
    by_name = {item.name: item for item in snapshot.values}
    payload = []
    for name in names:
        item = by_name.get(name)
        payload.append({"name": name, "value": item.value if item else None, "status": item.status.value if item else "missing", "model_eligible": item.model_eligible if item else False, "sources": item.sources if item else ()})
    return stable_hash({"instrument": snapshot.instrument.to_dict(), "origin_session": origin_session_date, "feature_set_id": feature_set_id, "feature_set_version": FEATURE_SET_VERSION, "preprocessing_version": "robust_missing_v1", "features": payload})
