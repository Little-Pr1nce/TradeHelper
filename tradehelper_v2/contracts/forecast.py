"""V2-3 预测层的不可变合同。

这里定义的对象是预测层与后续情景层之间唯一允许传递的事实。
它刻意不包含买卖动作、仓位或止损：预测只能描述未来交易日的
方向概率和收益分布，不能越过架构边界直接形成交易建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import math
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping

from .analysis import FeatureEvidenceMode, FeatureSnapshot
from .enums import AdjustmentMode, DecisionMode, Market, QualityStatus
from .market_data import CanonicalBar, ContractViolation, InstrumentId, ensure_finite, ensure_utc
from .quality import DataQualityReport


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ForecastDirection(_StringEnum):
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class ForecastAvailability(_StringEnum):
    AVAILABLE = "available"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    DATA_BLOCKED = "data_blocked"
    CALENDAR_UNAVAILABLE = "calendar_unavailable"
    NO_ELIGIBLE_MODEL = "no_eligible_model"


class ForecastScope(_StringEnum):
    STOCK = "stock"
    INDUSTRY = "industry"
    MARKET = "market"
    BASELINE = "baseline"


class ModelFamily(_StringEnum):
    EMPIRICAL = "empirical"
    ANALOG = "analog"
    MULTINOMIAL_LOGISTIC = "multinomial_logistic"
    PROBABILITY_TREE = "probability_tree"
    ENSEMBLE = "ensemble"
    REGIME_ANALOG = "regime_analog"


class ModelLifecycle(_StringEnum):
    CANDIDATE = "candidate"
    CHALLENGER = "challenger"
    CHAMPION = "champion"
    DRIFTED = "drifted"
    RETIRED = "retired"


class ValidationStatus(_StringEnum):
    NOT_EVALUATED = "not_evaluated"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    EVALUATED_NOT_BETTER = "evaluated_not_better"
    CALIBRATION_FAILED = "calibration_failed"
    SELECTION_PASSED = "selection_passed"
    CONFIRMATION_PASSED = "confirmation_passed"
    DRIFTED = "drifted"


def _coerce(enum_type: type[_StringEnum], value: _StringEnum | str, field_name: str) -> _StringEnum:
    try:
        return value if isinstance(value, enum_type) else enum_type(str(value))
    except ValueError as exc:
        raise ContractViolation(f"unsupported {field_name}: {value}") from exc


def _finite(value: float, name: str) -> float:
    return ensure_finite(value, name)


@dataclass(frozen=True, slots=True)
class DirectionProbabilities:
    """上涨、震荡、下跌三分类概率；构造时拒绝自动归一化。

    拒绝“帮调用方修正”是有意为之：概率和错误通常意味着模型或
    artifact 损坏，悄悄归一化会掩盖可审计的预测质量问题。
    """
    bullish: float
    neutral: float
    bearish: float

    def __post_init__(self) -> None:
        values = tuple(_finite(item, "forecast probability") for item in (self.bullish, self.neutral, self.bearish))
        if any(item < 0.0 or item > 1.0 for item in values) or abs(sum(values) - 1.0) > 1e-9:
            raise ContractViolation("direction probabilities must be finite and sum to one")
        object.__setattr__(self, "bullish", values[0]); object.__setattr__(self, "neutral", values[1]); object.__setattr__(self, "bearish", values[2])

    def for_direction(self, direction: ForecastDirection) -> float:
        return {ForecastDirection.BULLISH: self.bullish, ForecastDirection.NEUTRAL: self.neutral, ForecastDirection.BEARISH: self.bearish}[direction]

    def to_dict(self) -> dict[str, float]:
        return {"bullish": self.bullish, "neutral": self.neutral, "bearish": self.bearish}


@dataclass(frozen=True, slots=True)
class ReturnDistribution:
    """同一预测的 P10/P50/P90 收益区间及其生成方法。"""
    p10: float
    p50: float
    p90: float
    method: str

    def __post_init__(self) -> None:
        values = tuple(_finite(item, "return distribution value") for item in (self.p10, self.p50, self.p90))
        if values[0] > values[1] or values[1] > values[2]:
            raise ContractViolation("return distribution must be ordered")
        if self.method not in {"empirical", "analog_weighted", "class_mixture", "ensemble_mixture"}:
            raise ContractViolation("unsupported return distribution method")
        object.__setattr__(self, "p10", values[0]); object.__setattr__(self, "p50", values[1]); object.__setattr__(self, "p90", values[2])


@dataclass(frozen=True, slots=True)
class ForecastDriver:
    """局部替换法得到的解释项，不代表因果关系或交易理由。"""
    feature_name: str
    observed_value: float
    winning_probability_effect: float
    direction: ForecastDirection
    rank: int

    def __post_init__(self) -> None:
        if not self.feature_name or self.rank < 1:
            raise ContractViolation("forecast driver requires feature name and positive rank")
        object.__setattr__(self, "observed_value", _finite(self.observed_value, "driver observed value"))
        object.__setattr__(self, "winning_probability_effect", _finite(self.winning_probability_effect, "driver effect"))
        object.__setattr__(self, "direction", _coerce(ForecastDirection, self.direction, "driver direction"))


@dataclass(frozen=True, slots=True)
class ForecastRequest:
    """对一个已完成交易日特征快照提出的预测请求。

    ``requested_at`` 只记录请求发生时间，绝不能参与模型输入；因此
    盘前、盘中重复使用同一 EOD 快照时，必须得到相同预测身份。
    """
    feature_snapshot: FeatureSnapshot
    reference_bar: CanonicalBar
    requested_at: datetime
    horizons: tuple[int, ...] = (1, 3, 5, 10)
    data_quality: DataQualityReport | None = None

    def __post_init__(self) -> None:
        if self.reference_bar.instrument != self.feature_snapshot.instrument or self.reference_bar.trading_date != self.feature_snapshot.latest_bar_date:
            raise ContractViolation("forecast reference bar must match feature snapshot")
        if self.reference_bar.adjustment_mode is not AdjustmentMode.FRONT_ADJUSTED:
            raise ContractViolation("forecast reference bar must be front adjusted")
        if self.feature_snapshot.mode is not DecisionMode.EOD:
            raise ContractViolation("forecast requests require EOD feature snapshots")
        selected = tuple(sorted(set(self.horizons)))
        if not selected or any(item not in {1, 3, 5, 10} for item in selected):
            raise ContractViolation("forecast horizons must be selected from 1, 3, 5, 10")
        object.__setattr__(self, "horizons", selected)
        object.__setattr__(self, "requested_at", ensure_utc(self.requested_at, "forecast requested_at"))

    @property
    def data_blocked(self) -> bool:
        return self.data_quality is not None and self.data_quality.status is QualityStatus.BLOCKED


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """预注册候选模型的静态定义，防止在 confirmation 段临时调参。"""
    spec_id: str
    family: ModelFamily
    feature_set_id: str
    hyperparameters: Mapping[str, int | float | str | bool]
    primary_metric: str = "multiclass_brier"
    label_policy_version: str = "direction_v1_vol_scaled"
    preprocessing_version: str = "robust_missing_v1"
    complexity_rank: int = 0

    def __post_init__(self) -> None:
        if not self.spec_id or not self.feature_set_id or self.primary_metric != "multiclass_brier" or self.label_policy_version != "direction_v1_vol_scaled" or self.preprocessing_version != "robust_missing_v1" or self.complexity_rank < 0:
            raise ContractViolation("invalid forecast model spec")
        object.__setattr__(self, "family", _coerce(ModelFamily, self.family, "model family"))
        object.__setattr__(self, "hyperparameters", MappingProxyType(dict(sorted(self.hyperparameters.items()))))


@dataclass(frozen=True, slots=True)
class ForecastModelVersion:
    """可复现模型版本及其安全 artifact。

    artifact 必须是 ``json+zlib-v1``，哈希直接对压缩字节计算；这一
    约束禁止 pickle/joblib 一类会在加载时执行任意代码的格式。
    """
    version: str
    scope: ForecastScope
    scope_key: str
    market: Market
    horizon: int
    spec: ModelSpec
    lifecycle: ModelLifecycle
    validation_status: ValidationStatus
    training_start: date
    training_end: date
    selection_start: date | None
    selection_end: date | None
    confirmation_start: date | None
    confirmation_end: date | None
    training_data_hash: str
    artifact_format: str
    artifact_hash: str
    artifact: bytes
    random_seed: int
    sample_count: int
    oof_sample_count: int
    created_at: datetime
    promoted_at: datetime | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        hex_digits = frozenset("0123456789abcdef")
        if not self.version or not self.scope_key or self.horizon not in {1, 3, 5, 10} or self.artifact_format != "json+zlib-v1" or len(self.artifact_hash) != 64 or not set(self.artifact_hash).issubset(hex_digits) or len(self.training_data_hash) != 64 or not set(self.training_data_hash).issubset(hex_digits) or not self.artifact or self.sample_count < 0 or self.oof_sample_count < 0:
            raise ContractViolation("invalid forecast model version")
        if sha256(self.artifact).hexdigest() != self.artifact_hash:
            raise ContractViolation("forecast model artifact hash does not match artifact bytes")
        object.__setattr__(self, "scope", _coerce(ForecastScope, self.scope, "forecast scope"))
        object.__setattr__(self, "market", self.market if isinstance(self.market, Market) else Market(str(self.market)))
        lifecycle = _coerce(ModelLifecycle, self.lifecycle, "model lifecycle")
        validation_status = _coerce(ValidationStatus, self.validation_status, "validation status")
        if lifecycle is ModelLifecycle.CHAMPION and validation_status is not ValidationStatus.CONFIRMATION_PASSED:
            raise ContractViolation("champion model version requires confirmation-passed validation")
        object.__setattr__(self, "lifecycle", lifecycle)
        object.__setattr__(self, "validation_status", validation_status)
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, "model created_at"))
        object.__setattr__(self, "promoted_at", ensure_utc(self.promoted_at, "model promoted_at") if self.promoted_at else None)


@dataclass(frozen=True, slots=True)
class ForecastTrainingSample:
    """一个已到期的监督学习样本，保留 origin 时真正可见的特征。"""
    instrument: InstrumentId
    scope_membership: Mapping[ForecastScope, str]
    origin_session_date: date
    target_session_date: date
    horizon: int
    reference_price: float
    target_price: float
    future_return: float
    flat_band: float
    direction: ForecastDirection
    feature_snapshot: FeatureSnapshot
    feature_hash: str
    evidence_mode: FeatureEvidenceMode
    matured_at: date
    scope_membership_available_at: Mapping[ForecastScope, datetime] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.horizon not in {1, 3, 5, 10} or self.target_session_date <= self.origin_session_date or self.matured_at != self.target_session_date:
            raise ContractViolation("invalid forecast training sample dates")
        if self.feature_snapshot.instrument != self.instrument or self.feature_snapshot.latest_bar_date != self.origin_session_date or self.feature_hash != self.feature_snapshot.feature_hash:
            raise ContractViolation("training sample feature snapshot does not match origin")
        if self.feature_snapshot.mode is not DecisionMode.EOD:
            raise ContractViolation("forecast training samples require EOD feature snapshots")
        evidence_mode = _coerce(FeatureEvidenceMode, self.evidence_mode, "evidence mode")
        if evidence_mode is not self.feature_snapshot.evidence_mode:
            raise ContractViolation("training sample evidence mode must match feature snapshot")
        if any(value.available_at > self.feature_snapshot.cutoff_at for value in self.feature_snapshot.values):
            raise ContractViolation("training sample contains features unavailable at its cutoff")
        membership = {(_coerce(ForecastScope, scope, "scope membership")): str(key) for scope, key in self.scope_membership.items()}
        if membership.get(ForecastScope.STOCK) != self.instrument.stable_key:
            raise ContractViolation("training sample requires stock scope membership")
        membership_times = {
            _coerce(ForecastScope, scope, "scope membership timestamp"): ensure_utc(value, "scope membership available_at")
            for scope, value in self.scope_membership_available_at.items()
        }
        for scope in membership:
            if scope in {ForecastScope.INDUSTRY, ForecastScope.MARKET} and (
                scope not in membership_times or membership_times[scope] > self.feature_snapshot.cutoff_at
            ):
                raise ContractViolation("cross-stock scope membership requires point-in-time evidence")
        object.__setattr__(self, "scope_membership", MappingProxyType(membership))
        object.__setattr__(self, "scope_membership_available_at", MappingProxyType(membership_times))
        object.__setattr__(self, "reference_price", _finite(self.reference_price, "reference price"))
        object.__setattr__(self, "target_price", _finite(self.target_price, "target price"))
        object.__setattr__(self, "future_return", _finite(self.future_return, "future return"))
        object.__setattr__(self, "flat_band", _finite(self.flat_band, "flat band"))
        object.__setattr__(self, "direction", _coerce(ForecastDirection, self.direction, "training direction"))
        object.__setattr__(self, "evidence_mode", evidence_mode)
        if self.reference_price <= 0 or self.target_price <= 0 or self.flat_band < 0:
            raise ContractViolation("training prices must be positive and flat band non-negative")
        expected_return = self.target_price / self.reference_price - 1.0
        if abs(self.future_return - expected_return) > 1e-12:
            raise ContractViolation("training future return is inconsistent with prices")
        expected_direction = ForecastDirection.BULLISH if self.future_return > self.flat_band else ForecastDirection.BEARISH if self.future_return < -self.flat_band else ForecastDirection.NEUTRAL
        if self.direction is not expected_direction:
            raise ContractViolation("training direction is inconsistent with return and flat band")


@dataclass(frozen=True, slots=True)
class ForecastResult:
    """一次已经发行的预测事实，而非可执行交易指令。

    ``event_key`` 将标的、起止交易日、周期、模型版本和原始输入哈希
    绑定，既支持幂等保存，也能阻止同一历史事实被悄悄覆盖。
    """
    instrument: InstrumentId
    cutoff_at: datetime
    origin_session_date: date
    target_session_date: date | None
    horizon: int
    reference_price: float
    availability: ForecastAvailability
    probabilities: DirectionProbabilities | None
    return_distribution: ReturnDistribution | None
    direction: ForecastDirection | None
    confidence_margin: float | None
    model_scope: ForecastScope
    scope_key: str
    model_family: ModelFamily
    model_version: str
    lifecycle: ModelLifecycle
    validation_status: ValidationStatus
    execution_eligible: bool
    feature_set_id: str
    feature_set_version: str
    model_input_hash: str
    training_data_hash: str | None
    sample_count: int
    oof_sample_count: int
    drivers: tuple[ForecastDriver, ...]
    calendar_source: str
    reason: str | None
    event_key: str
    generated_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.horizon not in {1, 3, 5, 10} or self.sample_count < 0 or self.oof_sample_count < 0 or not self.scope_key or not self.model_version or len(self.model_input_hash) != 64:
            raise ContractViolation("invalid forecast result identity")
        availability = _coerce(ForecastAvailability, self.availability, "forecast availability")
        scope = _coerce(ForecastScope, self.model_scope, "forecast scope")
        family = _coerce(ModelFamily, self.model_family, "model family")
        lifecycle = _coerce(ModelLifecycle, self.lifecycle, "model lifecycle")
        validation = _coerce(ValidationStatus, self.validation_status, "validation status")
        direction = _coerce(ForecastDirection, self.direction, "forecast direction") if self.direction is not None else None
        if availability is ForecastAvailability.AVAILABLE:
            if self.target_session_date is None or self.target_session_date <= self.origin_session_date:
                raise ContractViolation("available forecast requires a future target session")
            if self.probabilities is None or self.return_distribution is None or direction is None or self.confidence_margin is None:
                raise ContractViolation("available forecast requires probabilities, interval, direction and confidence")
            ordered = sorted(((self.probabilities.bullish, ForecastDirection.BULLISH), (self.probabilities.neutral, ForecastDirection.NEUTRAL), (self.probabilities.bearish, ForecastDirection.BEARISH)), key=lambda item: (item[0], {ForecastDirection.BULLISH: 0, ForecastDirection.BEARISH: 1, ForecastDirection.NEUTRAL: 2}[item[1]]), reverse=True)
            expected = ordered[0][1]
            margin = ordered[0][0] - ordered[1][0]
            if direction is not expected or abs(float(self.confidence_margin) - margin) > 1e-9:
                raise ContractViolation("forecast direction or confidence margin is inconsistent")
        elif any(item is not None for item in (self.probabilities, self.return_distribution, direction, self.confidence_margin)):
            raise ContractViolation("unavailable forecast must not contain probabilistic output")
        if availability is ForecastAvailability.CALENDAR_UNAVAILABLE and self.target_session_date is not None:
            raise ContractViolation("calendar-unavailable forecast cannot invent a target session")
        if self.target_session_date is not None and self.target_session_date <= self.origin_session_date:
            raise ContractViolation("forecast target session must follow origin")
        if self.execution_eligible and not (lifecycle is ModelLifecycle.CHAMPION and validation is ValidationStatus.CONFIRMATION_PASSED):
            raise ContractViolation("only confirmed champion forecasts can be execution eligible")
        target_identity = self.target_session_date.isoformat() if self.target_session_date is not None else "calendar-unavailable"
        expected_key = "|".join((self.instrument.stable_key, self.origin_session_date.isoformat(), target_identity, str(self.horizon), self.model_version, self.model_input_hash))
        if self.event_key != expected_key:
            raise ContractViolation("forecast event key does not match identity")
        if tuple(sorted(self.drivers, key=lambda driver: driver.rank)) != self.drivers or len(self.drivers) > 5:
            raise ContractViolation("forecast drivers must be ordered and limited to five")
        object.__setattr__(self, "reference_price", _finite(self.reference_price, "forecast reference price"))
        object.__setattr__(self, "cutoff_at", ensure_utc(self.cutoff_at, "forecast cutoff_at"))
        object.__setattr__(self, "generated_at", ensure_utc(self.generated_at, "forecast generated_at"))
        object.__setattr__(self, "availability", availability); object.__setattr__(self, "model_scope", scope); object.__setattr__(self, "model_family", family); object.__setattr__(self, "lifecycle", lifecycle); object.__setattr__(self, "validation_status", validation); object.__setattr__(self, "direction", direction)
