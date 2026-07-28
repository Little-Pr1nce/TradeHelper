"""按交易所日历生成标签，并构造不可变训练样本。

未来收益的终点永远是第 h 个正式交易日，不能以自然日相加；这同时
避免周末、节假日和 A 股/美股交易日不同造成的标签错位。
"""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Mapping

from contracts import (
    CanonicalBar, ContractViolation, FeatureEvidenceMode, FeatureSnapshot, ForecastDirection,
    ForecastScope, ForecastTrainingSample, InstrumentId,
)
from data.calendar import TradingCalendar, TradingCalendarUnavailable


def target_session_date(calendar: TradingCalendar, instrument: InstrumentId, origin_session_date: date, horizon: int) -> date:
    if horizon not in {1, 3, 5, 10}:
        raise ContractViolation("unsupported forecast horizon")
    return calendar.target_dates(instrument.market, origin_session_date, (horizon,))[horizon]


def flat_band(realized_vol_20: float, horizon: int) -> float:
    """以 origin 当时 20 日波动率计算三分类的“震荡”带宽。

    上下限避免极低波动时标签过度敏感，也避免高波动股票的中性类别
    消失；任何改动都需要升级标签政策版本并重新做 OOF。
    """
    if horizon not in {1, 3, 5, 10} or not math.isfinite(realized_vol_20) or realized_vol_20 < 0:
        raise ContractViolation("flat band requires non-negative realized volatility and supported horizon")
    return min(0.04, max(0.005, 0.35 * (realized_vol_20 / math.sqrt(252.0)) * math.sqrt(horizon)))


def direction_label(future_return: float, band: float) -> ForecastDirection:
    if future_return > band:
        return ForecastDirection.BULLISH
    if future_return < -band:
        return ForecastDirection.BEARISH
    return ForecastDirection.NEUTRAL


def build_training_sample(
    *,
    calendar: TradingCalendar,
    feature_snapshot: FeatureSnapshot,
    reference_bar: CanonicalBar,
    target_bar: CanonicalBar,
    horizon: int,
    scope_membership: Mapping[ForecastScope, str] | None = None,
    scope_membership_available_at: Mapping[ForecastScope, datetime] | None = None,
) -> ForecastTrainingSample | None:
    """构造正式样本；origin 缺少当时波动率时明确返回 ``None``。

    不用今天的波动率补历史标签，也不把缺失波动率当作固定阈值，避免
    训练集获得历史上并不存在的信息。
    """
    if reference_bar.instrument != feature_snapshot.instrument or reference_bar.trading_date != feature_snapshot.latest_bar_date:
        raise ContractViolation("training reference bar must match feature snapshot")
    if feature_snapshot.mode.value != "eod":
        raise ContractViolation("training labels require an EOD feature snapshot")
    try:
        visible_session = calendar.latest_completed_session(reference_bar.instrument.market, feature_snapshot.cutoff_at)
    except TradingCalendarUnavailable as exc:
        raise ContractViolation("cannot validate training snapshot cutoff") from exc
    if visible_session != reference_bar.trading_date:
        raise ContractViolation("training snapshot cutoff is not point-in-time for its origin session")
    expected_target = target_session_date(calendar, reference_bar.instrument, reference_bar.trading_date, horizon)
    if target_bar.instrument != reference_bar.instrument or target_bar.trading_date != expected_target or target_bar.adjustment_mode != reference_bar.adjustment_mode:
        raise ContractViolation("training target bar must be the calendar target in the same canonical series")
    values = {item.name: item for item in feature_snapshot.values}
    vol = values.get("closed.realized_vol_20")
    if vol is None or vol.status.value != "available" or not isinstance(vol.value, (float, int)):
        return None
    band = flat_band(float(vol.value), horizon)
    future_return = target_bar.close / reference_bar.close - 1.0
    membership = dict(scope_membership or {})
    membership.setdefault(ForecastScope.STOCK, reference_bar.instrument.stable_key)
    return ForecastTrainingSample(
        instrument=reference_bar.instrument, scope_membership=membership,
        origin_session_date=reference_bar.trading_date, target_session_date=target_bar.trading_date,
        horizon=horizon, reference_price=reference_bar.close, target_price=target_bar.close,
        future_return=future_return, flat_band=band, direction=direction_label(future_return, band),
        feature_snapshot=feature_snapshot, feature_hash=feature_snapshot.feature_hash,
        evidence_mode=feature_snapshot.evidence_mode, matured_at=target_bar.trading_date,
        scope_membership_available_at=scope_membership_available_at or {},
    )


def matured_samples(samples: tuple[ForecastTrainingSample, ...], origin: ForecastTrainingSample) -> tuple[ForecastTrainingSample, ...]:
    """在某个 OOF 起点只保留当日已到期的历史标签，执行 purge。"""
    return tuple(sample for sample in samples if sample.origin_session_date < origin.origin_session_date and sample.target_session_date <= origin.origin_session_date)
