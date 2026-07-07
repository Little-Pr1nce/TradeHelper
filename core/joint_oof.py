"""Nested out-of-sample replay for the final forecast/strategy/risk policy.

Each fold selects forecast parameters and strategy candidates using only the
fold's training prefix.  The frozen policy is then executed on the following
test sessions through the same StrategyDecision -> Order -> Broker path used
by normal backtests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np
import pandas as pd

from backtest.analytics import compute_metrics
from backtest.engine import BacktestConfig, BacktestEngine
from core.data_quality import evaluate_data_quality
from core.forecast_engine import (
    forecast_candidate_passes_baseline,
    evaluate_forecast_candidates,
    generate_oof_forecast_snapshot,
    multiclass_log_loss,
    probability_diagnostics,
)
from core.signal_check import (
    build_forecast_consensus,
    generate_operation_plan,
    run_signal_check,
    select_actionable_sell_signals,
    select_signal_family_representatives,
)
from core.strategy_pool import StrategyVariant, generate_variants
from strategies import get_execution_strategy, get_overlay_strategy_keys
from strategies.base import (
    BaseExecutionStrategy,
    StrategyContext,
    StrategyDecision,
    market_lot_size,
    round_lot_shares,
)


POLICY_VERSION = "joint_oof_v4_embargo_coherent"
DEFAULT_STRATEGY_KEYS = (
    "A", "B", "C", "D", "E", "F", "G", "H",
    "I", "J", "K", "L", "M", "N",
)


@dataclass
class JointOOFFold:
    train_end: int
    test_start: int
    test_end: int
    strategy_specs: list[dict]
    forecast_configs: dict[int, dict]
    audit_entries: list = field(default_factory=list)


@dataclass
class JointOOFResult:
    code: str
    market: str
    data_start: str
    data_end: str
    policy_version: str = POLICY_VERSION
    samples: int = 0
    actionable_signals: int = 0
    forecast_gate_active: int = 0
    total_return: float = 0.0
    annual_return: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    forecast_brier: float = 0.0
    forecast_log_loss: float = 0.0
    forecast_ece: float = 0.0
    horizon_metrics: dict = field(default_factory=dict)
    calibration_bins: list[dict] = field(default_factory=list)
    regime_metrics: dict = field(default_factory=dict)
    fold_summaries: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


def run_joint_oof_replay(
    df: pd.DataFrame,
    *,
    code: str,
    market: str,
    strategy_keys: list[str] | None = None,
    initial_capital: float = 100000.0,
    min_train: int = 120,
    test_size: int = 20,
    n_splits: int = 3,
) -> JointOOFResult | None:
    """Replay the final policy on sequential held-out folds.

    Strategy selection, forecast parameter selection and forecast labels all
    stop at each fold's training boundary.  The test folds are contiguous and
    the account state carries forward across them.
    """
    if df is None or "Final_Score" not in df.columns:
        return None
    work = df.reset_index(drop=True).copy()
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(work.columns):
        return None
    folds = _build_folds(
        work, market=market, strategy_keys=strategy_keys,
        initial_capital=initial_capital, min_train=min_train,
        test_size=test_size, n_splits=n_splits,
    )
    if not folds:
        return None

    policy = _JointOOFPolicy(code=code, market=market, folds=folds)
    engine = BacktestEngine(BacktestConfig(initial_capital=initial_capital))
    raw = engine.run(work, policy)
    _annotate_broker_outcomes(policy.trace, raw.fills)
    first_test = folds[0].test_start
    first_test_date = str(work.iloc[first_test]["date"])[:10]
    test_equity = [
        item for item in raw.equity_curve
        if str(item.get("date", ""))[:10] >= first_test_date
    ]
    benchmark = (
        float(work["close"].iloc[-1]) / float(work["close"].iloc[first_test]) - 1.0
        if float(work["close"].iloc[first_test]) > 0 else 0.0
    )
    metrics = compute_metrics(
        test_equity, raw.trades, initial_capital,
        benchmark_return=benchmark, trading_days=max(len(test_equity), 1),
    )
    forecast_metrics = _forecast_trace_metrics(policy.trace, work)
    fold_summaries = [
        {
            "train_end": str(work.iloc[fold.train_end]["date"])[:10],
            "test_start": str(work.iloc[fold.test_start]["date"])[:10],
            "test_end": str(work.iloc[fold.test_end]["date"])[:10],
            "strategy_specs": list(fold.strategy_specs),
            "forecast_configs": {
                str(horizon): dict(config)
                for horizon, config in fold.forecast_configs.items()
            },
        }
        for fold in folds
    ]
    return JointOOFResult(
        code=code.upper(), market=market,
        data_start=first_test_date,
        data_end=str(work.iloc[folds[-1].test_end]["date"])[:10],
        samples=len(policy.trace),
        actionable_signals=sum(bool(row.get("actionable")) for row in policy.trace),
        forecast_gate_active=sum(bool(row.get("forecast_validated")) for row in policy.trace),
        total_return=float(metrics["total_return"]),
        annual_return=float(metrics["annual_return"]),
        benchmark_return=float(metrics["benchmark_return"]),
        excess_return=float(metrics["excess_return"]),
        max_drawdown=float(metrics["max_drawdown"]),
        sharpe_ratio=float(metrics["sharpe_ratio"]),
        win_rate=float(metrics["win_rate"]),
        total_trades=int(raw.total_trades),
        forecast_brier=float(forecast_metrics["brier_score"]),
        forecast_log_loss=float(forecast_metrics["log_loss"]),
        forecast_ece=float(forecast_metrics["ece"]),
        horizon_metrics=forecast_metrics["horizon_metrics"],
        calibration_bins=forecast_metrics["calibration_bins"],
        regime_metrics=forecast_metrics["regime_metrics"],
        fold_summaries=fold_summaries,
        trace=policy.trace,
    )


def _annotate_broker_outcomes(trace: list[dict], fills: list) -> None:
    """Separate a submitted decision from what the Broker actually filled."""
    fills_by_order: dict[tuple[str, str], list] = {}
    for fill in fills or []:
        key = (
            str(getattr(fill, "order_date", "") or "")[:10],
            str(getattr(fill, "action", "") or ""),
        )
        fills_by_order.setdefault(key, []).append(fill)

    for event in trace:
        action = str(event.get("action") or "watch")
        if action not in ("buy", "sell"):
            event["broker_status"] = "not_submitted"
            event["executed_action"] = ""
            continue
        matched = fills_by_order.get((str(event.get("date") or "")[:10], action), [])
        if not matched:
            event["broker_status"] = "rejected"
            event["executed_action"] = ""
            continue
        event["broker_status"] = "filled"
        event["executed_action"] = action
        event["executed_shares"] = sum(
            int(getattr(fill, "shares", 0) or 0) for fill in matched
        )
        event["fill_date"] = max(
            str(getattr(fill, "date", "") or "") for fill in matched
        )
        total_shares = event["executed_shares"]
        event["fill_price"] = (
            sum(
                float(getattr(fill, "price", 0.0) or 0.0)
                * int(getattr(fill, "shares", 0) or 0)
                for fill in matched
            ) / total_shares
            if total_shares > 0 else 0.0
        )


def _build_folds(
    df: pd.DataFrame,
    *,
    market: str,
    strategy_keys: list[str] | None,
    initial_capital: float,
    min_train: int,
    test_size: int,
    n_splits: int,
) -> list[JointOOFFold]:
    n = len(df)
    test_size = max(int(test_size), 5)
    possible = max((n - int(min_train) - 1) // test_size, 0)
    split_count = min(max(int(n_splits), 1), possible)
    if split_count <= 0:
        return []
    # The final row has no T+1 bar and cannot be a decision origin.
    first_start = (n - 1) - split_count * test_size
    candidate_keys = list(strategy_keys or DEFAULT_STRATEGY_KEYS)
    folds = []
    for split in range(split_count):
        test_start = first_start + split * test_size
        test_end = min(test_start + test_size - 1, n - 2)
        train_end = test_start - 1
        train = df.iloc[:train_end + 1].copy()
        selected, audits = _select_training_strategies(
            train, candidate_keys, initial_capital,
        )
        forecast_configs = _select_training_forecasts(train)
        folds.append(JointOOFFold(
            train_end=train_end, test_start=test_start, test_end=test_end,
            strategy_specs=selected, forecast_configs=forecast_configs,
            audit_entries=audits,
        ))
    return folds


def _select_training_strategies(
    train: pd.DataFrame,
    keys: list[str],
    initial_capital: float,
) -> tuple[list[dict], list]:
    engine = BacktestEngine(BacktestConfig(initial_capital=initial_capital))
    candidates = []
    for variant in generate_variants(keys, max_per_strategy=3):
        try:
            strategy = variant.strategy
            if not strategy.live_signal_enabled or strategy.overlay_scope:
                continue
            result = engine.run(train.copy(), strategy)
        except Exception:
            continue
        if _joint_strategy_eligible(result):
            candidates.append((variant, result))
    if not candidates:
        return [], []

    scored = _rank_joint_strategy_candidates(candidates)
    best_by_key = {}
    for item in scored:
        best_by_key.setdefault(item[1].base_key, item)
    scored = sorted(best_by_key.values(), key=lambda item: item[0], reverse=True)
    chosen = scored[:5]
    selected = [{
        "base_key": item[1].base_key,
        "variant_label": item[1].variant_label,
        "params": dict(item[1].params),
    } for item in chosen]
    audits = []
    for _, variant, result in chosen:
        verdict = _joint_training_verdict(result)
        audits.append(SimpleNamespace(
            strategy_key=variant.variant_label, strategy_name=result.strategy_name,
            verdict=verdict, test_sharpe=float(result.sharpe_ratio),
        ))
    return selected, audits


def _joint_strategy_eligible(result) -> bool:
    """Hard gate used before a strategy can enter one joint-OOF fold."""
    return bool(
        int(result.total_trades) >= 2
        and float(result.total_return) > 0
        and float(result.sharpe_ratio) > 0
        and float(result.max_drawdown) <= 0.35
    )


def _joint_training_verdict(result) -> str:
    """Keep the fold's PASS label consistent with its risk qualification."""
    return "PASS" if (
        int(result.total_trades) >= 3
        and float(result.total_return) >= 0.02
        and float(result.sharpe_ratio) >= 0.50
        and float(result.max_drawdown) <= 0.30
        and float(result.win_rate) >= 0.45
    ) else "CONDITIONAL"


def _rank_joint_strategy_candidates(candidates: list[tuple]) -> list[tuple]:
    """Rank eligible candidates by scale-free risk-adjusted percentiles."""
    if not candidates:
        return []

    def _finite(value: float, default: float = 0.0) -> float:
        value = float(value)
        return value if np.isfinite(value) else default

    returns = pd.Series([_finite(item[1].total_return) for item in candidates])
    sharpes = pd.Series([_finite(item[1].sharpe_ratio) for item in candidates])
    calmars = pd.Series([_finite(item[1].calmar_ratio) for item in candidates])
    drawdowns = pd.Series([_finite(item[1].max_drawdown, 1.0) for item in candidates])
    return_rank = returns.rank(method="average", pct=True)
    sharpe_rank = sharpes.rank(method="average", pct=True)
    calmar_rank = calmars.rank(method="average", pct=True)
    drawdown_rank = (-drawdowns).rank(method="average", pct=True)

    ranked = []
    for index, (variant, result) in enumerate(candidates):
        score = (
            0.35 * float(return_rank.iloc[index])
            + 0.30 * float(sharpe_rank.iloc[index])
            + 0.20 * float(calmar_rank.iloc[index])
            + 0.15 * float(drawdown_rank.iloc[index])
        )
        ranked.append((score, variant, result))
    return sorted(ranked, key=lambda item: item[0], reverse=True)


def _select_training_forecasts(train: pd.DataFrame) -> dict[int, dict]:
    configs = {}
    for horizon in (1, 3, 5):
        candidates = evaluate_forecast_candidates(
            train, horizon=horizon, max_evaluations=40,
        )
        if not candidates:
            configs[horizon] = {
                "model_type": "analog", "neighbor_count": 80,
                "flat_threshold": 0.01, "validated": False,
            }
            continue
        best = candidates[0]
        validated = forecast_candidate_passes_baseline(best)
        configs[horizon] = {
            **dict(best["params"]),
            "validated": validated,
            "selection_brier": float(best["brier_score"]),
            "baseline_brier": float(best["baseline_brier"]),
            "selection_log_loss": float(best["log_loss"]),
            "selection_ece": float(best["ece"]),
        }
    return configs


class _JointOOFPolicy(BaseExecutionStrategy):
    def __init__(self, *, code: str, market: str, folds: list[JointOOFFold]):
        self.code = code.upper()
        self.market = market
        self.folds = folds
        self.trace: list[dict] = []
        self._last_position_shares = 0
        self._pending_entry_key = ""
        self._entry_strategy_key = ""
        self._entry_equity = 0.0
        self._entry_position_value = 0.0
        self._health_returns: dict[str, list[float]] = {}

    @property
    def name(self) -> str:
        return "联合OOF最终策略"

    @property
    def description(self) -> str:
        return "预测、策略和风控官在历史测试折上的联合重放"

    def diagnose_no_signal(self, df, context) -> list[str]:
        return ["联合策略在当前OOF测试点未形成A/B级可执行共识"]

    def generate_decision(
        self, df: pd.DataFrame, context: StrategyContext,
    ) -> StrategyDecision:
        self._refresh_realized_health(context)
        origin = len(df) - 1
        fold = next(
            (item for item in self.folds if item.test_start <= origin <= item.test_end),
            None,
        )
        if fold is None:
            return self._watch(df)
        forecasts = []
        for horizon, config in fold.forecast_configs.items():
            forecast = generate_oof_forecast_snapshot(
                df, code=self.code, market=self.market, horizon=horizon,
                neighbor_count=int(config.get("neighbor_count", 80)),
                model_config=config,
                validated=bool(config.get("validated", False)),
                model_version=(
                    f"{POLICY_VERSION}_h{horizon}_"
                    f"{config.get('model_type', 'analog')}"
                ),
            )
            if forecast is not None:
                forecasts.append(forecast)
        variants = []
        for spec in fold.strategy_specs:
            try:
                strategy = get_execution_strategy(spec["base_key"], **spec["params"])
                variants.append(StrategyVariant(
                    spec["base_key"], spec["variant_label"], strategy,
                    dict(spec["params"]), False,
                ))
            except Exception:
                continue
        existing = {item.base_key for item in variants}
        for key in get_overlay_strategy_keys(has_position=context.position.shares > 0):
            if key in existing:
                continue
            try:
                variants.append(StrategyVariant(
                    key, key, get_execution_strategy(key), {}, True,
                ))
            except Exception:
                continue
        quality = evaluate_data_quality(df, market=self.market).to_dict()
        ranked, _ = run_signal_check(
            df=df, variants=variants, market=self.market,
            audit_entries=fold.audit_entries,
            current_price=float(df["close"].iloc[-1]),
            final_score=float(df["Final_Score"].iloc[-1]),
            account_equity=context.equity, account_cash=context.cash,
            current_position=context.position, holding_days=context.holding_days,
            health_data=self._health_data(), data_quality=quality, forecasts=forecasts,
        )
        consensus = build_forecast_consensus(forecasts)
        decision = self._select_decision(df, context, ranked, quality, consensus)
        forecast_trace = {
            str(forecast.horizon): {
                "probabilities": [forecast.prob_up, forecast.prob_flat, forecast.prob_down],
                "direction": forecast.direction,
                "confidence": float(forecast.confidence),
                "market_regime": forecast.market_regime,
            }
            for forecast in forecasts
        }
        self.trace.append({
            "origin": origin,
            "date": context.date,
            "action": decision.action,
            "execution_level": decision.execution_level,
            "source": decision.source,
            "actionable": decision.action in ("buy", "sell"),
            "forecast_validated": bool(consensus["validated_horizons"]),
            "forecast_direction": consensus["direction"],
            "forecast_confidence": float(consensus["confidence"]),
            "forecast_conflict": bool(consensus["conflict"]),
            "validated_horizons": list(consensus["validated_horizons"]),
            "forecasts": forecast_trace,
        })
        return decision

    def _refresh_realized_health(self, context: StrategyContext) -> None:
        current_shares = int(context.position.shares or 0)
        if self._last_position_shares <= 0 and current_shares > 0:
            self._entry_strategy_key = self._pending_entry_key
            self._entry_position_value = (
                float(context.position.avg_cost or context.position.entry_price or 0.0)
                * current_shares
            )
            self._pending_entry_key = ""
        elif self._last_position_shares > 0 and current_shares <= 0:
            if self._entry_strategy_key and self._entry_position_value > 0:
                trade_return = (
                    float(context.equity) - self._entry_equity
                ) / self._entry_position_value
                self._health_returns.setdefault(self._entry_strategy_key, []).append(
                    float(trade_return)
                )
            self._entry_strategy_key = ""
            self._entry_equity = 0.0
            self._entry_position_value = 0.0
        elif self._last_position_shares <= 0 and current_shares <= 0:
            # The prior buy order was rejected by the broker.
            self._pending_entry_key = ""
        self._last_position_shares = current_shares

    def _health_data(self) -> list[dict]:
        rows = []
        for key, returns in self._health_returns.items():
            total = len(returns)
            if total < 3:
                continue
            wins = sum(value > 0 for value in returns)
            accuracy = wins / total
            average = float(np.mean(returns))
            recent = returns[-5:]
            recent_accuracy = sum(value > 0 for value in recent) / len(recent)
            if total < 5:
                action, status, note = "watch", "unstable", "OOF历史样本不足"
            elif average < -0.02 or recent_accuracy < 0.30:
                action, status, note = "demote", "unreliable", "OOF历史净收益为负"
            elif accuracy >= 0.60 and recent_accuracy >= 0.50 and average >= 0:
                action, status, note = "keep", "reliable", ""
            else:
                action, status, note = "watch", "unstable", "OOF表现尚未稳定"
            rows.append({
                "strategy_name": key, "signal_action": "buy", "total": total,
                "accuracy": accuracy, "recent_accuracy": recent_accuracy,
                "confidence_lower_95": _wilson_lower_bound(wins, total),
                "avg_return": average, "sample_status": (
                    "insufficient" if total < 5 else "thin" if total < 8 else "ok"
                ),
                "status": status, "action": action, "risk_note": note,
            })
        return rows

    def _select_decision(self, df, context, ranked, quality, consensus) -> StrategyDecision:
        if context.position.shares > 0:
            sells = [
                signal for signal in select_actionable_sell_signals(ranked)
                if signal.execution_level in ("A", "B")
            ]
            if sells:
                signal = sells[0]
                shares = context.position.shares
                if 0 < signal.position_pct < 1 and context.equity > 0:
                    proposed = round_lot_shares(
                        signal.position_pct * context.equity / max(signal.entry_price, 1e-9),
                        self.market,
                    )
                    shares = min(shares, proposed or shares)
                return StrategyDecision(
                    action="sell", signal_intent=signal.signal_intent,
                    execution_level=signal.execution_level, shares=shares,
                    trigger_price=signal.entry_price,
                    reason=signal.reason, source=signal.base_key,
                )
            return self._watch(df, holding=True)

        buys = select_signal_family_representatives([
            signal for signal in ranked
            if signal.signal == "buy" and signal.execution_level in ("A", "B")
        ])
        if not buys:
            return self._watch(df)
        bias = (
            consensus["direction"]
            if consensus["direction"] in ("bullish", "bearish", "neutral")
            else "neutral"
        )
        plan = generate_operation_plan(
            buys, float(df["close"].iloc[-1]), bias, df,
            account_equity=context.equity, data_quality=quality,
            market=self.market, current_position=context.position,
        )
        selected = plan.conservative
        if not selected or float(selected.get("position_pct", 0.0) or 0.0) <= 0:
            return self._watch(df)
        pass_signals = [signal for signal in buys if signal.audit_verdict == "PASS"]
        signal = pass_signals[0] if pass_signals else buys[0]
        price = float(selected["entry"])
        budget = min(context.cash, context.equity * float(selected["position_pct"]))
        raw_shares = budget / price if price > 0 else 0.0
        shares = (
            round_lot_shares(raw_shares, self.market)
            if raw_shares >= market_lot_size(self.market) else 0
        )
        if shares <= 0:
            return self._watch(df)
        self._pending_entry_key = signal.base_key
        self._entry_equity = float(context.equity)
        return StrategyDecision(
            action="buy", signal_intent="alpha_entry",
            execution_level=signal.execution_level, shares=shares,
            trigger_price=price, stop_loss=float(selected["stop_loss"]),
            take_profit=float(selected.get("take_profit", 0.0) or 0.0),
            take_profit_mode=str(selected.get("take_profit_mode", "none")),
            take_profit_rule=str(selected.get("take_profit_rule", "")),
            position_pct=float(selected["position_pct"]),
            max_loss_amount=float(selected.get("max_loss_amount", 0.0) or 0.0),
            invalidation=str(selected.get("invalidation", "")),
            reason=signal.reason, source=signal.base_key,
        )

    def _watch(self, df, holding: bool = False) -> StrategyDecision:
        return StrategyDecision(
            action="hold" if holding else "watch", execution_level="C",
            trigger_price=float(df["close"].iloc[-1]) if len(df) else 0.0,
            reason="联合OOF测试点无可执行共识", source=POLICY_VERSION,
        )


def _forecast_trace_metrics(trace: list[dict], df: pd.DataFrame) -> dict:
    probability_rows, actual_rows, regime_rows = [], [], []
    grouped: dict[int, dict[str, list]] = {}
    for row in trace:
        origin = int(row.get("origin", -1))
        for horizon_text, forecast in (row.get("forecasts") or {}).items():
            horizon = int(horizon_text)
            probs = forecast.get("probabilities") or []
            if origin < 0 or origin + horizon >= len(df) or len(probs) != 3:
                continue
            current = float(df["close"].iloc[origin])
            future = float(df["close"].iloc[origin + horizon])
            if current <= 0 or future <= 0:
                continue
            ret = future / current - 1.0
            label = 0 if ret > 0.01 else 2 if ret < -0.01 else 1
            actual_direction = ("bullish", "neutral", "bearish")[label]
            forecast["actual_return"] = float(ret)
            forecast["actual_direction"] = actual_direction
            forecast["target_date"] = str(df["date"].iloc[origin + horizon])[:10]
            forecast["correct"] = int(
                int(np.asarray(probs, dtype=float).argmax()) == label
            )
            regime = str(forecast.get("market_regime") or "unknown")
            probability_rows.append(probs)
            actual_rows.append(label)
            regime_rows.append(regime)
            bucket = grouped.setdefault(
                horizon, {"probabilities": [], "actual": [], "regimes": []}
            )
            bucket["probabilities"].append(probs)
            bucket["actual"].append(label)
            bucket["regimes"].append(regime)
    diagnostics = _probability_metric_summary(
        probability_rows, actual_rows, regime_rows,
    )
    horizon_metrics = {
        str(horizon): _probability_metric_summary(
            values["probabilities"], values["actual"], values["regimes"],
        )
        for horizon, values in sorted(grouped.items())
    }
    return {
        "brier_score": diagnostics["brier_score"],
        "log_loss": diagnostics["log_loss"],
        "ece": float(diagnostics["ece"]),
        "calibration_bins": diagnostics["calibration_bins"],
        "regime_metrics": diagnostics["regime_metrics"],
        "horizon_metrics": horizon_metrics,
    }


def _probability_metric_summary(probabilities, actual, regimes) -> dict:
    diagnostics = probability_diagnostics(probabilities, actual, regimes=regimes)
    briers, log_losses = [], []
    for probs, label in zip(probabilities, actual):
        values = np.asarray(probs, dtype=float)
        outcome = np.zeros(3)
        outcome[int(label)] = 1.0
        briers.append(float(((values - outcome) ** 2).sum()))
        log_losses.append(multiclass_log_loss(values, int(label)))
    return {
        "samples": len(briers),
        "brier_score": float(np.mean(briers)) if briers else 0.0,
        "log_loss": float(np.mean(log_losses)) if log_losses else 0.0,
        "ece": float(diagnostics["ece"]),
        "calibration_bins": diagnostics["calibration_bins"],
        "regime_metrics": diagnostics["regime_metrics"],
    }


def _wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = proportion + z * z / (2.0 * total)
    margin = z * np.sqrt(
        (proportion * (1.0 - proportion) + z * z / (4.0 * total)) / total
    )
    return float(max(0.0, (centre - margin) / denominator))
