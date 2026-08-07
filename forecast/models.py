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
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.exceptions import ConvergenceWarning
from sklearn.tree import DecisionTreeClassifier

from contracts import (
    DirectionProbabilities, ForecastDirection, ForecastTrainingSample, ModelFamily, ModelSpec,
    ReturnDistribution,
)

from .feature_sets import extract_feature_row
from .preprocessing import RobustMissingPreprocessor

_DIRECTIONS = (ForecastDirection.BULLISH, ForecastDirection.NEUTRAL, ForecastDirection.BEARISH)


def _windowed_samples(
    spec: ModelSpec, samples: tuple[ForecastTrainingSample, ...]
) -> tuple[ForecastTrainingSample, ...]:
    """Apply a preregistered rolling training window without touching test data."""
    raw = spec.hyperparameters.get("training_window")
    if raw is None:
        return samples
    window = int(raw)
    if window < 80:
        raise ValueError("forecast training_window must be at least 80 samples")
    return samples[-window:]


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


def with_stock_return_history(
    model: TrainedForecastModel, samples: tuple[ForecastTrainingSample, ...],
) -> TrainedForecastModel:
    """Keep fitted direction parameters while refreshing stock return history."""
    return replace(
        model,
        training_returns=tuple(sample.future_return for sample in samples),
        training_labels=tuple(sample.direction for sample in samples),
    )


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


def _tree_payload(estimator: DecisionTreeClassifier) -> dict:
    tree = estimator.tree_
    return {
        "children_left": tree.children_left.tolist(),
        "children_right": tree.children_right.tolist(),
        "feature": tree.feature.tolist(),
        "threshold": tree.threshold.tolist(),
        "value": tree.value[:, 0, :].tolist(),
    }


def _tree_leaf_values(payload: dict, vector: np.ndarray) -> list[float]:
    node = 0
    while int(payload["children_left"][node]) != -1:
        feature = int(payload["feature"][node])
        node = int(
            payload["children_left"][node]
            if vector[feature] <= float(payload["threshold"][node])
            else payload["children_right"][node]
        )
    return [float(value) for value in payload["value"][node]]


def fit_model(spec: ModelSpec, samples: tuple[ForecastTrainingSample, ...], *, random_seed: int = 20260714) -> TrainedForecastModel | None:
    """Fit a candidate using only the supplied (fold-local) mature samples."""
    samples = _windowed_samples(spec, samples)
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
        payload = {"classes": estimator.classes_.tolist(), **_tree_payload(estimator)}
    elif family is ModelFamily.PROBABILITY_FOREST:
        # ExtraTrees supplies nonlinear interactions while remaining bounded:
        # fixed seed, shallow trees and one CPU thread. Each tree is flattened
        # into JSON arrays, preserving the no-pickle artifact contract.
        min_leaf = max(15, int(math.ceil(len(samples) * 0.02)))
        estimator = ExtraTreesClassifier(
            n_estimators=int(spec.hyperparameters.get("n_estimators", 24)),
            max_depth=int(spec.hyperparameters.get("max_depth", 4)),
            min_samples_leaf=min_leaf,
            max_features=float(spec.hyperparameters.get("max_features", 0.7)),
            random_state=random_seed,
            n_jobs=1,
        )
        estimator.fit(matrix, y)
        payload = {
            "classes": estimator.classes_.tolist(),
            "trees": [_tree_payload(tree) for tree in estimator.estimators_],
        }
    elif family is ModelFamily.ENSEMBLE:
        # 集成权重是预注册常量，不能根据 confirmation 结果再调权重。
        child_window = ({"training_window": spec.hyperparameters["training_window"]}
                        if "training_window" in spec.hyperparameters else {})
        analog_spec = ModelSpec(spec.spec_id + ":analog", ModelFamily.ANALOG, spec.feature_set_id, {"k": 80, **child_window}, complexity_rank=spec.complexity_rank)
        logistic_spec = ModelSpec(spec.spec_id + ":logistic", ModelFamily.MULTINOMIAL_LOGISTIC, spec.feature_set_id, {"C": 0.1, **child_window}, complexity_rank=spec.complexity_rank)
        analog = fit_model(analog_spec, samples, random_seed=random_seed)
        logistic = fit_model(logistic_spec, samples, random_seed=random_seed)
        if analog is None or logistic is None:
            return None
        payload = {"analog": analog.to_payload(), "logistic": logistic.to_payload(), "weight": 0.5}
    else:
        return None
    return TrainedForecastModel(spec, preprocessor, payload, returns, labels)


def _calibration_start(samples: tuple[ForecastTrainingSample, ...]) -> int | None:
    """Return a complete-date 20% calibration boundary with 30 points per side."""
    calibration_count = int(math.ceil(len(samples) * 0.20))
    if calibration_count < 30 or len(samples) - calibration_count < 30:
        return None
    start = len(samples) - calibration_count
    boundary = samples[start].origin_session_date
    while start > 0 and samples[start - 1].origin_session_date == boundary:
        start -= 1
    return start if start >= 30 and len(samples) - start >= 30 else None


def _calibrate_fitted_model(
    model: TrainedForecastModel,
    calibration: tuple[ForecastTrainingSample, ...],
    *, prior_samples: tuple[ForecastTrainingSample, ...],
    final_history: tuple[ForecastTrainingSample, ...],
    calibrate_probabilities: bool = True,
) -> TrainedForecastModel | None:
    """Fit probability and interval calibration on a model-untouched time slice."""
    prediction_model = with_stock_return_history(model, prior_samples)
    raw_probabilities: list[DirectionProbabilities] = []
    interval_ratios: list[float] = []
    labels: list[ForecastDirection] = []
    for sample in calibration:
        try:
            probability, interval = predict_model(prediction_model, sample.feature_snapshot)
        except InsufficientRegimeSamples:
            return None
        raw_probabilities.append(probability)
        labels.append(sample.direction)
        observed = float(sample.future_return)
        center = float(interval.p50)
        width = max(center - float(interval.p10), 1e-9) if observed < center else max(float(interval.p90) - center, 1e-9)
        interval_ratios.append(abs(observed - center) / width)
    from .diagnostics import apply_temperature, fit_prior_shrinkage, fit_temperature
    temperature = (
        fit_temperature(tuple(raw_probabilities), tuple(labels))
        if calibrate_probabilities else 1.0
    )
    # Calibrate the nominal P10-P90 interval separately from direction
    # probabilities. The finite-sample "higher" quantile avoids claiming an
    # 80% interval that was too narrow even on its own calibration segment.
    interval_scale = float(np.quantile(np.asarray(interval_ratios), 0.80, method="higher"))
    interval_scale = min(max(interval_scale, 0.50), 3.00)
    payload = dict(model.payload)
    payload["_interval_scale"] = interval_scale
    if calibrate_probabilities:
        temperature_scaled = tuple(apply_temperature(item, temperature) for item in raw_probabilities)
        prior_shrinkage, calibration_prior = fit_prior_shrinkage(
            temperature_scaled, tuple(labels),
            prior_labels=tuple(sample.direction for sample in prior_samples),
        )
        payload["_prior_shrinkage"] = prior_shrinkage
        payload["_calibration_prior"] = calibration_prior.to_dict()
    return replace(
        model,
        payload=payload,
        training_returns=tuple(sample.future_return for sample in final_history),
        training_labels=tuple(sample.direction for sample in final_history),
        temperature=temperature,
    )


def fit_calibrated_model(
    spec: ModelSpec, samples: tuple[ForecastTrainingSample, ...], *, random_seed: int = 20260714,
) -> TrainedForecastModel | None:
    """按完整交易日保留最后 20% 成熟样本校准概率与收益区间。"""
    effective_samples = _windowed_samples(spec, samples)
    start = _calibration_start(effective_samples)
    if start is None:
        return fit_model(spec, effective_samples, random_seed=random_seed)
    calibration = effective_samples[start:]
    calibration_origin = calibration[0].origin_session_date
    training = tuple(
        sample for sample in effective_samples[:start]
        if sample.target_session_date <= calibration_origin
    )
    if len(training) < 30:
        return fit_model(spec, effective_samples, random_seed=random_seed)
    model = fit_model(spec, training, random_seed=random_seed)
    if model is None:
        return None
    calibrated = _calibrate_fitted_model(
        model, calibration, prior_samples=training, final_history=effective_samples,
        calibrate_probabilities=spec.family is not ModelFamily.EMPIRICAL,
    )
    if calibrated is None or spec.family is not ModelFamily.EMPIRICAL:
        return calibrated
    # The held-out slice estimates interval width only.  Direction probability
    # remains the full-history empirical prior used by the original baseline.
    refitted = fit_model(spec, effective_samples, random_seed=random_seed)
    if refitted is None:
        return None
    payload = dict(refitted.payload)
    payload["_interval_scale"] = calibrated.payload["_interval_scale"]
    return replace(refitted, payload=payload)


def fit_panel_calibrated_model(
    spec: ModelSpec,
    panel_samples: tuple[ForecastTrainingSample, ...],
    target_samples: tuple[ForecastTrainingSample, ...],
    *,
    random_seed: int = 20260714,
) -> TrainedForecastModel | None:
    """Fit market-pooled direction parameters and stock-specific calibration.

    The target calibration dates are excluded from every panel instrument's
    direction fit.  Target-stock OOF therefore remains the sole promotion
    authority while the estimator can borrow stable cross-stock relationships.
    """
    target = _windowed_samples(spec, target_samples)
    start = _calibration_start(target)
    if start is None:
        fitted = fit_model(spec, panel_samples, random_seed=random_seed)
        return None if fitted is None else with_stock_return_history(fitted, target)
    calibration = target[start:]
    calibration_origin = calibration[0].origin_session_date
    direction_training = tuple(
        sample for sample in panel_samples
        if sample.origin_session_date < calibration_origin
        and sample.target_session_date <= calibration_origin
    )
    prior_samples = tuple(
        sample for sample in target[:start]
        if sample.target_session_date <= calibration_origin
    )
    if (
        len(prior_samples) < 30
        or len({sample.instrument.stable_key for sample in direction_training}) < 5
    ):
        fitted = fit_model(spec, panel_samples, random_seed=random_seed)
        return None if fitted is None else with_stock_return_history(fitted, target)
    fitted = fit_model(spec, direction_training, random_seed=random_seed)
    if fitted is None:
        return None
    return _calibrate_fitted_model(
        fitted, calibration, prior_samples=prior_samples, final_history=target,
    )


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - values.max(); exp = np.exp(shifted); return exp / exp.sum()


def _calibrated(model: TrainedForecastModel, probabilities: DirectionProbabilities) -> DirectionProbabilities:
    from .diagnostics import apply_temperature
    adjusted = probabilities if model.temperature == 1.0 else apply_temperature(probabilities, model.temperature)
    shrinkage = float(model.payload.get("_prior_shrinkage", 0.0))
    prior_payload = model.payload.get("_calibration_prior")
    if shrinkage <= 0.0 or not isinstance(prior_payload, dict):
        return adjusted
    prior = DirectionProbabilities(**prior_payload)
    return _probabilities(
        np.asarray((adjusted.bullish, adjusted.neutral, adjusted.bearish)) * (1.0 - shrinkage)
        + np.asarray((prior.bullish, prior.neutral, prior.bearish)) * shrinkage
    )


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
            same_regime = np.flatnonzero(regimes == current_regime) if current_regime is not None else np.asarray([], dtype=int)
            # A rare or newly observed regime must not make this candidate skip
            # an OOF event.  Use the state-specific neighborhood only after it
            # has the frozen minimum; otherwise shrink all the way back to the
            # ordinary stock-level analog pool.  The candidate still has to
            # beat/non-inferiorly match the baseline on every OOF event.
            eligible = same_regime if len(same_regime) >= 30 else np.arange(len(distances))
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
        probabilities = _probabilities(values)
        empirical_blend = float(model.spec.hyperparameters.get("empirical_blend", 0.0))
        if not 0.0 <= empirical_blend <= 0.8:
            raise ValueError("empirical_blend must be in [0.0, 0.8]")
        if empirical_blend:
            prior = _laplace(model.training_labels)
            probabilities = _probabilities(
                np.asarray(
                    (probabilities.bullish, probabilities.neutral, probabilities.bearish),
                    dtype=float,
                ) * (1.0 - empirical_blend)
                + np.asarray((prior.bullish, prior.neutral, prior.bearish), dtype=float)
                * empirical_blend
            )
        probabilities = _calibrated(model, probabilities)
        return probabilities, _class_mixture_distribution(model, probabilities)
    if family is ModelFamily.PROBABILITY_TREE:
        values = np.ones(3, dtype=float)
        for class_index, count in zip(model.payload["classes"], _tree_leaf_values(model.payload, vector)): values[int(class_index)] += count
        probabilities = _calibrated(model, _probabilities(values))
        return probabilities, _class_mixture_distribution(model, probabilities)
    if family is ModelFamily.PROBABILITY_FOREST:
        values = np.zeros(3, dtype=float)
        for tree in model.payload["trees"]:
            leaf = np.asarray(_tree_leaf_values(tree, vector), dtype=float)
            total = float(leaf.sum())
            if total <= 0:
                continue
            for class_index, probability in zip(model.payload["classes"], leaf / total):
                values[int(class_index)] += float(probability)
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
    probabilities, distribution = _model_probabilities(model, row)
    scale = float(model.payload.get("_interval_scale", 1.0))
    if scale != 1.0:
        center = float(distribution.p50)
        distribution = ReturnDistribution(
            center - (center - float(distribution.p10)) * scale,
            center,
            center + (float(distribution.p90) - center) * scale,
            distribution.method,
        )
    return probabilities, distribution


def local_replacement_drivers(model: TrainedForecastModel, snapshot, *, maximum: int = 5):
    """用“替换为训练中位数”的局部扰动排序解释项。

    它只说明当前模型在输入附近依赖哪些字段，不等于因果解释，更不是
    交易条件。
    """
    from contracts import ForecastDriver
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
