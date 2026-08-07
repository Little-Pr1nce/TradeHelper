"""按市场、scope、周期隔离的模型注册表与保守回退选择。

行业/市场 Champion 即使能作为信息回退，也不能被标成执行证据；如何将
跨股票预测转化为交易决策留给后续情景与风控层明确处理。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts import (
    ForecastModelVersion, ForecastScope, ForecastTrainingSample, ModelFamily, ModelLifecycle,
    ModelSpec, ValidationStatus,
)

from .models import TrainedForecastModel, fit_calibrated_model, fit_model, model_from_artifact


@dataclass(frozen=True, slots=True)
class RegisteredModel:
    version: ForecastModelVersion
    model: TrainedForecastModel


class ForecastRegistry:
    """内存投影；持久化的一周期唯一 Champion 由 SQLite repository 保证。"""

    def __init__(self) -> None:
        self._models: dict[tuple, RegisteredModel] = {}
        self._validation: dict[tuple, tuple[ValidationStatus, str | None]] = {}

    @staticmethod
    def _key(version: ForecastModelVersion) -> tuple:
        return version.market, version.scope, version.scope_key, version.horizon

    def promote(self, version: ForecastModelVersion, model: TrainedForecastModel | None = None) -> None:
        if version.lifecycle is not ModelLifecycle.CHAMPION or version.validation_status not in {
            ValidationStatus.CONFIRMATION_PASSED, ValidationStatus.NONINFERIOR_PASSED,
        }:
            raise ValueError("only confirmed champion model versions may be registered")
        loaded = model or model_from_artifact(version.spec, version.artifact)
        self._models[self._key(version)] = RegisteredModel(version, loaded)

    def retire(
        self, *, market, scope: ForecastScope, scope_key: str, horizon: int,
        expected_version: str | None = None,
    ) -> RegisteredModel | None:
        """Remove a stale Champion after a definitive fresh OOF failure."""
        key = (market, scope, scope_key, horizon)
        current = self._models.get(key)
        if current is None or (expected_version is not None and current.version.version != expected_version):
            return None
        return self._models.pop(key)

    def restore(self, versions: tuple[ForecastModelVersion, ...]) -> None:
        """从 repository 恢复持久化 Champion；任一损坏 artifact 都明确失败。"""
        for version in versions:
            self.promote(version)

    def champion(self, *, market, scope: ForecastScope, scope_key: str, horizon: int) -> RegisteredModel | None:
        return self._models.get((market, scope, scope_key, horizon))

    def resolve(
        self, *, market, stock_key: str, industry_key: str | None, horizon: int,
    ) -> RegisteredModel | None:
        """按 stock → industry → market 的固定顺序寻找确认 Champion。"""
        candidates = ((ForecastScope.STOCK, stock_key), (ForecastScope.INDUSTRY, industry_key), (ForecastScope.MARKET, market.value))
        for scope, key in candidates:
            if key:
                selected = self.champion(market=market, scope=scope, scope_key=key, horizon=horizon)
                if selected:
                    return selected
        return None

    def record_validation(self, *, market, scope_key: str, horizon: int, status: ValidationStatus, reason: str | None) -> None:
        self._validation[(market, scope_key, horizon)] = (status, reason)

    def last_validation(self, *, market, scope_key: str, horizon: int) -> tuple[ValidationStatus, str | None] | None:
        return self._validation.get((market, scope_key, horizon))

    @staticmethod
    def baseline(
        samples: tuple[ForecastTrainingSample, ...], *, market, stock_key: str, horizon: int,
        origin_session_date,
    ) -> tuple[ForecastScope, str, TrainedForecastModel] | None:
        """无 Champion 时构造满足最低样本量的经验基线。"""
        matured = tuple(item for item in samples if item.target_session_date <= origin_session_date)
        stock = tuple(item for item in matured if item.horizon == horizon and item.scope_membership.get(ForecastScope.STOCK) == stock_key)
        if len(stock) >= 20:
            spec = ModelSpec("empirical-stock-baseline", ModelFamily.EMPIRICAL, "tech", {})
            return ForecastScope.STOCK, stock_key, fit_calibrated_model(spec, stock)  # type: ignore[return-value]
        market_samples = tuple(item for item in matured if item.horizon == horizon and item.instrument.market == market)
        if len(market_samples) >= 100 and len({item.instrument.stable_key for item in market_samples}) >= 5:
            spec = ModelSpec("empirical-market-baseline", ModelFamily.EMPIRICAL, "tech", {})
            return ForecastScope.MARKET, market.value, fit_calibrated_model(spec, market_samples)  # type: ignore[return-value]
        return None
