"""可确定复现的预测模型族，以及只含 JSON 的安全 artifact。

训练阶段可借助 sklearn 求解参数，但会立即转换成数组和基础类型；推理
阶段只解释这些基础数据，永不反序列化 pickle/joblib，避免模型文件
成为代码执行入口。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from hashlib import sha256
from typing import Iterable
import warnings
import zlib

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.exceptions import ConvergenceWarning
from sklearn.tree import DecisionTreeClassifier

from tradehelper_v2.contracts import (
    DirectionProbabilities, ForecastDirection, ForecastTrainingSample, ModelFamily, ModelSpec,
    ReturnDistribution,
)

from .feature_sets import extract_feature_row
from .preprocessing import RobustMissingPreprocessor

_DIRECTIONS = (ForecastDirection.BULLISH, ForecastDirection.NEUTRAL, ForecastDirection.BEARISH)


def _probabilities(values: Iterable[float]) -> DirectionProbabilities:
    """将内部数值安全投影为合同要求的三分类概率。"""
    clipped = np.maximum(np.asarray(tuple(values), dtype=float), 0.0)
    total = float(clipped.sum())
    if not math.isfinite(total) or total <= 0:
        clipped = np.full(3, 1.0 / 3.0)
    else:
        clipped /= total
    # Make the final sum exact within the frozen contract tolerance.
    clipped[2] = 1.0 - float(clipped[0] + clipped[1])
    return DirectionProbabilities(*map(float, clipped))


def _quantile(values: np.ndarray, quantiles: tuple[float, ...], weights: np.ndarray | None = None) -> tuple[float, ...]:
    if not len(values):
        return tuple(0.0 for _ in quantiles)
    if weights is None:
        return tuple(float(np.quantile(values, item)) for item in quantiles)
    order = np.argsort(values); ordered = values[order]; weights = weights[order]
    cumulative = np.cumsum(weights) / float(weights.sum())
    return tuple(float(ordered[min(np.searchsorted(cumulative, item, side="left"), len(ordered) - 1)]) for item in quantiles)


def _distribution(values: Iterable[float], method: str, weights: np.ndarray | None = None) -> ReturnDistribution:
    """用历史/邻居收益构造 P10、P50、P90，而非伪造精确收益点。"""
    p10, p50, p90 = _quantile(np.asarray(tuple(values), dtype=float), (0.1, 0.5, 0.9), weights)
    return ReturnDistribution(p10, p50, p90, method)


def _label_index(label: ForecastDirection) -> int:
    return _DIRECTIONS.index(label)


def _laplace(labels: Iterable[ForecastDirection]) -> DirectionProbabilities:
    counts = np.ones(3, dtype=float)
    for label in labels:
        counts[_label_index(label)] += 1.0
    return _probabilities(counts)


@dataclass(frozen=True, slots=True)
class TrainedForecastModel:
    """已拟合模型的内存表示；生命周期证据由 ModelVersion 单独保存。"""
    spec: ModelSpec
    preprocessor: RobustMissingPreprocessor | None
    payload: dict
    training_returns: tuple[float, ...]
    training_labels: tuple[ForecastDirection, ...]
    temperature: float = 1.0

    def to_payload(self) -> dict:
        return {
            "artifact_version": 1, "spec_id": self.spec.spec_id, "family": self.spec.family.value,
            "feature_set_id": self.spec.feature_set_id, "hyperparameters": dict(self.spec.hyperparameters),
            "preprocessor": self.preprocessor.to_dict() if self.preprocessor else None,
            "payload": self.payload, "training_returns": self.training_returns,
            "training_labels": [item.value for item in self.training_labels],
            "temperature": self.temperature,
        }

    def artifact_bytes(self) -> bytes:
        """稳定 JSON 键序与固定压缩级别保证相同训练可复现字节。"""
        return zlib.compress(json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8"), level=9)

    @property
    def artifact_hash(self) -> str:
        return sha256(self.artifact_bytes()).hexdigest()


def model_from_artifact(spec: ModelSpec, artifact: bytes) -> TrainedForecastModel:
    """安全加载 artifact；格式、版本或 spec 不一致即拒绝。"""
    payload = json.loads(zlib.decompress(artifact).decode("utf-8"))
    if (
        payload.get("artifact_version") != 1
        or payload.get("spec_id") != spec.spec_id
        or payload.get("family") != spec.family.value
        or payload.get("feature_set_id") != spec.feature_set_id
        or payload.get("hyperparameters") != dict(spec.hyperparameters)
    ):
        raise ValueError("unsupported or mismatched forecast artifact")
    return TrainedForecastModel(
        spec=spec,
        preprocessor=RobustMissingPreprocessor.from_dict(payload["preprocessor"]) if payload.get("preprocessor") else None,
        payload=dict(payload["payload"]), training_returns=tuple(float(item) for item in payload["training_returns"]),
        training_labels=tuple(ForecastDirection(item) for item in payload["training_labels"]),
        temperature=float(payload.get("temperature", 1.0)),
    )


def _rows(samples: tuple[ForecastTrainingSample, ...], feature_set_id: str) -> tuple[tuple[str, ...], tuple[tuple[float | None, ...], ...]]:
    if not samples:
        raise ValueError("training samples cannot be empty")
    names, first = extract_feature_row(samples[0].feature_snapshot, feature_set_id)
    rows = [first]
    for sample in samples[1:]:
        current_names, row = extract_feature_row(sample.feature_snapshot, feature_set_id)
        if current_names != names:
            raise ValueError("feature columns must be stable within a model fit")
        rows.append(row)
    return names, tuple(rows)


class InsufficientRegimeSamples(ValueError):
    """当前技术状态缺少至少 30 条同状态成熟样本。"""


def _regime_payload(names: tuple[str, ...], raw: tuple[tuple[float | None, ...], ...]) -> dict | None:
    try:
        trend_index = names.index("closed.ma_distance_20")
        volatility_index = names.index("closed.realized_vol_20")
    except ValueError:
        return None
    volatility = np.asarray([row[volatility_index] for row in raw if row[volatility_index] is not None], dtype=float)
    if len(volatility) != len(raw):
        return None
    q33, q67 = (float(value) for value in np.quantile(volatility, (1.0 / 3.0, 2.0 / 3.0)))
    regimes: list[str] = []
    for row in raw:
        trend = row[trend_index]
        vol = row[volatility_index]
        if trend is None or vol is None:
            return None
        bucket = "low" if vol <= q33 else "mid" if vol <= q67 else "high"
        regimes.append(f"{'up' if trend > 0 else 'down_or_flat'}:{bucket}")
    return {
        "trend_index": trend_index,
        "volatility_index": volatility_index,
        "q33": q33,
        "q67": q67,
        "regimes": regimes,
    }


def _row_regime(row: tuple[float | None, ...], payload: dict) -> str | None:
    trend = row[int(payload["trend_index"])]
    volatility = row[int(payload["volatility_index"])]
    if trend is None or volatility is None:
        return None
    bucket = "low" if volatility <= float(payload["q33"]) else "mid" if volatility <= float(payload["q67"]) else "high"
    return f"{'up' if trend > 0 else 'down_or_flat'}:{bucket}"


def fit_model(spec: ModelSpec, samples: tuple[ForecastTrainingSample, ...], *, random_seed: int = 20260714) -> TrainedForecastModel | None:
    """Fit a candidate using only the supplied (fold-local) mature samples."""
    labels = tuple(sample.direction for sample in samples)
    returns = tuple(sample.future_return for sample in samples)
    if spec.family is ModelFamily.EMPIRICAL:
        # 基线同样使用 Laplace 平滑，防止小样本把某个方向概率写成零。
        return TrainedForecastModel(spec, None, {"probabilities": _laplace(labels).to_dict()}, returns, labels)
    names, raw = _rows(samples, spec.feature_set_id)
    preprocessor = RobustMissingPreprocessor.fit(names, raw)
    if preprocessor is None:
        return None
    matrix = preprocessor.transform(raw)
    y = np.asarray([_label_index(label) for label in labels], dtype=int)
    if len(set(y)) < 2:
        return None
    family = spec.family
    if family in {ModelFamily.ANALOG, ModelFamily.REGIME_ANALOG}:
        # 邻居模型保存训练折的已缩放矩阵；距离权重在推理时才计算。
        k = int(spec.hyperparameters.get("k", 40))
        payload = {"k": k, "matrix": matrix.tolist(), "labels": y.tolist(), "returns": list(returns)}
        if family is ModelFamily.REGIME_ANALOG:
            regime = _regime_payload(names, raw)
            if regime is None:
                return None
            payload["regime"] = regime
    elif family is ModelFamily.MULTINOMIAL_LOGISTIC:
        # 只保存系数/截距/类别索引，推理时自行 softmax，不保存 sklearn 对象。
        estimator = LogisticRegression(C=float(spec.hyperparameters.get("C", 1.0)), max_iter=1000, random_state=random_seed)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            try:
                estimator.fit(matrix, y)
            except ConvergenceWarning:
                return None
        payload = {"classes": estimator.classes_.tolist(), "coef": estimator.coef_.tolist(), "intercept": estimator.intercept_.tolist()}
    elif family is ModelFamily.PROBABILITY_TREE:
        # 树被拍平成节点数组，避免 joblib 反序列化并保留节点可审计性。
        min_leaf = max(15, int(math.ceil(len(samples) * 0.02)))
        estimator = DecisionTreeClassifier(max_depth=int(spec.hyperparameters.get("max_depth", 2)), min_samples_leaf=min_leaf, random_state=random_seed)
        estimator.fit(matrix, y)
        tree = estimator.tree_
        payload = {"classes": estimator.classes_.tolist(), "children_left": tree.children_left.tolist(), "children_right": tree.children_right.tolist(), "feature": tree.feature.tolist(), "threshold": tree.threshold.tolist(), "value": tree.value[:, 0, :].tolist()}
    elif family is ModelFamily.ENSEMBLE:
        # 集成权重是预注册常量，不能根据 confirmation 结果再调权重。
        analog_spec = ModelSpec(spec.spec_id + ":analog", ModelFamily.ANALOG, spec.feature_set_id, {"k": 80}, complexity_rank=spec.complexity_rank)
        logistic_spec = ModelSpec(spec.spec_id + ":logistic", ModelFamily.MULTINOMIAL_LOGISTIC, spec.feature_set_id, {"C": 0.1}, complexity_rank=spec.complexity_rank)
        analog = fit_model(analog_spec, samples, random_seed=random_seed)
        logistic = fit_model(logistic_spec, samples, random_seed=random_seed)
        if analog is None or logistic is None:
            return None
        payload = {"analog": analog.to_payload(), "logistic": logistic.to_payload(), "weight": 0.5}
    else:
        return None
    return TrainedForecastModel(spec, preprocessor, payload, returns, labels)


def fit_calibrated_model(
    spec: ModelSpec, samples: tuple[ForecastTrainingSample, ...], *, random_seed: int = 20260714,
) -> TrainedForecastModel | None:
    """按时间保留最后 20% 成熟样本校准概率，并把温度固化进 artifact。"""
    if spec.family is ModelFamily.EMPIRICAL:
        return fit_model(spec, samples, random_seed=random_seed)
    calibration_count = int(math.ceil(len(samples) * 0.20))
    if calibration_count < 30 or len(samples) - calibration_count < 30:
        return fit_model(spec, samples, random_seed=random_seed)
    training = samples[:-calibration_count]
    calibration = samples[-calibration_count:]
    model = fit_model(spec, training, random_seed=random_seed)
    if model is None:
        return None
    raw_probabilities: list[DirectionProbabilities] = []
    labels: list[ForecastDirection] = []
    for sample in calibration:
        try:
            probability, _ = predict_model(model, sample.feature_snapshot)
        except InsufficientRegimeSamples:
            return None
        raw_probabilities.append(probability)
        labels.append(sample.direction)
    from .diagnostics import fit_temperature
    temperature = fit_temperature(tuple(raw_probabilities), tuple(labels))
    return replace(
        model,
        training_returns=tuple(sample.future_return for sample in samples),
        training_labels=tuple(sample.direction for sample in samples),
        temperature=temperature,
    )


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - values.max(); exp = np.exp(shifted); return exp / exp.sum()


def _calibrated(model: TrainedForecastModel, probabilities: DirectionProbabilities) -> DirectionProbabilities:
    if model.temperature == 1.0:
        return probabilities
    from .diagnostics import apply_temperature
    return apply_temperature(probabilities, model.temperature)


def _class_mixture_distribution(model: TrainedForecastModel, probabilities: DirectionProbabilities) -> ReturnDistribution:
    labels = np.asarray([_label_index(label) for label in model.training_labels], dtype=int)
    returns = np.asarray(model.training_returns, dtype=float)
    counts = np.bincount(labels, minlength=3)
    if len(returns) == 0 or np.any(counts == 0):
        return _distribution(returns, "class_mixture")
    class_probabilities = np.asarray((probabilities.bullish, probabilities.neutral, probabilities.bearish), dtype=float)
    weights = np.asarray([class_probabilities[label] / counts[label] for label in labels], dtype=float)
    return _distribution(returns, "class_mixture", weights)


def _model_probabilities(model: TrainedForecastModel, row: tuple[float | None, ...]) -> tuple[DirectionProbabilities, ReturnDistribution]:
    """用 artifact 中的纯数据执行一次预测，返回概率及同源收益分布。"""
    family = model.spec.family
    if family is ModelFamily.EMPIRICAL:
        probs = _calibrated(model, DirectionProbabilities(**model.payload["probabilities"]))
        return probs, _distribution(model.training_returns, "empirical")
    assert model.preprocessor is not None
    vector = model.preprocessor.transform((row,))[0]
    if family in {ModelFamily.ANALOG, ModelFamily.REGIME_ANALOG}:
        matrix = np.asarray(model.payload["matrix"], dtype=float); distances = np.linalg.norm(matrix - vector, axis=1)
        eligible = np.arange(len(distances))
        if family is ModelFamily.REGIME_ANALOG:
            current_regime = _row_regime(row, model.payload["regime"])
            regimes = np.asarray(model.payload["regime"]["regimes"], dtype=object)
            eligible = np.flatnonzero(regimes == current_regime) if current_regime is not None else np.asarray([], dtype=int)
            if len(eligible) < 30:
                raise InsufficientRegimeSamples("current regime has fewer than 30 mature samples")
        ranked = eligible[np.argsort(distances[eligible])]
        indices = ranked[: min(int(model.payload["k"]), len(ranked))]
        weights = 1.0 / np.maximum(distances[indices], 1e-9)
        labels = np.asarray(model.payload["labels"], dtype=int)[indices]
        counts = np.ones(3, dtype=float)
        for index, weight in zip(labels, weights): counts[index] += weight
        returns = np.asarray(model.payload["returns"], dtype=float)[indices]
        return _calibrated(model, _probabilities(counts)), _distribution(returns, "analog_weighted", weights)
    if family is ModelFamily.MULTINOMIAL_LOGISTIC:
        scores = np.asarray(model.payload["coef"], dtype=float) @ vector + np.asarray(model.payload["intercept"], dtype=float)
        partial = _softmax(scores); values = np.zeros(3, dtype=float)
        for class_index, probability in zip(model.payload["classes"], partial): values[int(class_index)] = probability
        probabilities = _calibrated(model, _probabilities(values))
        return probabilities, _class_mixture_distribution(model, probabilities)
    if family is ModelFamily.PROBABILITY_TREE:
        node = 0
        while int(model.payload["children_left"][node]) != -1:
            node = int(model.payload["children_left"][node] if vector[int(model.payload["feature"][node])] <= float(model.payload["threshold"][node]) else model.payload["children_right"][node])
        values = np.ones(3, dtype=float)
        for class_index, count in zip(model.payload["classes"], model.payload["value"][node]): values[int(class_index)] += float(count)
        probabilities = _calibrated(model, _probabilities(values))
        return probabilities, _class_mixture_distribution(model, probabilities)
    if family is ModelFamily.ENSEMBLE:
        def child(raw: dict) -> TrainedForecastModel:
            return TrainedForecastModel(
                ModelSpec(raw["spec_id"], ModelFamily(raw["family"]), raw["feature_set_id"], raw["hyperparameters"]),
                RobustMissingPreprocessor.from_dict(raw["preprocessor"]) if raw["preprocessor"] else None,
                raw["payload"], tuple(raw["training_returns"]), tuple(ForecastDirection(value) for value in raw["training_labels"]),
                float(raw.get("temperature", 1.0)),
            )
        ap, ad = _model_probabilities(child(model.payload["analog"]), row); lp, ld = _model_probabilities(child(model.payload["logistic"]), row)
        weight = float(model.payload["weight"])
        probs = _calibrated(model, _probabilities(np.asarray((ap.bullish, ap.neutral, ap.bearish)) * weight + np.asarray((lp.bullish, lp.neutral, lp.bearish)) * (1.0 - weight)))
        values = np.asarray((ad.p10, ad.p50, ad.p90)) * weight + np.asarray((ld.p10, ld.p50, ld.p90)) * (1.0 - weight)
        return probs, ReturnDistribution(*map(float, values), method="ensemble_mixture")
    raise ValueError("unsupported model family")


def predict_model(model: TrainedForecastModel, snapshot) -> tuple[DirectionProbabilities, ReturnDistribution]:
    _, row = extract_feature_row(snapshot, model.spec.feature_set_id)
    return _model_probabilities(model, row)


def local_replacement_drivers(model: TrainedForecastModel, snapshot, *, maximum: int = 5):
    """用“替换为训练中位数”的局部扰动排序解释项。

    它只说明当前模型在输入附近依赖哪些字段，不等于因果解释，更不是
    交易条件。
    """
    from tradehelper_v2.contracts import ForecastDriver
    probabilities, _ = predict_model(model, snapshot)
    winning = max(_DIRECTIONS, key=lambda item: (probabilities.for_direction(item), {ForecastDirection.BULLISH: 0, ForecastDirection.BEARISH: 1, ForecastDirection.NEUTRAL: 2}[item]))
    if model.preprocessor is None:
        return ()
    names, row = extract_feature_row(snapshot, model.spec.feature_set_id)
    effects = []
    for active, source_index in enumerate(model.preprocessor.active_indices):
        observed = row[source_index]
        if observed is None:
            continue
        replaced = list(row); replaced[source_index] = model.preprocessor.medians[active]
        alternate, _ = _model_probabilities(model, tuple(replaced))
        effect = probabilities.for_direction(winning) - alternate.for_direction(winning)
        effects.append((abs(effect), names[source_index], float(observed), float(effect)))
    effects.sort(key=lambda value: (-value[0], value[1]))
    return tuple(ForecastDriver(name, observed, effect, winning, rank + 1) for rank, (_, name, observed, effect) in enumerate(effects[:maximum]))
