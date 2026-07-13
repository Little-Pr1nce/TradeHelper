"""Pure closed-bar and current-quote feature calculations."""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from typing import Iterable

from tradehelper_v2.contracts import CanonicalBar, FeatureStatus, FeatureValue, QuoteSnapshot


def _value(name: str, value: float | None, at: datetime, sources: tuple[str, ...], *, lookback: int | None = None,
           unit: str | None = None, reason: str | None = None, model_eligible: bool = True,
           status: FeatureStatus | None = None) -> FeatureValue:
    selected = status or (FeatureStatus.AVAILABLE if value is not None else FeatureStatus.MISSING)
    return FeatureValue(name, value, selected, unit, lookback, at, sources, model_eligible, reason)


def _insufficient(name: str, at: datetime, sources: tuple[str, ...], lookback: int) -> FeatureValue:
    return _value(name, None, at, sources, lookback=lookback, reason=f"SAMPLE_LT_{lookback}", status=FeatureStatus.INSUFFICIENT_HISTORY)


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _rsi(closes: list[float]) -> float:
    gains = [max(closes[index] - closes[index - 1], 0.0) for index in range(1, len(closes))]
    losses = [max(closes[index - 1] - closes[index], 0.0) for index in range(1, len(closes))]
    avg_gain = sum(gains[:14]) / 14.0
    avg_loss = sum(losses[:14]) / 14.0
    for gain, loss in zip(gains[14:], losses[14:]):
        avg_gain = (avg_gain * 13.0 + gain) / 14.0
        avg_loss = (avg_loss * 13.0 + loss) / 14.0
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _atr(bars: list[CanonicalBar]) -> float:
    ranges = [max(bar.high - bar.low, abs(bar.high - bars[index - 1].close), abs(bar.low - bars[index - 1].close))
              for index, bar in enumerate(bars[1:], start=1)]
    atr = sum(ranges[:14]) / 14.0
    for true_range in ranges[14:]:
        atr = (atr * 13.0 + true_range) / 14.0
    return atr


def closed_features(
    bars: Iterable[CanonicalBar], available_at: datetime, *, volume_quality_degraded: bool = False,
) -> tuple[FeatureValue, ...]:
    """Calculate technical facts from already point-in-time filtered, ordered bars."""
    ordered = sorted(bars, key=lambda bar: bar.trading_date)
    sources = tuple(sorted({bar.source for bar in ordered}))
    closes = [bar.close for bar in ordered]
    result: list[FeatureValue] = []
    for period in (1, 5, 20, 60):
        name = f"closed.return_{period}"
        result.append(_value(name, closes[-1] / closes[-period - 1] - 1.0, available_at, sources, lookback=period + 1, unit="ratio")
                      if len(closes) >= period + 1 else _insufficient(name, available_at, sources, period + 1))
    ma_values: dict[int, float] = {}
    for period in (5, 10, 20, 60, 120):
        ma_name = f"closed.ma_{period}"
        distance_name = f"closed.ma_distance_{period}"
        if len(closes) < period:
            result.extend((_insufficient(ma_name, available_at, sources, period), _insufficient(distance_name, available_at, sources, period)))
            continue
        average = sum(closes[-period:]) / period
        ma_values[period] = average
        result.extend((_value(ma_name, average, available_at, sources, lookback=period, unit="price"),
                       _value(distance_name, closes[-1] / average - 1.0, available_at, sources, lookback=period, unit="ratio")))
    for period in (20, 60):
        name = f"closed.realized_vol_{period}"
        if len(closes) < period + 1:
            result.append(_insufficient(name, available_at, sources, period + 1))
            continue
        returns = [math.log(closes[index] / closes[index - 1]) for index in range(len(closes) - period, len(closes))]
        mean = sum(returns) / period
        variance = sum((item - mean) ** 2 for item in returns) / (period - 1)
        result.append(_value(name, math.sqrt(variance) * math.sqrt(252.0), available_at, sources, lookback=period + 1, unit="ratio"))
    result.append(_value("closed.rsi_14", _rsi(closes), available_at, sources, lookback=15, unit="index")
                  if len(closes) >= 15 else _insufficient("closed.rsi_14", available_at, sources, 15))
    result.append(_value("closed.atr_pct_14", _atr(ordered) / closes[-1], available_at, sources, lookback=15, unit="ratio")
                  if len(closes) >= 15 else _insufficient("closed.atr_pct_14", available_at, sources, 15))
    if len(closes) < 26:
        result.append(_insufficient("closed.macd_dif_pct", available_at, sources, 26))
    else:
        difs = [fast - slow for fast, slow in zip(_ema(closes, 12), _ema(closes, 26))]
        result.append(_value("closed.macd_dif_pct", difs[-1] / closes[-1], available_at, sources, lookback=26, unit="ratio"))
    if len(closes) < 34:
        result.extend((_insufficient("closed.macd_signal_pct", available_at, sources, 34),
                       _insufficient("closed.macd_hist_pct", available_at, sources, 34)))
    else:
        difs = [fast - slow for fast, slow in zip(_ema(closes, 12), _ema(closes, 26))]
        signal = _ema(difs[25:], 9)[-1]
        result.extend((_value("closed.macd_signal_pct", signal / closes[-1], available_at, sources, lookback=34, unit="ratio"),
                       _value("closed.macd_hist_pct", (difs[-1] - signal) / closes[-1], available_at, sources, lookback=34, unit="ratio")))
    if len(closes) < 20:
        result.extend((_insufficient("closed.bb_pct_20", available_at, sources, 20),
                       _insufficient("closed.bb_width_20", available_at, sources, 20)))
    else:
        window = closes[-20:]
        mid = sum(window) / 20.0
        std = math.sqrt(sum((item - mid) ** 2 for item in window) / 20.0)
        lower, upper = mid - 2.0 * std, mid + 2.0 * std
        if upper == lower:
            result.extend((_value("closed.bb_pct_20", None, available_at, sources, lookback=20, reason="BB_ZERO_WIDTH"),
                           _value("closed.bb_width_20", 0.0, available_at, sources, lookback=20, unit="ratio")))
        else:
            result.extend((_value("closed.bb_pct_20", (closes[-1] - lower) / (upper - lower), available_at, sources, lookback=20, unit="ratio"),
                           _value("closed.bb_width_20", (upper - lower) / mid, available_at, sources, lookback=20, unit="ratio")))
    if len(ordered) < 21:
        result.append(_insufficient("closed.volume_ratio_20", available_at, sources, 21))
    else:
        mean_volume = sum(bar.volume for bar in ordered[-21:-1]) / 20.0
        result.append(_value("closed.volume_ratio_20", ordered[-1].volume / mean_volume, available_at, sources, lookback=21, unit="ratio",
                             model_eligible=not volume_quality_degraded,
                             reason="ZERO_VOLUME_RATIO_HIGH" if volume_quality_degraded else None)
                      if mean_volume > 0 else _value("closed.volume_ratio_20", None, available_at, sources, lookback=21, reason="VOLUME_AVG_ZERO"))
    result.append(_value("closed.gap_1", ordered[-1].open / ordered[-2].close - 1.0, available_at, sources, lookback=2, unit="ratio")
                  if len(ordered) >= 2 else _insufficient("closed.gap_1", available_at, sources, 2))
    for period in (20, 60, 252):
        high_name, low_name = f"closed.high_distance_{period}", f"closed.low_distance_{period}"
        if len(ordered) < period:
            result.extend((_insufficient(high_name, available_at, sources, period), _insufficient(low_name, available_at, sources, period)))
        else:
            window = ordered[-period:]
            result.extend((_value(high_name, closes[-1] / max(bar.high for bar in window) - 1.0, available_at, sources, lookback=period, unit="ratio"),
                           _value(low_name, closes[-1] / min(bar.low for bar in window) - 1.0, available_at, sources, lookback=period, unit="ratio")))
    if len(closes) < 60:
        result.append(_insufficient("closed.drawdown_252", available_at, sources, 60))
    else:
        period = min(252, len(closes))
        result.append(_value("closed.drawdown_252", closes[-1] / max(closes[-period:]) - 1.0, available_at, sources, lookback=period, unit="ratio"))
    return tuple(result)


def current_features(quote: QuoteSnapshot | None, closed: tuple[FeatureValue, ...], available_at: datetime,
                     bars: Iterable[CanonicalBar], *, volume_quality_degraded: bool = False) -> tuple[FeatureValue, ...]:
    names = ("current.price", "current.change_from_prev_close", "current.ma_distance_20", "current.ma_distance_60",
             "current.ma_distance_120", "current.spread_pct", "current.volume_vs_daily_20", "current.retreat_from_session_high")
    if quote is None:
        return tuple(_value(name, None, available_at, (), model_eligible=False, reason="QUOTE_MISSING") for name in names)
    if quote.freshness_status.value != "fresh" or quote.observed_at > available_at + timedelta(minutes=5):
        reason = "QUOTE_FUTURE" if quote.observed_at > available_at + timedelta(minutes=5) else f"QUOTE_{quote.freshness_status.value.upper()}"
        status = FeatureStatus.STALE if quote.freshness_status.value == "stale" else FeatureStatus.BLOCKED
        return tuple(_value(name, None, available_at, (quote.source,), model_eligible=False, reason=reason, status=status) for name in names)
    source = (quote.source,)
    by_name = {item.name: item for item in closed}
    result = [_value("current.price", quote.price, available_at, source, unit="price", model_eligible=False),
              _value("current.change_from_prev_close", quote.price / quote.prev_close - 1.0 if quote.prev_close else None, available_at, source, unit="ratio", model_eligible=False, reason=None if quote.prev_close else "QUOTE_PREV_CLOSE_MISSING")]
    for period in (20, 60, 120):
        ma = by_name[f"closed.ma_{period}"]
        result.append(_value(f"current.ma_distance_{period}", quote.price / ma.value - 1.0 if ma.value is not None else None,
                             available_at, source + ma.sources, lookback=period, unit="ratio", model_eligible=False,
                             reason=ma.reason if ma.value is None else None,
                             status=ma.status if ma.value is None else None))
    result.append(_value("current.spread_pct", (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2.0) if quote.ask is not None and quote.bid is not None else None,
                         available_at, source, unit="ratio", model_eligible=False, reason=None if quote.ask is not None and quote.bid is not None else "BID_ASK_MISSING"))
    ordered_bars = sorted(bars, key=lambda bar: bar.trading_date)
    if quote.volume is None:
        result.append(_value("current.volume_vs_daily_20", None, available_at, source, lookback=20, unit="ratio", model_eligible=False, reason="QUOTE_VOLUME_MISSING"))
    elif len(ordered_bars) < 20:
        result.append(_value("current.volume_vs_daily_20", None, available_at, source, lookback=20, unit="ratio", model_eligible=False, reason="SAMPLE_LT_20", status=FeatureStatus.INSUFFICIENT_HISTORY))
    else:
        average_volume = sum(bar.volume for bar in ordered_bars[-20:]) / 20.0
        result.append(_value("current.volume_vs_daily_20", quote.volume / average_volume if average_volume > 0 else None,
                             available_at, source, lookback=20, unit="ratio", model_eligible=False,
                             reason="ZERO_VOLUME_RATIO_HIGH" if average_volume > 0 and volume_quality_degraded else None if average_volume > 0 else "VOLUME_AVG_ZERO"))
    result.append(_value("current.retreat_from_session_high", quote.price / quote.high - 1.0 if quote.high else None,
                         available_at, source, unit="ratio", model_eligible=False, reason=None if quote.high else "QUOTE_HIGH_MISSING"))
    return tuple(result)
