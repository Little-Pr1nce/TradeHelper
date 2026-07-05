"""独立概率预测引擎。

预测只使用截至当前时点可见的 OHLCV 派生特征，不读取策略动作。首个
Champion 使用历史相似状态的经验分布，透明、可复现，并会随已知历史
样本增加自动重新校准概率和收益区间。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

import numpy as np
import pandas as pd

from data.models import ForecastResult
from utils.trading_calendar import TradingTargets, forecast_target_dates


FORECAST_MODEL_FAMILY = "forecast_v4"
MODEL_VERSION = f"{FORECAST_MODEL_FAMILY}_analog"
FEATURE_NAMES = ("momentum_5", "momentum_20", "trend_20", "volatility_20")
PROBABILITY_EPSILON = 1e-12


def forecast_candidate_configs() -> list[dict]:
    """Controlled model space; candidates still need two-window OOF promotion."""
    return [
        {"model_type": "analog", "neighbor_count": neighbors, "flat_threshold": 0.01}
        for neighbors in (40, 80, 120)
    ] + [
        {"model_type": "logistic", "regularization": value, "flat_threshold": 0.01}
        for value in (0.05, 0.20)
    ] + [
        {"model_type": "tree", "max_depth": 2, "min_leaf": value, "flat_threshold": 0.01}
        for value in (15, 25)
    ] + [{
        "model_type": "ensemble", "neighbor_count": 80,
        "regularization": 0.20, "blend_weight": 0.50,
        "flat_threshold": 0.01,
    }]


def generate_forecasts(
    df: pd.DataFrame,
    *,
    code: str,
    market: str,
    mode: str,
    market_regime: str = "unknown",
    horizons: tuple[int, ...] = (1, 3, 5),
    generated_at: str | None = None,
    targets: TradingTargets | None = None,
    min_samples: int = 30,
    neighbor_count: int = 80,
    configs_by_horizon: dict[int, dict] | None = None,
) -> list[ForecastResult]:
    """生成 1/3/5 日独立预测；样本不足时不输出伪概率。"""
    if df is None or df.empty or "close" not in df.columns or len(df) < 60:
        return []
    work = df.copy().reset_index(drop=True)
    close = pd.to_numeric(work["close"], errors="coerce")
    if close.dropna().empty or float(close.iloc[-1] or 0.0) <= 0:
        return []
    feature_frame = _feature_frame(close)
    current = feature_frame.iloc[-1]
    if current.isna().any():
        return []

    data_cutoff = str(work["date"].iloc[-1])[:10] if "date" in work.columns else ""
    if not data_cutoff:
        return []
    targets = targets or forecast_target_dates(data_cutoff, market, horizons)
    now = generated_at or datetime.now().isoformat()
    reference_price = float(close.iloc[-1])
    feature_hash = _feature_hash(current, data_cutoff)
    results = []

    for horizon in sorted({int(h) for h in horizons if int(h) > 0}):
        config = (configs_by_horizon or {}).get(horizon, {})
        candidate = _normalize_candidate_config({
            "model_type": config.get("model_type", "analog"),
            "neighbor_count": config.get("neighbor_count", neighbor_count),
            "regularization": config.get("regularization", 0.20),
            "blend_weight": config.get("blend_weight", 0.50),
            "max_depth": config.get("max_depth", 2),
            "min_leaf": config.get("min_leaf", 20),
            "flat_threshold": 0.01,
        })
        # 正式标签定义固定为 ±1%，模型选择不得通过改变标签口径美化指标。
        horizon_threshold = 0.01
        validated_model = bool(config.get("validated", False))
        model_version = str(
            config.get("model_version")
            or f"{MODEL_VERSION}_unvalidated"
        )
        target_date = targets.dates.get(horizon, "")
        if not target_date:
            continue
        future_return = close.shift(-horizon) / close - 1.0
        samples = feature_frame.copy()
        samples["future_return"] = future_return
        samples = samples.iloc[:-horizon].dropna()
        if len(samples) < min_samples:
            continue

        predicted = _predict_candidate(samples, current, candidate, horizon_threshold)
        if predicted is None:
            continue
        probability_values, returns, return_weights = predicted
        if len(returns) < min_samples:
            continue
        prob_up, prob_flat, prob_down = probability_values.tolist()
        probabilities = {
            "bullish": prob_up,
            "neutral": prob_flat,
            "bearish": prob_down,
        }
        direction = max(probabilities, key=probabilities.get)
        ordered = sorted(probabilities.values(), reverse=True)
        margin = ordered[0] - ordered[1]
        confidence = (
            min(len(samples) / max(int(candidate.get("neighbor_count", 80)), 1), 1.0) * margin
            if validated_model else 0.0
        )
        p10, p50, p90 = _weighted_quantile(
            returns, [0.10, 0.50, 0.90], return_weights,
        )
        event_key = (
            f"{code.upper()}|{mode}|{data_cutoff}|{target_date}|"
            f"{horizon}"
        )
        results.append(ForecastResult(
            code=code.upper(), market=market, mode=mode,
            generated_at=now, data_cutoff=data_cutoff,
            target_session_date=target_date, horizon=horizon,
            reference_price=reference_price,
            prob_up=prob_up, prob_flat=prob_flat, prob_down=prob_down,
            expected_return_p10=float(p10),
            expected_return_p50=float(p50),
            expected_return_p90=float(p90),
            direction=direction, confidence=float(confidence),
            market_regime=market_regime, model_version=model_version,
            feature_snapshot_hash=feature_hash, sample_count=len(samples),
            calendar_source=targets.source, event_key=event_key,
        ))
    return results


def evaluate_forecast_candidates(
    df: pd.DataFrame,
    *,
    horizon: int,
    candidates: list[dict] | None = None,
    min_train_samples: int = 60,
    max_evaluations: int = 80,
    evaluation_end_offset: int = 0,
) -> list[dict]:
    """用逐点 OOF 回放评估候选参数，任何评估点都只看当时已知数据。"""
    if df is None or len(df) < min_train_samples + horizon + 20:
        return []
    close = pd.to_numeric(df["close"], errors="coerce").reset_index(drop=True)
    features = _feature_frame(close)
    returns = close.shift(-horizon) / close - 1.0
    candidates = candidates or forecast_candidate_configs()
    last_origin = len(close) - horizon - 1 - max(0, int(evaluation_end_offset))
    first_origin = max(min_train_samples, last_origin - max_evaluations + 1)
    results = []
    for candidate in candidates:
        candidate = _normalize_candidate_config(candidate)
        threshold = 0.01
        briers, baseline_briers, baseline_log_losses = [], [], []
        correct, interval_hits, log_losses = [], [], []
        probability_rows, baseline_probability_rows = [], []
        actual_indices, regimes = [], []
        for origin in range(first_origin, last_origin + 1):
            current = features.iloc[origin]
            if current.isna().any():
                continue
            # origin 时只能使用其结果已经到期的样本，即 sample+horizon<=origin。
            eligible = features.iloc[:origin - horizon + 1].copy()
            eligible["future_return"] = returns.iloc[:origin - horizon + 1]
            eligible = eligible.dropna()
            if len(eligible) < min_train_samples:
                continue
            predicted = _predict_candidate(eligible, current, candidate, threshold)
            if predicted is None:
                continue
            probabilities, sample_returns, sample_weights = predicted
            actual_return = float(returns.iloc[origin])
            actual_index = 0 if actual_return > threshold else 2 if actual_return < -threshold else 1
            outcome = np.zeros(3)
            outcome[actual_index] = 1.0
            briers.append(float(((probabilities - outcome) ** 2).sum()))
            log_losses.append(multiclass_log_loss(probabilities, actual_index))
            probability_rows.append(probabilities.tolist())
            actual_indices.append(actual_index)
            regimes.append(_regime_at_origin(df, origin))
            historical_returns = eligible["future_return"].astype(float).to_numpy()
            historical_counts = np.array([
                (historical_returns > threshold).sum(),
                ((historical_returns >= -threshold) & (historical_returns <= threshold)).sum(),
                (historical_returns < -threshold).sum(),
            ], dtype=float) + 1.0
            baseline_probabilities = historical_counts / historical_counts.sum()
            baseline_probability_rows.append(baseline_probabilities.tolist())
            baseline_briers.append(
                float(((baseline_probabilities - outcome) ** 2).sum())
            )
            baseline_log_losses.append(
                multiclass_log_loss(baseline_probabilities, actual_index)
            )
            correct.append(int(int(probabilities.argmax()) == actual_index))
            p10, p90 = _weighted_quantile(
                sample_returns, [0.10, 0.90], sample_weights,
            )
            interval_hits.append(int(float(p10) <= actual_return <= float(p90)))
        if not briers:
            continue
        diagnostics = probability_diagnostics(
            probability_rows, actual_indices, regimes=regimes,
        )
        baseline_diagnostics = probability_diagnostics(
            baseline_probability_rows, actual_indices, regimes=regimes,
        )
        improvement = paired_block_improvement(
            briers,
            baseline_briers,
            block_size=max(3, int(horizon)),
        )
        results.append({
            "params": dict(candidate),
            "samples": len(briers),
            "brier_score": float(np.mean(briers)),
            "baseline_brier": float(np.mean(baseline_briers)),
            "log_loss": float(np.mean(log_losses)),
            "baseline_log_loss": float(np.mean(baseline_log_losses)),
            "ece": diagnostics["ece"],
            "baseline_ece": baseline_diagnostics["ece"],
            "brier_improvement": improvement["mean_improvement"],
            "brier_improvement_lower_90": improvement["lower_90"],
            "brier_improvement_probability": improvement["probability_positive"],
            "calibration_bins": diagnostics["calibration_bins"],
            "regime_metrics": diagnostics["regime_metrics"],
            "accuracy": float(np.mean(correct)),
            "interval_coverage": float(np.mean(interval_hits)),
            "_brier_losses": list(briers),
            "_baseline_brier_losses": list(baseline_briers),
        })
    return sorted(
        results,
        key=lambda item: (item["brier_score"], item["log_loss"], -item["accuracy"]),
    )


def paired_block_improvement(
    candidate_losses: list[float],
    comparator_losses: list[float],
    *,
    block_size: int = 5,
    iterations: int = 500,
) -> dict:
    """Estimate paired loss improvement with a deterministic moving-block bootstrap."""
    count = min(len(candidate_losses), len(comparator_losses))
    if count <= 0:
        return {
            "mean_improvement": 0.0,
            "lower_90": float("-inf"),
            "probability_positive": 0.0,
        }
    candidate = np.asarray(candidate_losses[:count], dtype=float)
    comparator = np.asarray(comparator_losses[:count], dtype=float)
    differences = comparator - candidate
    differences = differences[np.isfinite(differences)]
    if len(differences) < 10:
        return {
            "mean_improvement": float(np.mean(differences)) if len(differences) else 0.0,
            "lower_90": float("-inf"),
            "probability_positive": 0.0,
        }

    size = max(1, min(int(block_size), len(differences)))
    starts = np.arange(0, len(differences) - size + 1)
    rng = np.random.default_rng(20260705 + len(differences) * 31 + size)
    means = []
    blocks_needed = int(np.ceil(len(differences) / size))
    for _ in range(max(int(iterations), 100)):
        sampled_starts = rng.choice(starts, size=blocks_needed, replace=True)
        sample = np.concatenate([
            differences[start:start + size] for start in sampled_starts
        ])[:len(differences)]
        means.append(float(np.mean(sample)))
    return {
        "mean_improvement": float(np.mean(differences)),
        "lower_90": float(np.quantile(means, 0.10)),
        "probability_positive": float(np.mean(np.asarray(means) > 0.0)),
    }


def forecast_candidate_passes_baseline(candidate: dict, *, min_samples: int = 30) -> bool:
    """Require performance and calibration evidence before a model is trusted."""
    baseline_brier = float(candidate.get("baseline_brier", 0.0) or 0.0)
    baseline_ece = float(candidate.get("baseline_ece", 0.0) or 0.0)
    return bool(
        int(candidate.get("samples", 0) or 0) >= min_samples
        and baseline_brier > 0
        and float(candidate.get("brier_score", 0.0) or 0.0) <= baseline_brier * 0.98
        and float(candidate.get("log_loss", 0.0) or 0.0)
            <= float(candidate.get("baseline_log_loss", 0.0) or 0.0)
        and float(candidate.get("brier_improvement_lower_90", float("-inf"))) > 0
        and float(candidate.get("ece", 1.0) or 0.0) <= baseline_ece + 0.02
        and float(candidate.get("interval_coverage", 0.0) or 0.0) >= 0.70
    )


def generate_oof_forecast_snapshot(
    df: pd.DataFrame,
    *,
    code: str,
    market: str,
    horizon: int = 1,
    neighbor_count: int = 80,
    model_config: dict | None = None,
    min_train_samples: int = 60,
    validated: bool = False,
    model_version: str = "analog_oof_v1",
) -> ForecastResult | None:
    """Generate one expanding-window forecast using only matured labels.

    This is the forecast primitive used by the joint policy replay.  At the
    last row, a historical sample is eligible only when its horizon return was
    already observable, so changing later rows cannot alter this snapshot.
    """
    if df is None or len(df) < min_train_samples + horizon + 1:
        return None
    work = df.reset_index(drop=True)
    close = pd.to_numeric(work["close"], errors="coerce")
    features = _feature_frame(close)
    origin = len(work) - 1
    current = features.iloc[origin]
    if current.isna().any() or float(close.iloc[origin] or 0.0) <= 0:
        return None
    future_return = close.shift(-horizon) / close - 1.0
    eligible = features.iloc[:origin - horizon + 1].copy()
    eligible["future_return"] = future_return.iloc[:origin - horizon + 1]
    eligible = eligible.dropna()
    if len(eligible) < min_train_samples:
        return None
    threshold = 0.01
    candidate = _normalize_candidate_config(model_config or {
        "model_type": "analog", "neighbor_count": neighbor_count,
    })
    predicted = _predict_candidate(eligible, current, candidate, threshold)
    if predicted is None:
        return None
    probabilities, returns, return_weights = predicted
    direction = ("bullish", "neutral", "bearish")[int(probabilities.argmax())]
    ordered = np.sort(probabilities)[::-1]
    confidence = float(ordered[0] - ordered[1]) if validated else 0.0
    p10, p50, p90 = _weighted_quantile(
        returns, [0.10, 0.50, 0.90], return_weights,
    )
    data_cutoff = str(work["date"].iloc[-1])[:10] if "date" in work.columns else ""
    regime = _regime_at_origin(work, origin)
    return ForecastResult(
        code=code.upper(), market=market, mode="oof", data_cutoff=data_cutoff,
        target_session_date="", horizon=horizon,
        reference_price=float(close.iloc[-1]),
        prob_up=float(probabilities[0]), prob_flat=float(probabilities[1]),
        prob_down=float(probabilities[2]),
        expected_return_p10=float(p10), expected_return_p50=float(p50),
        expected_return_p90=float(p90), direction=direction,
        confidence=confidence, market_regime=regime,
        model_version=model_version, feature_snapshot_hash=_feature_hash(current, data_cutoff),
        sample_count=len(eligible), calendar_source="historical_session_index",
        event_key=f"{code.upper()}|oof|{data_cutoff}|{horizon}",
    )


def _normalize_candidate_config(candidate: dict | None) -> dict:
    source = candidate or {}
    model_type = str(source.get("model_type") or "analog").lower()
    if model_type not in {"analog", "logistic", "tree", "ensemble"}:
        model_type = "analog"
    result = {"model_type": model_type, "flat_threshold": 0.01}
    if model_type in {"analog", "ensemble"}:
        result["neighbor_count"] = max(20, int(source.get("neighbor_count", 80)))
    if model_type in {"logistic", "ensemble"}:
        result["regularization"] = float(np.clip(
            float(source.get("regularization", 0.20)), 0.001, 10.0,
        ))
    if model_type == "ensemble":
        result["blend_weight"] = float(np.clip(
            float(source.get("blend_weight", 0.50)), 0.20, 0.80,
        ))
    if model_type == "tree":
        result["max_depth"] = max(1, min(int(source.get("max_depth", 2)), 3))
        result["min_leaf"] = max(10, int(source.get("min_leaf", 20)))
    return result


def _predict_candidate(
    samples: pd.DataFrame,
    current: pd.Series,
    candidate: dict,
    threshold: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if samples is None or samples.empty:
        return None
    feature_values = samples[list(FEATURE_NAMES)].astype(float)
    returns_all = samples["future_return"].astype(float).to_numpy()
    scale = feature_values.std(ddof=0).replace(0, 1.0).fillna(1.0)
    distance = (
        ((feature_values - current[list(FEATURE_NAMES)].astype(float)) / scale) ** 2
    ).sum(axis=1)
    neighbors = max(20, int(candidate.get("neighbor_count", 80)))
    nearest = samples.loc[
        distance.nsmallest(min(neighbors, len(samples))).index
    ]
    interval_returns = nearest["future_return"].astype(float).to_numpy()
    analog_probabilities = _empirical_probabilities(interval_returns, threshold)
    model_type = str(candidate.get("model_type") or "analog")
    if model_type == "analog":
        return _coherent_return_distribution(
            analog_probabilities, interval_returns, threshold,
        )

    labels = np.where(
        returns_all > threshold, 0,
        np.where(returns_all < -threshold, 2, 1),
    ).astype(int)
    if model_type == "tree":
        probabilities = _shallow_tree_probabilities(
            feature_values.to_numpy(dtype=float), labels,
            current[list(FEATURE_NAMES)].to_numpy(dtype=float),
            max_depth=int(candidate.get("max_depth", 2)),
            min_leaf=int(candidate.get("min_leaf", 20)),
        )
        return _coherent_return_distribution(probabilities, returns_all, threshold)
    logistic_probabilities = _regularized_logistic_probabilities(
        feature_values.to_numpy(dtype=float), labels,
        current[list(FEATURE_NAMES)].to_numpy(dtype=float),
        regularization=float(candidate.get("regularization", 0.20)),
    )
    if model_type == "logistic":
        return _coherent_return_distribution(
            logistic_probabilities, returns_all, threshold,
        )
    blend = float(candidate.get("blend_weight", 0.50))
    probabilities = blend * analog_probabilities + (1.0 - blend) * logistic_probabilities
    probabilities = np.clip(probabilities, PROBABILITY_EPSILON, 1.0)
    probabilities /= probabilities.sum()
    return _coherent_return_distribution(probabilities, returns_all, threshold)


def _coherent_return_distribution(
    probabilities: np.ndarray,
    returns: np.ndarray,
    threshold: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Map class probabilities onto returns from the same fitted sample."""
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return np.full(3, 1.0 / 3.0), np.array([0.0]), np.array([1.0])
    labels = np.where(
        values > threshold, 0,
        np.where(values < -threshold, 2, 1),
    ).astype(int)
    probs = np.asarray(probabilities, dtype=float).copy()
    counts = np.bincount(labels, minlength=3)
    probs[counts == 0] = 0.0
    if probs.sum() <= 0:
        probs = counts.astype(float)
    probs /= probs.sum()
    weights = np.zeros(len(values), dtype=float)
    for label in range(3):
        mask = labels == label
        if mask.any():
            weights[mask] = probs[label] / int(mask.sum())
    weights /= weights.sum()
    return probs, values, weights


def _weighted_quantile(
    values: np.ndarray,
    quantiles: list[float],
    weights: np.ndarray,
) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    valid = np.isfinite(values) & np.isfinite(weights) & (weights >= 0)
    values, weights = values[valid], weights[valid]
    if len(values) == 0 or weights.sum() <= 0:
        return np.zeros(len(quantiles), dtype=float)
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cumulative = np.cumsum(weights) / weights.sum()
    return np.interp(np.asarray(quantiles, dtype=float), cumulative, values)


def _empirical_probabilities(returns: np.ndarray, threshold: float) -> np.ndarray:
    counts = np.array([
        (returns > threshold).sum(),
        ((returns >= -threshold) & (returns <= threshold)).sum(),
        (returns < -threshold).sum(),
    ], dtype=float) + 1.0
    return counts / counts.sum()


def _regularized_logistic_probabilities(
    features: np.ndarray,
    labels: np.ndarray,
    current: np.ndarray,
    *,
    regularization: float,
    iterations: int = 80,
) -> np.ndarray:
    """Small deterministic multinomial logistic model without external ML state."""
    if len(features) == 0:
        return np.full(3, 1.0 / 3.0)
    mean = np.nanmean(features, axis=0)
    scale = np.nanstd(features, axis=0)
    scale = np.where(np.isfinite(scale) & (scale > 1e-12), scale, 1.0)
    x = np.nan_to_num((features - mean) / scale, nan=0.0, posinf=0.0, neginf=0.0)
    point = np.nan_to_num((current - mean) / scale, nan=0.0, posinf=0.0, neginf=0.0)
    design = np.column_stack([np.ones(len(x)), x])
    point_design = np.concatenate([[1.0], point])
    outcome = np.eye(3)[labels]
    weights = np.zeros((design.shape[1], 3), dtype=float)
    learning_rate = 0.18
    penalty = max(float(regularization), 0.001)
    for _ in range(max(int(iterations), 1)):
        logits = np.clip(design @ weights, -30.0, 30.0)
        logits -= logits.max(axis=1, keepdims=True)
        probabilities = np.exp(logits)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        gradient = design.T @ (probabilities - outcome) / len(design)
        gradient[1:] += penalty * weights[1:]
        weights -= learning_rate * gradient
    logits = np.clip(point_design @ weights, -30.0, 30.0)
    logits -= logits.max()
    probabilities = np.exp(logits)
    probabilities /= probabilities.sum()
    # Small prior shrinkage reduces overconfidence in short OOF windows.
    prior = (np.bincount(labels, minlength=3).astype(float) + 1.0)
    prior /= prior.sum()
    probabilities = 0.90 * probabilities + 0.10 * prior
    probabilities = np.clip(probabilities, PROBABILITY_EPSILON, 1.0)
    return probabilities / probabilities.sum()


def _shallow_tree_probabilities(
    features: np.ndarray,
    labels: np.ndarray,
    current: np.ndarray,
    *,
    max_depth: int,
    min_leaf: int,
) -> np.ndarray:
    """Deterministic shallow probability tree with Laplace/prior shrinkage."""
    if len(features) == 0:
        return np.full(3, 1.0 / 3.0)
    x = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)
    point = np.nan_to_num(current, nan=0.0, posinf=0.0, neginf=0.0)
    indices = np.arange(len(x))
    global_counts = np.bincount(labels, minlength=3).astype(float) + 1.0
    global_prior = global_counts / global_counts.sum()

    for _ in range(max(1, int(max_depth))):
        if len(indices) < max(2 * int(min_leaf), 2):
            break
        best = None
        for feature_index in range(x.shape[1]):
            values = x[indices, feature_index]
            for threshold in np.unique(np.quantile(values, [0.2, 0.4, 0.6, 0.8])):
                left = indices[values <= threshold]
                right = indices[values > threshold]
                if len(left) < min_leaf or len(right) < min_leaf:
                    continue
                score = (
                    len(left) * _gini(labels[left])
                    + len(right) * _gini(labels[right])
                ) / len(indices)
                candidate = (float(score), feature_index, float(threshold), left, right)
                if best is None or candidate[:3] < best[:3]:
                    best = candidate
        if best is None:
            break
        _, feature_index, threshold, left, right = best
        indices = left if point[feature_index] <= threshold else right

    leaf_counts = np.bincount(labels[indices], minlength=3).astype(float) + 1.0
    leaf_probabilities = leaf_counts / leaf_counts.sum()
    probabilities = 0.85 * leaf_probabilities + 0.15 * global_prior
    probabilities = np.clip(probabilities, PROBABILITY_EPSILON, 1.0)
    return probabilities / probabilities.sum()


def _gini(labels: np.ndarray) -> float:
    if len(labels) == 0:
        return 0.0
    counts = np.bincount(labels, minlength=3).astype(float)
    probabilities = counts / counts.sum()
    return float(1.0 - np.square(probabilities).sum())


def multiclass_log_loss(probabilities, actual_index: int) -> float:
    """Return numerically stable multiclass logarithmic loss."""
    values = np.asarray(probabilities, dtype=float)
    if values.size == 0 or actual_index < 0 or actual_index >= values.size:
        return 0.0
    values = np.clip(values, PROBABILITY_EPSILON, 1.0)
    values /= values.sum()
    return float(-np.log(values[int(actual_index)]))


def probability_diagnostics(
    probabilities,
    actual_indices,
    *,
    regimes: list[str] | None = None,
    bins: int = 5,
) -> dict:
    """Calculate top-label ECE, calibration bins and regime diagnostics."""
    probs = np.asarray(probabilities, dtype=float)
    actual = np.asarray(actual_indices, dtype=int)
    if probs.ndim != 2 or len(probs) == 0 or len(probs) != len(actual):
        return {"ece": 0.0, "calibration_bins": [], "regime_metrics": {}}
    probs = np.clip(probs, PROBABILITY_EPSILON, 1.0)
    probs /= probs.sum(axis=1, keepdims=True)
    confidence = probs.max(axis=1)
    predicted = probs.argmax(axis=1)
    correct = (predicted == actual).astype(float)
    edges = np.linspace(0.0, 1.0, max(int(bins), 1) + 1)
    calibration = []
    ece = 0.0
    for index in range(len(edges) - 1):
        lower, upper = float(edges[index]), float(edges[index + 1])
        mask = ((confidence >= lower) & (confidence < upper))
        if index == len(edges) - 2:
            mask = (confidence >= lower) & (confidence <= upper)
        count = int(mask.sum())
        if count <= 0:
            continue
        mean_confidence = float(confidence[mask].mean())
        accuracy = float(correct[mask].mean())
        gap = abs(mean_confidence - accuracy)
        ece += count / len(probs) * gap
        calibration.append({
            "lower": lower, "upper": upper, "count": count,
            "mean_confidence": mean_confidence, "accuracy": accuracy, "gap": gap,
        })

    regime_values = regimes or ["unknown"] * len(probs)
    regime_metrics = {}
    for regime in sorted(set(regime_values)):
        indices = [i for i, value in enumerate(regime_values) if value == regime]
        if not indices:
            continue
        rp = probs[indices]
        ra = actual[indices]
        outcome = np.eye(probs.shape[1])[ra]
        regime_metrics[str(regime)] = {
            "samples": len(indices),
            "accuracy": float((rp.argmax(axis=1) == ra).mean()),
            "brier_score": float(((rp - outcome) ** 2).sum(axis=1).mean()),
            "log_loss": float(np.mean([
                multiclass_log_loss(row, int(label)) for row, label in zip(rp, ra)
            ])),
            "ece": _top_label_ece(rp, ra, bins=bins),
        }
    return {
        "ece": float(ece),
        "calibration_bins": calibration,
        "regime_metrics": regime_metrics,
    }


def _top_label_ece(probabilities: np.ndarray, actual: np.ndarray, *, bins: int) -> float:
    confidence = probabilities.max(axis=1)
    correct = (probabilities.argmax(axis=1) == actual).astype(float)
    edges = np.linspace(0.0, 1.0, max(int(bins), 1) + 1)
    value = 0.0
    for index in range(len(edges) - 1):
        lower, upper = edges[index], edges[index + 1]
        mask = (confidence >= lower) & (confidence < upper)
        if index == len(edges) - 2:
            mask = (confidence >= lower) & (confidence <= upper)
        if mask.any():
            value += float(mask.mean()) * abs(
                float(confidence[mask].mean()) - float(correct[mask].mean())
            )
    return float(value)


def _feature_frame(close: pd.Series) -> pd.DataFrame:
    returns = close.pct_change()
    ma20 = close.rolling(20, min_periods=20).mean()
    return pd.DataFrame({
        "momentum_5": close.pct_change(5),
        "momentum_20": close.pct_change(20),
        "trend_20": close / ma20 - 1.0,
        "volatility_20": returns.rolling(20, min_periods=20).std(),
    })


def _regime_at_origin(df: pd.DataFrame, origin: int) -> str:
    try:
        from alpha.scoring import detect_market_regime

        return str(detect_market_regime(df.iloc[:origin + 1])[0])
    except Exception:
        return "unknown"


def _feature_hash(current: pd.Series, data_cutoff: str) -> str:
    payload = data_cutoff + "|" + "|".join(
        f"{name}={float(current[name]):.10f}" for name in FEATURE_NAMES
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
