"""V2-3 预测入口：只发行 ForecastResult，绝不创建交易动作。

盘前/盘中应复用最近完成日的 EOD FeatureSnapshot；这里故意不读取实时
quote，因此当前价格变化不会改变同一收盘事实对应的预测身份。
"""

from __future__ import annotations

from datetime import datetime, timezone

from tradehelper_v2.contracts import (
    ForecastAvailability, ForecastRequest, ForecastResult, ForecastScope, ModelFamily,
    ModelLifecycle, ValidationStatus,
)
from tradehelper_v2.data.calendar import TradingCalendar, TradingCalendarUnavailable

from .feature_sets import FEATURE_SET_VERSION, model_input_hash
from .labels import flat_band
from .models import InsufficientRegimeSamples, local_replacement_drivers, predict_model
from .registry import ForecastRegistry


class ForecastEngine:
    """组合日历、模型注册表和特征快照，生成可持久化预测事实。"""
    def __init__(self, calendar: TradingCalendar, registry: ForecastRegistry) -> None:
        self.calendar = calendar
        self.registry = registry

    def forecast(self, request: ForecastRequest, *, samples=(), industry_key: str | None = None) -> tuple[ForecastResult, ...]:
        """每个 horizon 独立选择 Champion/基线，绝不跨周期复用模型。"""
        try:
            targets = self.calendar.target_dates(request.feature_snapshot.instrument.market, request.feature_snapshot.latest_bar_date, request.horizons)
        except TradingCalendarUnavailable:
            return tuple(self._unavailable(request, horizon, ForecastAvailability.CALENDAR_UNAVAILABLE, "trading calendar unavailable") for horizon in request.horizons)
        results = []
        for horizon in request.horizons:
            target = targets[horizon]
            if request.data_blocked:
                results.append(self._unavailable(request, horizon, ForecastAvailability.DATA_BLOCKED, "input data quality is blocked", target))
                continue
            selected = self.registry.resolve(market=request.feature_snapshot.instrument.market, stock_key=request.feature_snapshot.instrument.stable_key, industry_key=industry_key, horizon=horizon)
            baseline = None
            if selected is not None:
                version, model = selected.version, selected.model
                try:
                    probabilities, distribution = predict_model(model, request.feature_snapshot)
                except InsufficientRegimeSamples:
                    selected = None
                else:
                    execution_eligible = version.scope is ForecastScope.STOCK
                    results.append(self._available(request, horizon, target, probabilities, distribution, version.scope, version.scope_key, version.spec.family, version.version, version.lifecycle, version.validation_status, version.spec.feature_set_id, version.training_data_hash, version.sample_count, version.oof_sample_count, local_replacement_drivers(model, request.feature_snapshot), execution_eligible))
            if selected is None:
                baseline = self.registry.baseline(
                    tuple(samples), market=request.feature_snapshot.instrument.market,
                    stock_key=request.feature_snapshot.instrument.stable_key, horizon=horizon,
                    origin_session_date=request.feature_snapshot.latest_bar_date,
                )
            if selected is not None:
                continue
            if baseline is not None:
                scope, key, model = baseline
                probabilities, distribution = predict_model(model, request.feature_snapshot)
                results.append(self._available(request, horizon, target, probabilities, distribution, scope, key, ModelFamily.EMPIRICAL, f"{scope.value}-empirical-baseline-h{horizon}", ModelLifecycle.CANDIDATE, ValidationStatus.NOT_EVALUATED, "tech", None, len(model.training_labels), 0, (), False))
            else:
                results.append(self._unavailable(request, horizon, ForecastAvailability.INSUFFICIENT_SAMPLE, "no eligible champion or baseline", target))
        return tuple(results)

    def _available(self, request, horizon, target, probabilities, distribution, scope, scope_key, family, version, lifecycle, status, feature_set_id, training_hash, sample_count, oof_count, drivers, execution_eligible):
        """把模型输出组装为严格校验的可用 ForecastResult。"""
        priority = {"bullish": 0, "bearish": 1, "neutral": 2}
        directions = ((probabilities.bullish, "bullish"), (probabilities.neutral, "neutral"), (probabilities.bearish, "bearish"))
        ordered = sorted(directions, key=lambda item: (item[0], priority[item[1]]), reverse=True)
        from tradehelper_v2.contracts import ForecastDirection
        direction = ForecastDirection(ordered[0][1]); margin = float(ordered[0][0] - ordered[1][0])
        input_hash = model_input_hash(request.feature_snapshot, request.feature_snapshot.latest_bar_date, feature_set_id)
        key = "|".join((request.feature_snapshot.instrument.stable_key, request.feature_snapshot.latest_bar_date.isoformat(), target.isoformat(), str(horizon), version, input_hash))
        values = {item.name: item for item in request.feature_snapshot.values}
        volatility = values.get("closed.realized_vol_20")
        if volatility is None or volatility.status.value != "available" or not isinstance(volatility.value, (float, int)):
            return self._unavailable(request, horizon, ForecastAvailability.DATA_BLOCKED, "origin volatility unavailable for label policy", target)
        band = flat_band(float(volatility.value), horizon)
        return ForecastResult(request.feature_snapshot.instrument, request.feature_snapshot.cutoff_at, request.feature_snapshot.latest_bar_date, target, horizon, request.reference_bar.close, ForecastAvailability.AVAILABLE, probabilities, distribution, direction, margin, scope, scope_key, family, version, lifecycle, status, execution_eligible, feature_set_id, FEATURE_SET_VERSION, input_hash, training_hash, sample_count, oof_count, tuple(drivers), type(self.calendar).__name__, None, key, datetime.now(timezone.utc), label_flat_band=band)

    def _unavailable(self, request, horizon, availability, reason, target=None):
        """发行带明确原因的不可用结果，不把样本不足伪装成中性预测。"""
        input_hash = model_input_hash(request.feature_snapshot, request.feature_snapshot.latest_bar_date, "tech")
        target_identity = target.isoformat() if target is not None else "calendar-unavailable"
        version = f"unavailable-h{horizon}"; key = "|".join((request.feature_snapshot.instrument.stable_key, request.feature_snapshot.latest_bar_date.isoformat(), target_identity, str(horizon), version, input_hash))
        return ForecastResult(request.feature_snapshot.instrument, request.feature_snapshot.cutoff_at, request.feature_snapshot.latest_bar_date, target, horizon, request.reference_bar.close, availability, None, None, None, None, ForecastScope.BASELINE, request.feature_snapshot.instrument.stable_key, ModelFamily.EMPIRICAL, version, ModelLifecycle.CANDIDATE, ValidationStatus.INSUFFICIENT_SAMPLE, False, "tech", FEATURE_SET_VERSION, input_hash, None, 0, 0, (), type(self.calendar).__name__, reason, key, datetime.now(timezone.utc))
