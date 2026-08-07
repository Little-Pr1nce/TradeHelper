"""扩张窗口 OOF 训练、证据筛选与 Champion 晋升准备。

训练器只生成统计证据和安全 artifact；数据库中的 Champion 替换由
repository 事务完成，因此取消或 confirmation 失败不会留下半成品。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from typing import Callable

import numpy as np

from contracts import (
    FeatureEvidenceMode, ForecastModelVersion, ForecastScope, ForecastTrainingSample, ModelFamily, ModelLifecycle,
    ModelSpec, ValidationStatus, stable_hash,
)

from .diagnostics import ForecastMetrics, evaluate_predictions, paired_brier_improvement_interval
from .feature_sets import extension_coverage
from .models import (
    InsufficientRegimeSamples, TrainedForecastModel, fit_calibrated_model,
    fit_model, fit_panel_calibrated_model, predict_model,
    with_stock_return_history,
)

# 生产模型按月度节奏重训，期间每日继续发行真正样本外预测。这样 OOF
# 复现实际部署频率，而不是假设桌面应用每天重训全部候选。
OOF_REFIT_INTERVAL = 20
SELECTION_LOOKBACK_DATES = 360


def default_candidate_specs() -> tuple[ModelSpec, ...]:
    """返回冻结且有上限的候选池；经验基线不计入候选数量。"""
    raw = (
        ("analog-tech-k40", ModelFamily.ANALOG, "tech", {"k": 40}),
        ("empirical-stock-w120", ModelFamily.EMPIRICAL, "tech", {"training_window": 120}),
        ("logistic-tech-c0.1", ModelFamily.MULTINOMIAL_LOGISTIC, "tech", {"C": 0.1}),
        ("panel-forest-tech-d4", ModelFamily.PROBABILITY_FOREST, "tech", {"n_estimators": 24, "max_depth": 4, "max_features": 0.7, "training_scope": "market_panel"}),
        ("tree-tech-d2", ModelFamily.PROBABILITY_TREE, "tech", {"max_depth": 2, "min_samples_leaf": "max(15,2pct)"}),
        ("ensemble-tech", ModelFamily.ENSEMBLE, "tech", {"weight": 0.5}),
        ("regime-analog-tech-k40", ModelFamily.REGIME_ANALOG, "tech", {"k": 40}),
        ("regime-analog-tech-k80", ModelFamily.REGIME_ANALOG, "tech", {"k": 80}),
        # Compact domain feature sets reduce dimensional noise for a few hundred
        # stock-specific observations. Rolling variants are fixed before OOF.
        ("analog-trend-k40", ModelFamily.ANALOG, "trend", {"k": 40}),
        ("empirical-stock-w250", ModelFamily.EMPIRICAL, "tech", {"training_window": 250}),
        ("logistic-trend-c0.1", ModelFamily.MULTINOMIAL_LOGISTIC, "trend", {"C": 0.1}),
        ("panel-logistic-trend-c0.1", ModelFamily.MULTINOMIAL_LOGISTIC, "trend", {"C": 0.1, "training_scope": "market_panel", "empirical_blend": 0.5}),
        ("tree-trend-d2", ModelFamily.PROBABILITY_TREE, "trend", {"max_depth": 2, "min_samples_leaf": "max(15,2pct)"}),
        ("ensemble-trend", ModelFamily.ENSEMBLE, "trend", {"weight": 0.5}),
        ("analog-reversion-k40", ModelFamily.ANALOG, "reversion", {"k": 40}),
        ("analog-reversion-k80-w180", ModelFamily.ANALOG, "reversion", {"k": 80, "training_window": 180}),
        ("logistic-reversion-c0.1", ModelFamily.MULTINOMIAL_LOGISTIC, "reversion", {"C": 0.1}),
        ("logistic-reversion-c0.1-w180", ModelFamily.MULTINOMIAL_LOGISTIC, "reversion", {"C": 0.1, "training_window": 180}),
        ("tree-reversion-d2", ModelFamily.PROBABILITY_TREE, "reversion", {"max_depth": 2, "min_samples_leaf": "max(15,2pct)"}),
        ("panel-logistic-reversion-c0.1", ModelFamily.MULTINOMIAL_LOGISTIC, "reversion", {"C": 0.1, "training_scope": "market_panel", "empirical_blend": 0.5}),
    )
    specs = [ModelSpec(spec_id, family, feature_set, parameters, complexity_rank=rank)
             for rank, (spec_id, family, feature_set, parameters) in enumerate(raw, start=1)]
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    spec: ModelSpec
    status: ValidationStatus
    selection: ForecastMetrics | None
    confirmation: ForecastMetrics | None
    baseline_selection: ForecastMetrics | None
    baseline_confirmation: ForecastMetrics | None
    oof_count: int
    temperature: float = 1.0


@dataclass(frozen=True, slots=True)
class TrainingOutcome:
    scope: ForecastScope
    scope_key: str
    horizon: int
    status: ValidationStatus
    evaluations: tuple[CandidateEvaluation, ...]
    champion: ForecastModelVersion | None
    champion_model: TrainedForecastModel | None
    reason: str | None


def selection_split_index(records: list[tuple]) -> int:
    """按完整交易日划分 selection/confirmation，绝不拆开同日横截面。"""
    desired_confirmation = max(30, ceil(len(records) * 0.25))
    split = len(records) - desired_confirmation
    if 0 < split < len(records):
        boundary_date = records[split][0].origin_session_date
        while split > 0 and records[split - 1][0].origin_session_date == boundary_date:
            split -= 1
    return split


def recent_selection_start(records: list[tuple], split: int, *, lookback_dates: int = SELECTION_LOOKBACK_DATES) -> int:
    """Start index for the most recent pre-confirmation market regime.

    Training still uses the full expanding history. Only candidate selection is
    bounded, and complete same-date cross sections remain inseparable.
    """
    dates = sorted({item[0].origin_session_date for item in records[:split]})
    if len(dates) <= lookback_dates:
        return 0
    cutoff = dates[-lookback_dates]
    return next(
        index for index, item in enumerate(records[:split])
        if item[0].origin_session_date >= cutoff
    )


def eligible_panel_training_samples(
    samples: tuple[ForecastTrainingSample, ...], origin_date,
) -> tuple[ForecastTrainingSample, ...]:
    """Return only cross-stock labels already mature at the target OOF origin."""
    return tuple(
        sample for sample in samples
        if sample.origin_session_date < origin_date and sample.target_session_date <= origin_date
    )


def training_data_hash(samples: tuple[ForecastTrainingSample, ...]) -> str:
    return stable_hash([
        {
            "instrument": sample.instrument.stable_key,
            "origin": sample.origin_session_date,
            "target": sample.target_session_date,
            "horizon": sample.horizon,
            "reference_price": sample.reference_price,
            "target_price": sample.target_price,
            "future_return": sample.future_return,
            "flat_band": sample.flat_band,
            "direction": sample.direction.value,
            "feature_hash": sample.feature_hash,
            "evidence_mode": sample.evidence_mode.value,
            "scope_membership": sample.scope_membership,
            "scope_membership_available_at": sample.scope_membership_available_at,
        }
        for sample in samples
    ])


class ForecastTrainer:
    """依市场 scope 与预测周期隔离的 walk-forward 训练器。"""
    def __init__(
        self, *, random_seed: int = 20260714,
        candidate_specs: tuple[ModelSpec, ...] | None = None,
        selection_lookback_dates: int = SELECTION_LOOKBACK_DATES,
    ) -> None:
        self.random_seed = random_seed
        self.candidate_specs = candidate_specs or default_candidate_specs()
        self.selection_lookback_dates = int(selection_lookback_dates)
        if len(self.candidate_specs) > 20:
            raise ValueError("V2-3 candidate pool cannot exceed 20")
        if self.selection_lookback_dates < 60:
            raise ValueError("forecast selection lookback must be at least 60 dates")

    @staticmethod
    def _minimum(scope: ForecastScope) -> int:
        return 80 if scope is ForecastScope.STOCK else 200

    def _scope_samples(self, samples: tuple[ForecastTrainingSample, ...], scope: ForecastScope, scope_key: str, horizon: int) -> tuple[ForecastTrainingSample, ...]:
        """筛选 scope 样本；跨股票确认只允许观察到的点时证据。"""
        selected = tuple(sorted((sample for sample in samples if sample.horizon == horizon and sample.scope_membership.get(scope) == scope_key), key=lambda sample: (sample.origin_session_date, sample.instrument.stable_key)))
        if scope in {ForecastScope.INDUSTRY, ForecastScope.MARKET}:
            selected = tuple(sample for sample in selected if sample.evidence_mode is FeatureEvidenceMode.OBSERVED_SNAPSHOT)
        if scope in {ForecastScope.INDUSTRY, ForecastScope.MARKET} and len({sample.instrument.stable_key for sample in selected}) < 5:
            return ()
        return selected

    @staticmethod
    def _panel_scope_samples(
        samples: tuple[ForecastTrainingSample, ...], *, market, horizon: int,
    ) -> tuple[ForecastTrainingSample, ...]:
        selected = tuple(sorted(
            (sample for sample in samples if sample.horizon == horizon and sample.instrument.market == market),
            key=lambda sample: (sample.origin_session_date, sample.instrument.stable_key),
        ))
        if len({sample.instrument.stable_key for sample in selected}) < 5:
            return ()
        return selected

    @classmethod
    def _selection_rank(cls, item, baseline_records, records):
        """Rank passers by the frozen primary metric, then diagnostics."""
        spec, evaluation = item
        return (
            evaluation.selection.multiclass_brier,
            evaluation.selection.log_loss,
            evaluation.selection.expected_calibration_error,
            cls._selection_stability_score(baseline_records, records[spec.spec_id]),
            spec.complexity_rank,
            spec.spec_id,
        )

    @staticmethod
    def _validated_empirical_baseline(
        selection: ForecastMetrics, confirmation: ForecastMetrics,
    ) -> bool:
        """Accept a simple stock prior only when it has absolute OOF skill.

        This is an independent gate against the uniform three-class forecast;
        it never relaxes the candidate-vs-baseline promotion requirements.
        """
        def acceptable(metrics: ForecastMetrics) -> bool:
            return (
                metrics.sample_count >= 30
                and metrics.multiclass_brier <= 0.66
                and metrics.log_loss <= 1.0986122886681098
                and metrics.expected_calibration_error <= 0.15
                and 0.65 <= metrics.interval_coverage <= 0.95
            )

        return acceptable(selection) and acceptable(confirmation)

    def evaluate(
        self, samples: tuple[ForecastTrainingSample, ...], *, scope: ForecastScope, scope_key: str,
        horizon: int, cancelled: Callable[[], bool] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        panel_samples: tuple[ForecastTrainingSample, ...] = (),
    ) -> TrainingOutcome:
        """评估预注册候选；只有 selection 唯一胜者可进入 confirmation。"""
        data = self._scope_samples(samples, scope, scope_key, horizon)
        minimum = self._minimum(scope)
        if len(data) <= minimum:
            return TrainingOutcome(scope, scope_key, horizon, ValidationStatus.INSUFFICIENT_SAMPLE, (), None, None, "not enough mature samples")
        if cancelled and cancelled():
            return TrainingOutcome(scope, scope_key, horizon, ValidationStatus.INSUFFICIENT_SAMPLE, (), None, None, "cancelled")
        panel_data = self._panel_scope_samples(
            panel_samples, market=data[0].instrument.market, horizon=horizon,
        )
        data_hash = stable_hash({
            "target": training_data_hash(data),
            "panel": training_data_hash(panel_data) if panel_data else None,
        })
        usable_specs = tuple(spec for spec in self.candidate_specs if extension_coverage(data, spec.feature_set_id) >= 0.60)
        if not usable_specs:
            return TrainingOutcome(scope, scope_key, horizon, ValidationStatus.INSUFFICIENT_SAMPLE, (), None, None, "no feature set meets coverage")
        # 同日横截面必须整体作为测试组移动；否则其他股票会泄漏同日未来环境。
        records: dict[str, list[tuple]] = {spec.spec_id: [] for spec in usable_specs}
        baseline_records: list[tuple] = []
        model_cache: dict[str, TrainedForecastModel | None] = {}
        eligible_date_index = 0
        dates = sorted({sample.origin_session_date for sample in data})
        for date_index, origin_date in enumerate(dates, start=1):
            if cancelled and cancelled():
                return TrainingOutcome(scope, scope_key, horizon, ValidationStatus.INSUFFICIENT_SAMPLE, (), None, None, "cancelled")
            if progress:
                progress("oof", date_index, len(dates))
            test_group = tuple(sample for sample in data if sample.origin_session_date == origin_date)
            train = tuple(sample for sample in data if sample.origin_session_date < origin_date and sample.target_session_date <= origin_date)
            panel_train = eligible_panel_training_samples(panel_data, origin_date)
            if len(train) < minimum:
                continue
            baseline = fit_calibrated_model(
                ModelSpec("empirical-baseline", ModelFamily.EMPIRICAL, "tech", {}, complexity_rank=0),
                train, random_seed=self.random_seed,
            )
            assert baseline is not None
            for item in test_group:
                probability, interval = predict_model(baseline, item.feature_snapshot)
                baseline_records.append((item, probability, interval))
            for spec in usable_specs:
                uses_panel = spec.hyperparameters.get("training_scope") == "market_panel"
                fit_samples = panel_train if uses_panel else train
                if uses_panel and len({item.instrument.stable_key for item in fit_samples}) < 5:
                    continue
                if eligible_date_index % OOF_REFIT_INTERVAL == 0 or spec.spec_id not in model_cache:
                    model_cache[spec.spec_id] = (
                        fit_panel_calibrated_model(
                            spec, fit_samples, train, random_seed=self.random_seed,
                        )
                        if uses_panel
                        else fit_calibrated_model(spec, fit_samples, random_seed=self.random_seed)
                    )
                model = model_cache[spec.spec_id]
                if model is None:
                    continue
                prediction_model = with_stock_return_history(model, train) if uses_panel else model
                group_records = []
                try:
                    for item in test_group:
                        probability, interval = predict_model(prediction_model, item.feature_snapshot)
                        group_records.append((item, probability, interval, prediction_model.temperature))
                except InsufficientRegimeSamples:
                    continue
                records[spec.spec_id].extend(group_records)
            eligible_date_index += 1
        if len(baseline_records) < 60:
            return TrainingOutcome(scope, scope_key, horizon, ValidationStatus.INSUFFICIENT_SAMPLE, (), None, None, "fewer than 60 OOF points")
        split = selection_split_index(baseline_records)
        if split < 30:
            return TrainingOutcome(scope, scope_key, horizon, ValidationStatus.INSUFFICIENT_SAMPLE, (), None, None, "selection needs 30 OOF points")
        selection_start = recent_selection_start(
            baseline_records, split, lookback_dates=self.selection_lookback_dates,
        )
        baseline_selection_records = baseline_records[selection_start:split]
        baseline_selection = self._metrics(baseline_selection_records, horizon, "baseline-selection", scope_key, data_hash)
        baseline_confirmation = self._metrics(baseline_records[split:], horizon, "baseline-confirmation", scope_key, data_hash)

        def validated_baseline_outcome(evaluations) -> TrainingOutcome | None:
            if scope is not ForecastScope.STOCK or not self._validated_empirical_baseline(
                baseline_selection, baseline_confirmation,
            ):
                return None
            spec = ModelSpec(
                "validated-empirical-stock", ModelFamily.EMPIRICAL, "tech", {},
                complexity_rank=0,
            )
            model = fit_calibrated_model(spec, data, random_seed=self.random_seed)
            if model is None:
                return None
            now = datetime.now(timezone.utc)
            version = ForecastModelVersion(
                version=f"{scope.value}:{scope_key}:{horizon}:{spec.spec_id}:{data_hash[:12]}",
                scope=scope, scope_key=scope_key, market=data[0].instrument.market,
                horizon=horizon, spec=spec, lifecycle=ModelLifecycle.CHAMPION,
                validation_status=ValidationStatus.NONINFERIOR_PASSED,
                training_start=data[0].origin_session_date,
                training_end=data[-1].origin_session_date,
                selection_start=baseline_records[selection_start][0].origin_session_date,
                selection_end=baseline_records[split - 1][0].origin_session_date,
                confirmation_start=baseline_records[split][0].origin_session_date,
                confirmation_end=baseline_records[-1][0].origin_session_date,
                training_data_hash=data_hash, artifact_format="json+zlib-v1",
                artifact_hash=model.artifact_hash, artifact=model.artifact_bytes(),
                random_seed=self.random_seed, sample_count=len(data),
                oof_sample_count=len(baseline_records), created_at=now,
                promoted_at=now,
            )
            baseline_evaluation = CandidateEvaluation(
                spec, ValidationStatus.NONINFERIOR_PASSED,
                baseline_selection, baseline_confirmation,
                baseline_selection, baseline_confirmation,
                len(baseline_records), 1.0,
            )
            return TrainingOutcome(
                scope, scope_key, horizon, ValidationStatus.NONINFERIOR_PASSED,
                tuple(evaluations) + (baseline_evaluation,), version, model,
                "empirical stock prior passed absolute OOF quality gates",
            )
        evaluations: list[CandidateEvaluation] = []
        passers: list[tuple[ModelSpec, CandidateEvaluation]] = []
        for spec in usable_specs:
            result = records[spec.spec_id]
            # A candidate must predict precisely the same OOF events as the baseline.
            if len(result) != len(baseline_records):
                evaluations.append(CandidateEvaluation(spec, ValidationStatus.INSUFFICIENT_SAMPLE, None, None, baseline_selection, baseline_confirmation, len(result)))
                continue
            selection_records = result[selection_start:split]
            selection = self._metrics(selection_records, horizon, spec.spec_id + ":selection", scope_key, data_hash)
            confirmation = self._metrics(result[split:], horizon, spec.spec_id + ":confirmation", scope_key, data_hash)
            strict_selection = self._selection_passes(selection, baseline_selection, baseline_selection_records, selection_records, horizon, scope_key, spec.spec_id, data_hash)
            noninferior_selection = self._noninferiority_passes(selection, baseline_selection, baseline_selection_records, selection_records, horizon, scope_key, spec.spec_id, data_hash, phase="selection")
            status = ValidationStatus.SELECTION_PASSED if strict_selection or noninferior_selection else self._failure_status(selection, baseline_selection)
            temperatures = tuple(float(item[3]) for item in result[:split])
            temperature = float(np.median(temperatures)) if temperatures else 1.0
            evaluation = CandidateEvaluation(spec, status, selection, confirmation, baseline_selection, baseline_confirmation, len(result), temperature)
            evaluations.append(evaluation)
            if status is ValidationStatus.SELECTION_PASSED:
                passers.append((spec, evaluation))
        if not passers:
            baseline_outcome = validated_baseline_outcome(evaluations)
            if baseline_outcome is not None:
                return baseline_outcome
            status = ValidationStatus.CALIBRATION_FAILED if any(item.status is ValidationStatus.CALIBRATION_FAILED for item in evaluations) else ValidationStatus.EVALUATED_NOT_BETTER
            return TrainingOutcome(scope, scope_key, horizon, status, tuple(evaluations), None, None, "no selection candidate passed")
        selection_records_by_spec = {
            spec.spec_id: records[spec.spec_id][selection_start:split] for spec, _ in passers
        }
        winner, selected = min(
            passers,
            key=lambda item: self._selection_rank(
                item, baseline_selection_records, selection_records_by_spec,
            ),
        )
        strict_confirmation = self._confirmation_passes(selected.confirmation, baseline_confirmation, baseline_records[split:], records[winner.spec_id][split:], horizon, scope_key, winner.spec_id, data_hash)
        noninferior_confirmation = self._noninferiority_passes(selected.confirmation, baseline_confirmation, baseline_records[split:], records[winner.spec_id][split:], horizon, scope_key, winner.spec_id, data_hash, phase="confirmation")
        if not strict_confirmation and not noninferior_confirmation:
            revised = [CandidateEvaluation(item.spec, ValidationStatus.EVALUATED_NOT_BETTER if item.spec != winner else self._failure_status(selected.confirmation, baseline_confirmation), item.selection, item.confirmation, item.baseline_selection, item.baseline_confirmation, item.oof_count, item.temperature) for item in evaluations]
            baseline_outcome = validated_baseline_outcome(revised)
            if baseline_outcome is not None:
                return baseline_outcome
            return TrainingOutcome(scope, scope_key, horizon, revised[[item.spec.spec_id for item in revised].index(winner.spec_id)].status, tuple(revised), None, None, "selection winner failed confirmation")
        validation_status = ValidationStatus.CONFIRMATION_PASSED if strict_confirmation else ValidationStatus.NONINFERIOR_PASSED
        winner_data = panel_data if winner.hyperparameters.get("training_scope") == "market_panel" else data
        final_model = (
            fit_panel_calibrated_model(
                winner, winner_data, data, random_seed=self.random_seed,
            )
            if winner.hyperparameters.get("training_scope") == "market_panel"
            else fit_calibrated_model(winner, winner_data, random_seed=self.random_seed)
        )
        if final_model is None:
            return TrainingOutcome(scope, scope_key, horizon, ValidationStatus.INSUFFICIENT_SAMPLE, tuple(evaluations), None, None, "winner cannot fit final data")
        training_hash = data_hash
        now = datetime.now(timezone.utc)
        version = ForecastModelVersion(
            version=f"{scope.value}:{scope_key}:{horizon}:{winner.spec_id}:{training_hash[:12]}", scope=scope, scope_key=scope_key,
            market=data[0].instrument.market, horizon=horizon, spec=winner, lifecycle=ModelLifecycle.CHAMPION,
            validation_status=validation_status, training_start=winner_data[0].origin_session_date,
            training_end=winner_data[-1].origin_session_date, selection_start=baseline_records[selection_start][0].origin_session_date,
            selection_end=baseline_records[split - 1][0].origin_session_date, confirmation_start=baseline_records[split][0].origin_session_date,
            confirmation_end=baseline_records[-1][0].origin_session_date, training_data_hash=training_hash,
            artifact_format="json+zlib-v1", artifact_hash=final_model.artifact_hash, artifact=final_model.artifact_bytes(),
            random_seed=self.random_seed, sample_count=len(winner_data), oof_sample_count=len(baseline_records),
            created_at=now, promoted_at=now,
        )
        revised = tuple(CandidateEvaluation(item.spec, validation_status if item.spec == winner else item.status, item.selection, item.confirmation, item.baseline_selection, item.baseline_confirmation, item.oof_count, final_model.temperature if item.spec == winner else item.temperature) for item in evaluations)
        return TrainingOutcome(scope, scope_key, horizon, validation_status, revised, version, final_model, None)

    def _metrics(self, records: list[tuple], horizon: int, phase: str, scope_key: str, data_hash: str) -> ForecastMetrics:
        """计算一个 OOF 片段指标，seed 从稳定身份派生而非运行时随机数。"""
        from hashlib import sha256
        seed = int.from_bytes(sha256(f"{scope_key}|{horizon}|{phase}|{data_hash}".encode()).digest()[:8], "big")
        return evaluate_predictions(tuple(item[1] for item in records), tuple(item[0].direction for item in records), tuple(item[0].future_return for item in records), tuple(item[2] for item in records), horizon=horizon, seed=seed)

    @staticmethod
    def _selection_passes(candidate: ForecastMetrics, baseline: ForecastMetrics, baseline_records: list[tuple], records: list[tuple], horizon: int, scope_key: str, spec_id: str, data_hash: str) -> bool:
        """执行冻结 selection 护栏：候选必须显著优于同期经验基线。"""
        from hashlib import sha256
        seed = int.from_bytes(sha256(f"{scope_key}|{horizon}|{spec_id}|selection|{data_hash}".encode()).digest()[:8], "big")
        _, lower80, _, _, _ = paired_brier_improvement_interval(tuple(item[1] for item in baseline_records), tuple(item[1] for item in records), tuple(item[0].direction for item in records), horizon=horizon, seed=seed)
        return candidate.sample_count >= 30 and candidate.multiclass_brier < baseline.multiclass_brier - max(0.005, baseline.multiclass_brier * .01) and candidate.log_loss <= baseline.log_loss * 1.02 and candidate.expected_calibration_error <= max(.15, baseline.expected_calibration_error + .03) and .65 <= candidate.interval_coverage <= .95 and lower80 >= -.002

    @staticmethod
    def _failure_status(candidate: ForecastMetrics, baseline: ForecastMetrics) -> ValidationStatus:
        if candidate.log_loss > baseline.log_loss * 1.02 or candidate.expected_calibration_error > max(.15, baseline.expected_calibration_error + .03) or not .65 <= candidate.interval_coverage <= .95:
            return ValidationStatus.CALIBRATION_FAILED
        return ValidationStatus.EVALUATED_NOT_BETTER

    @staticmethod
    def _selection_stability_score(baseline_records: list[tuple], records: list[tuple]) -> float:
        """Worst chronological-block Brier degradation inside selection only."""
        if len(records) != len(baseline_records) or not records:
            return float("inf")
        differences = []
        for base, candidate in zip(baseline_records, records):
            label = base[0].direction
            actual = np.zeros(3, dtype=float)
            actual[("bullish", "neutral", "bearish").index(label.value)] = 1.0
            base_vector = np.asarray((base[1].bullish, base[1].neutral, base[1].bearish), dtype=float)
            candidate_vector = np.asarray((candidate[1].bullish, candidate[1].neutral, candidate[1].bearish), dtype=float)
            differences.append(float(np.square(candidate_vector - actual).sum() - np.square(base_vector - actual).sum()))
        blocks = tuple(block for block in np.array_split(np.asarray(differences), 3) if len(block))
        return max(float(np.mean(block)) for block in blocks)

    @staticmethod
    def _noninferiority_passes(candidate: ForecastMetrics | None, baseline: ForecastMetrics, baseline_records: list[tuple], records: list[tuple], horizon: int, scope_key: str, spec_id: str, data_hash: str, *, phase: str) -> bool:
        """Admit a calibrated conditional model that is not materially worse.

        This is deliberately a second tier: it does not claim alpha over the
        empirical prior. It only proves that conditional probabilities remain
        calibrated on untouched data while adding a non-trivial state signal.
        """
        if candidate is None or candidate.sample_count < 30:
            return False
        if candidate.multiclass_brier > baseline.multiclass_brier + .01 or candidate.log_loss > baseline.log_loss * 1.02:
            return False
        if candidate.expected_calibration_error > max(.15, baseline.expected_calibration_error + .03) or not .65 <= candidate.interval_coverage <= .95:
            return False
        from hashlib import sha256
        seed = int.from_bytes(sha256(f"{scope_key}|{horizon}|{spec_id}|{phase}|noninferior|{data_hash}".encode()).digest()[:8], "big")
        _, lower80, _, _, _ = paired_brier_improvement_interval(tuple(item[1] for item in baseline_records), tuple(item[1] for item in records), tuple(item[0].direction for item in records), horizon=horizon, seed=seed)
        baseline_vectors = np.asarray([(item[1].bullish, item[1].neutral, item[1].bearish) for item in baseline_records], dtype=float)
        candidate_vectors = np.asarray([(item[1].bullish, item[1].neutral, item[1].bearish) for item in records], dtype=float)
        conditional_shift = float(np.mean(np.abs(candidate_vectors - baseline_vectors).sum(axis=1)))
        # This is a B-tier non-inferiority margin, not an alpha claim. The
        # point estimate still cannot trail baseline by more than 0.01 Brier;
        # the wider block-bootstrap bound acknowledges serial dependence in a
        # few hundred daily observations while keeping materially unstable
        # candidates out.
        return lower80 >= -.02 and conditional_shift >= .02

    def _confirmation_passes(self, candidate: ForecastMetrics | None, baseline: ForecastMetrics, baseline_records: list[tuple], records: list[tuple], horizon: int, scope_key: str, spec_id: str, data_hash: str) -> bool:
        """在从未参与选模的 confirmation 段确认唯一 selection 胜者。"""
        if candidate is None or candidate.sample_count < 30 or candidate.multiclass_brier >= baseline.multiclass_brier or candidate.log_loss > baseline.log_loss * 1.02 or candidate.expected_calibration_error > max(.15, baseline.expected_calibration_error + .03) or not .65 <= candidate.interval_coverage <= .95:
            return False
        from hashlib import sha256
        seed = int.from_bytes(sha256(f"{scope_key}|{horizon}|{spec_id}|confirmation|{data_hash}".encode()).digest()[:8], "big")
        _, lower80, _, _, _ = paired_brier_improvement_interval(tuple(item[1] for item in baseline_records), tuple(item[1] for item in records), tuple(item[0].direction for item in records), horizon=horizon, seed=seed)
        relative = (baseline.multiclass_brier - candidate.multiclass_brier) / baseline.multiclass_brier
        evidence = lower80 > 0 or (relative >= .05 and lower80 >= -.002)
        return evidence and len({item[0].direction for item in records}) >= 2
