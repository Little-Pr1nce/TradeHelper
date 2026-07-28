"""无泄漏预测诊断与确定性不确定性区间。

所有统计量都带样本数；Bootstrap 使用稳定 seed 和时间块，而非把相邻
交易日错误当作独立随机样本。
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from contracts import DirectionProbabilities, ForecastDirection


@dataclass(frozen=True, slots=True)
class ForecastMetrics:
    """一个评估切片的概率质量、校准和区间覆盖结果。"""
    multiclass_brier: float
    log_loss: float
    accuracy: float
    expected_calibration_error: float
    interval_coverage: float
    sample_count: int
    brier_ci_low: float
    brier_ci_high: float


def _vector(probabilities: DirectionProbabilities) -> np.ndarray:
    return np.asarray((probabilities.bullish, probabilities.neutral, probabilities.bearish), dtype=float)


def _index(direction: ForecastDirection) -> int:
    return (ForecastDirection.BULLISH, ForecastDirection.NEUTRAL, ForecastDirection.BEARISH).index(direction)


def multiclass_brier(probabilities: tuple[DirectionProbabilities, ...], labels: tuple[ForecastDirection, ...]) -> float:
    if not labels:
        return math.nan
    scores = []
    for prediction, label in zip(probabilities, labels):
        actual = np.zeros(3); actual[_index(label)] = 1.0
        scores.append(float(np.square(_vector(prediction) - actual).sum()))
    return float(np.mean(scores))


def log_loss(probabilities: tuple[DirectionProbabilities, ...], labels: tuple[ForecastDirection, ...]) -> float:
    if not labels:
        return math.nan
    return float(-np.mean([math.log(max(_vector(prediction)[_index(label)], 1e-15)) for prediction, label in zip(probabilities, labels)]))


def expected_calibration_error(probabilities: tuple[DirectionProbabilities, ...], labels: tuple[ForecastDirection, ...], *, bins: int = 10) -> float:
    """按最大预测概率分箱计算 ECE，衡量模型自信程度是否可信。"""
    if not labels:
        return math.nan
    confidences = np.asarray([max(_vector(item)) for item in probabilities]); predictions = np.asarray([np.argmax(_vector(item)) for item in probabilities])
    actual = np.asarray([_index(item) for item in labels]); total = len(labels); score = 0.0
    for bin_index in range(bins):
        lower, upper = bin_index / bins, (bin_index + 1) / bins
        mask = (confidences >= lower) & ((confidences < upper) if bin_index < bins - 1 else (confidences <= upper))
        if mask.any():
            score += mask.mean() * abs(float(confidences[mask].mean()) - float((predictions[mask] == actual[mask]).mean()))
    return float(score)


def bootstrap_brier_interval(probabilities: tuple[DirectionProbabilities, ...], labels: tuple[ForecastDirection, ...], *, horizon: int, seed: int, draws: int = 1000) -> tuple[float, float]:
    if not labels:
        return math.nan, math.nan
    probability_matrix = np.asarray([_vector(item) for item in probabilities], dtype=float)
    actual = np.zeros_like(probability_matrix)
    actual[np.arange(len(labels)), np.asarray([_index(item) for item in labels], dtype=int)] = 1.0
    event_scores = np.square(probability_matrix - actual).sum(axis=1)
    rng = np.random.default_rng(seed); n = len(labels); block = min(n, max(5, horizon))
    block_count = int(math.ceil(n / block))
    starts = rng.integers(0, n, size=(draws, block_count))
    indices = ((starts[:, :, None] + np.arange(block)[None, None, :]) % n).reshape(draws, -1)[:, :n]
    scores = event_scores[indices].mean(axis=1)
    return float(np.quantile(scores, 0.025)), float(np.quantile(scores, 0.975))


def paired_brier_improvement_interval(
    baseline: tuple[DirectionProbabilities, ...], candidate: tuple[DirectionProbabilities, ...], labels: tuple[ForecastDirection, ...], *, horizon: int, seed: int, draws: int = 1000,
) -> tuple[float, float, float, float, float]:
    """对同一 OOF 事件比较 baseline-candidate Brier 的时间块区间。

    正数代表候选更好；配对比较避免不同市场阶段的差异污染结论。
    """
    if not labels:
        return math.nan, math.nan, math.nan, math.nan, math.nan
    per_event = []
    for base, proposed, label in zip(baseline, candidate, labels):
        actual = np.zeros(3); actual[_index(label)] = 1.0
        per_event.append(float(np.square(_vector(base) - actual).sum() - np.square(_vector(proposed) - actual).sum()))
    values = np.asarray(per_event); rng = np.random.default_rng(seed); n = len(values); block = min(n, max(5, horizon))
    block_count = int(math.ceil(n / block))
    starts = rng.integers(0, n, size=(draws, block_count))
    indices = ((starts[:, :, None] + np.arange(block)[None, None, :]) % n).reshape(draws, -1)[:, :n]
    means = values[indices].mean(axis=1)
    return float(values.mean()), float(np.quantile(means, .10)), float(np.quantile(means, .90)), float(np.quantile(means, .05)), float(np.quantile(means, .95))


def evaluate_predictions(probabilities: tuple[DirectionProbabilities, ...], labels: tuple[ForecastDirection, ...], future_returns: tuple[float, ...], intervals: tuple, *, horizon: int, seed: int) -> ForecastMetrics:
    if not (len(probabilities) == len(labels) == len(future_returns) == len(intervals)):
        raise ValueError("forecast diagnostic arrays must align")
    if not labels:
        return ForecastMetrics(math.nan, math.nan, math.nan, math.nan, math.nan, 0, math.nan, math.nan)
    accuracy = np.mean([int(np.argmax(_vector(prediction)) == _index(label)) for prediction, label in zip(probabilities, labels)])
    coverage = np.mean([int(interval.p10 <= observed <= interval.p90) for observed, interval in zip(future_returns, intervals)])
    low, high = bootstrap_brier_interval(probabilities, labels, horizon=horizon, seed=seed)
    return ForecastMetrics(multiclass_brier(probabilities, labels), log_loss(probabilities, labels), float(accuracy), expected_calibration_error(probabilities, labels), float(coverage), len(labels), low, high)


def apply_temperature(probability: DirectionProbabilities, temperature: float) -> DirectionProbabilities:
    if not 0.5 <= temperature <= 5.0:
        raise ValueError("temperature must be in [0.5, 5.0]")
    values = np.maximum(_vector(probability), 1e-15); adjusted = np.exp(np.log(values) / temperature); adjusted /= adjusted.sum()
    return DirectionProbabilities(*map(float, adjusted))


def fit_temperature(probabilities: tuple[DirectionProbabilities, ...], labels: tuple[ForecastDirection, ...]) -> float:
    """以固定网格拟合温度，避免引入不透明且不稳定的优化器。"""
    if len(labels) < 30:
        return 1.0
    candidates = np.linspace(0.5, 5.0, 91)
    values = np.clip(np.asarray([_vector(item) for item in probabilities], dtype=float), 1e-15, 1.0)
    logits = np.log(values)[None, :, :] / candidates[:, None, None]
    adjusted = np.exp(logits - logits.max(axis=2, keepdims=True))
    adjusted /= adjusted.sum(axis=2, keepdims=True)
    actual = np.asarray([_index(item) for item in labels], dtype=int)
    losses = -np.log(np.clip(adjusted[:, np.arange(len(labels)), actual], 1e-15, 1.0)).mean(axis=1)
    return float(candidates[int(np.argmin(losses))])
