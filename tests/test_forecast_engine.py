"""独立预测、到期验证和交易方案分账测试。"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.forecast_engine import (
    LOGISTIC_SOLVER_VERSION,
    _coherent_return_distribution,
    _regularized_logistic_probabilities,
    _weighted_quantile,
    evaluate_forecast_candidates,
    forecast_candidate_passes_baseline,
    forecast_candidate_configs,
    forecast_model_params_compatible,
    generate_forecasts,
    generate_oof_forecast_snapshot,
    probability_diagnostics,
    paired_block_improvement,
)
from core.joint_oof import (
    _joint_strategy_eligible,
    _joint_training_verdict,
    _rank_joint_strategy_candidates,
    _select_training_strategies,
    run_joint_oof_replay,
)
from core.pipeline import run_pipeline
from data.database import Database
from data.models import ForecastModelVersion, ForecastResult, IntradayBar, PriceData
from report.prompts import build_forecast_section, build_forecast_tracking_section
from services.forecast_service import get_forecast_configs, persist_forecasts_and_plans
from services.forecast_service import optimize_forecast_models, persist_point_in_time_context
from utils.trading_calendar import TradingTargets, forecast_target_dates


def _fresh_db():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    return Database.init(os.path.join(tmpdir, "test.db"))


def _prices(count=150):
    dates = pd.date_range("2025-01-02", periods=count, freq="B")
    rows = []
    for i, date in enumerate(dates):
        close = 100 + i * 0.18 + ((i % 9) - 4) * 0.35
        rows.append({
            "date": date,
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1_000_000 + i * 1000,
            "code": "TEST",
        })
    return pd.DataFrame(rows)


def _targets():
    return TradingTargets(
        {1: "2025-08-01", 3: "2025-08-05", 5: "2025-08-07"},
        "test_calendar",
        True,
    )


def test_forecast_is_independent_probabilistic_and_deterministic():
    df = _prices()
    first = generate_forecasts(
        df, code="TEST", market="US", mode="eod", targets=_targets(),
    )
    second = generate_forecasts(
        df, code="TEST", market="US", mode="eod", targets=_targets(),
    )

    assert [item.horizon for item in first] == [1, 3, 5]
    assert [item.event_key for item in first] == [item.event_key for item in second]
    for item in first:
        assert abs(item.prob_up + item.prob_flat + item.prob_down - 1.0) < 1e-9
        assert item.direction in ("bullish", "neutral", "bearish")
        assert item.sample_count >= 30
        assert item.feature_snapshot_hash
        assert item.calendar_source == "test_calendar"
        assert item.confidence == 0.0
        assert item.model_version.endswith("_unvalidated")


def test_future_rows_cannot_change_a_past_forecast():
    full = _prices(170)
    cutoff = full.iloc[:130].copy()
    before = generate_forecasts(
        cutoff, code="TEST", market="US", mode="eod", targets=_targets(),
        generated_at="2026-07-03T10:00:00",
    )
    full.loc[130:, ["open", "high", "low", "close"]] *= 9.0
    after = generate_forecasts(
        full.iloc[:130], code="TEST", market="US", mode="eod", targets=_targets(),
        generated_at="2026-07-03T10:00:00",
    )

    assert [item.to_dict() for item in before] == [item.to_dict() for item in after]


def test_controlled_forecast_challengers_are_oof_and_future_safe():
    frame = _prices(180)
    candidates = evaluate_forecast_candidates(frame, horizon=3, max_evaluations=20)
    assert {item["params"]["model_type"] for item in candidates} == {
        "analog", "logistic", "tree", "ensemble",
    }
    assert {item["model_type"] for item in forecast_candidate_configs()} == {
        "analog", "logistic", "tree", "ensemble",
    }
    logistic = next(
        item["params"] for item in candidates
        if item["params"]["model_type"] == "logistic"
    )
    origin = frame.iloc[:150].copy()
    first = generate_oof_forecast_snapshot(
        origin, code="TEST", market="US", horizon=3,
        model_config=logistic, validated=True,
    )
    changed_future = frame.copy()
    changed_future.loc[150:, "close"] *= 5
    second = generate_oof_forecast_snapshot(
        changed_future.iloc[:150], code="TEST", market="US", horizon=3,
        model_config=logistic, validated=True,
    )
    assert first is not None and second is not None
    assert (first.prob_up, first.prob_flat, first.prob_down) == (
        second.prob_up, second.prob_flat, second.prob_down,
    )


def test_forecast_promotion_requires_paired_evidence_and_calibration():
    strong = paired_block_improvement(
        [0.30] * 40,
        [0.40] * 40,
        block_size=5,
    )
    assert strong["lower_90"] > 0
    candidate = {
        "samples": 40,
        "brier_score": 0.30,
        "baseline_brier": 0.40,
        "log_loss": 0.55,
        "baseline_log_loss": 0.65,
        "ece": 0.08,
        "baseline_ece": 0.07,
        "interval_coverage": 0.80,
        "brier_improvement_lower_90": strong["lower_90"],
    }
    assert forecast_candidate_passes_baseline(candidate) is True
    candidate["ece"] = 0.11
    assert forecast_candidate_passes_baseline(candidate) is False
    candidate["ece"] = 0.08
    candidate["interval_coverage"] = 0.65
    assert forecast_candidate_passes_baseline(candidate) is False


def test_forecast_selection_window_embargoes_unmatured_horizon_labels():
    db = _fresh_db()
    with patch(
        "core.forecast_engine.evaluate_forecast_candidates", return_value=[],
    ) as evaluate, patch(
        "core.joint_oof.run_joint_oof_replay", return_value=None,
    ):
        optimize_forecast_models(
            db, df=_prices(70), market="US", stock_code="TEST",
        )

    offsets = [call.kwargs["evaluation_end_offset"] for call in evaluate.call_args_list]
    assert offsets == [40, 0, 42, 0, 44, 0]


def test_probability_and_return_interval_share_one_weighted_distribution():
    probabilities, returns, weights = _coherent_return_distribution(
        np.array([0.70, 0.10, 0.20]),
        np.array([-0.20, -0.10, 0.00, 0.10, 0.20]),
        0.01,
    )
    p10, p50, p90 = _weighted_quantile(
        returns, [0.10, 0.50, 0.90], weights,
    )

    labels = np.where(returns > 0.01, 0, np.where(returns < -0.01, 2, 1))
    represented = np.array([weights[labels == index].sum() for index in range(3)])
    assert np.allclose(represented, probabilities)
    assert p10 <= p50 <= p90
    assert p50 > 0


def test_point_in_time_context_is_frozen_and_deduplicated_by_payload():
    db = _fresh_db()
    news = pd.DataFrame([{
        "date": "2026-07-01", "published_at": "2026-07-01T12:00:00",
        "source": "Reuters", "finbert_score": 0.4,
    }])
    fundamental = {
        "source": "finnhub", "style_factors": {"pe_percentile": 0.4},
        "fundamental_factors": {"roe": 0.2},
    }
    first = persist_point_in_time_context(
        db, code="AAPL", market="US", mode="eod",
        effective_date="2026-07-01", news_data=news,
        fundamental_data=fundamental, captured_at="2026-07-01T16:01:00",
    )
    second = persist_point_in_time_context(
        db, code="AAPL", market="US", mode="eod",
        effective_date="2026-07-01", news_data=news,
        fundamental_data=fundamental, captured_at="2026-07-01T16:05:00",
    )
    changed = dict(fundamental)
    changed["fundamental_factors"] = {"roe": 0.25}
    third = persist_point_in_time_context(
        db, code="AAPL", market="US", mode="eod",
        effective_date="2026-07-01", news_data=news,
        fundamental_data=changed, captured_at="2026-07-01T16:10:00",
    )
    assert first == second
    assert third != first
    snapshots = db.get_feature_context_snapshots("AAPL")
    assert len(snapshots) == 2
    assert snapshots[0].captured_at == "2026-07-01T16:10:00"
    assert snapshots[0].quality_status == "complete"
    assert db.get_feature_context_snapshots(
        "AAPL", before_at="2026-07-01T16:03:00",
    )[0].id == first


def test_oof_snapshot_uses_only_matured_labels():
    full = _prices(160)
    prefix = full.iloc[:130].copy()
    before = generate_oof_forecast_snapshot(
        prefix, code="TEST", market="US", validated=True,
    )
    full.loc[130:, ["open", "high", "low", "close"]] *= 5.0
    after = generate_oof_forecast_snapshot(
        full.iloc[:130], code="TEST", market="US", validated=True,
    )

    assert before is not None
    assert before.to_dict() == after.to_dict()


def test_probability_diagnostics_include_logical_calibration_and_regimes():
    diagnostics = probability_diagnostics(
        [[0.8, 0.1, 0.1], [0.6, 0.2, 0.2], [0.1, 0.2, 0.7]],
        [0, 1, 2],
        regimes=["ranging", "ranging", "trending_steady"],
    )

    assert 0 <= diagnostics["ece"] <= 1
    assert sum(item["count"] for item in diagnostics["calibration_bins"]) == 3
    assert diagnostics["regime_metrics"]["ranging"]["samples"] == 2
    assert diagnostics["regime_metrics"]["trending_steady"]["samples"] == 1


def test_weekday_fallback_is_explicitly_unreliable():
    targets = forecast_target_dates(
        "2026-07-03", "US", allow_weekday_fallback=True,
    )
    assert set(targets.dates) == {1, 3, 5}
    assert targets.reliable in (True, False)
    if targets.source == "weekday_fallback":
        assert targets.reliable is False
        assert targets.dates[1] == "2026-07-06"


def test_stale_daily_history_does_not_generate_retroactive_live_forecasts():
    frame = _prices(150)
    frame["date"] = pd.date_range("2025-12-04", periods=150, freq="B")
    result = generate_forecasts(
        frame,
        code="TEST",
        market="US",
        mode="eod",
        generated_at="2026-07-05T12:52:00+08:00",
    )

    assert result == []


def test_retroactive_forecast_is_quarantined_before_metrics():
    db = _fresh_db()
    db.insert_forecast(ForecastResult(
        code="SNDK", market="US", mode="eod",
        generated_at="2026-07-05T12:52:00+08:00",
        data_cutoff="2026-07-01", target_session_date="2026-07-02",
        horizon=1, reference_price=100.0,
        prob_up=0.53, prob_flat=0.13, prob_down=0.34,
        direction="bullish", event_key="retroactive-test",
        status="verified", actual_price=90.0, actual_return=-0.10,
        actual_direction="bearish", brier_score=0.74,
    ))

    db.verify_due_forecasts(as_of_date="2026-07-06")

    stored = db.get_forecasts(code="SNDK", limit=1)[0]
    assert stored.status == "unsupported"
    assert db.get_forecast_metrics(code="SNDK")["samples"] == 0


def test_forecast_from_stale_cutoff_is_quarantined_even_if_target_is_future():
    db = _fresh_db()
    db.insert_forecast(ForecastResult(
        code="SNDK", market="US", mode="eod",
        generated_at="2026-07-05T12:52:00+08:00",
        data_cutoff="2026-07-01", target_session_date="2026-07-07",
        horizon=3, reference_price=100.0,
        prob_up=0.53, prob_flat=0.13, prob_down=0.34,
        direction="bullish", event_key="stale-cutoff-test",
        status="pending",
    ))

    db.verify_due_forecasts(as_of_date="2026-07-06")

    assert db.get_forecasts(code="SNDK", limit=1)[0].status == "unsupported"


def test_database_freezes_forecast_and_verifies_exact_target_close():
    db = _fresh_db()
    forecast = generate_forecasts(
        _prices(), code="TEST", market="US", mode="eod", targets=_targets(),
        generated_at="2025-07-31T20:00:00+08:00",
    )[0]
    original_probability = forecast.prob_up
    first_id = db.insert_forecast(forecast)

    duplicate = generate_forecasts(
        _prices(), code="TEST", market="US", mode="eod", targets=_targets(),
        generated_at="2025-07-31T20:00:00+08:00",
    )[0]
    duplicate.prob_up, duplicate.prob_flat = duplicate.prob_flat, duplicate.prob_up
    second_id = db.insert_forecast(duplicate)
    stored = db.get_forecasts(code="TEST", limit=10)[0]

    assert first_id == second_id
    assert stored.prob_up == original_probability

    target_close = forecast.reference_price * 1.03
    db.insert_prices([PriceData(
        "TEST", forecast.target_session_date,
        target_close, target_close, target_close, target_close, 1000,
    )])
    verified = db.verify_due_forecasts(
        code="TEST", as_of_date=forecast.target_session_date,
    )
    metrics = db.get_forecast_metrics(code="TEST")

    assert len(verified) == 1
    assert verified[0].actual_direction == "bullish"
    assert verified[0].actual_price == target_close
    assert 0 <= verified[0].brier_score <= 2
    assert metrics["samples"] == 1
    assert 0 <= metrics["accuracy"] <= 1
    assert metrics["log_loss"] >= 0
    assert 0 <= metrics["ece"] <= 1
    assert metrics["calibration_bins"]


def test_joint_oof_replay_and_database_audit_are_separate_from_in_sample_backtest():
    from indicators.technical import calc_all_indicators

    df = calc_all_indicators(_prices(150))
    df["Final_Score"] = df["close"].pct_change(20).fillna(0.0).clip(-1, 1)
    result = run_joint_oof_replay(
        df, code="TEST", market="US", strategy_keys=["A", "G"],
        min_train=90, test_size=10, n_splits=2,
    )

    assert result is not None
    assert result.samples == 20
    assert result.data_start > str(df["date"].iloc[0])[:10]
    assert len(result.fold_summaries) == 2
    assert result.forecast_log_loss >= 0
    assert {"1", "3", "5"}.issubset(result.horizon_metrics)
    annotated = [
        forecast
        for event in result.trace
        for forecast in (event.get("forecasts") or {}).values()
        if "actual_direction" in forecast
    ]
    assert annotated
    assert all(item.get("target_date") and item.get("correct") in (0, 1) for item in annotated)
    assert all(event.get("broker_status") for event in result.trace)
    assert all(
        event["broker_status"] in ("filled", "rejected")
        for event in result.trace if event.get("actionable")
    )

    db = _fresh_db()
    first_id = db.save_joint_oof_run(result.to_dict())
    second_id = db.save_joint_oof_run(result.to_dict())
    rows = db.get_joint_oof_runs(code="TEST")

    assert first_id == second_id
    assert len(rows) == 1
    assert rows[0]["samples"] == 20
    assert isinstance(rows[0]["fold_summaries"], list)
    assert {"1", "3", "5"}.issubset(rows[0]["horizon_metrics"])

    previous = dict(result.to_dict())
    previous.update({"excess_return": 0.10, "forecast_brier": 0.40})
    db.save_joint_oof_run(previous)
    latest = dict(result.to_dict())
    latest.update({
        "data_end": "2099-12-31", "excess_return": 0.02,
        "forecast_brier": 0.50, "total_return": 0.05,
    })
    db.save_joint_oof_run(latest)
    health = db.get_joint_oof_health("TEST")
    assert health["drift_status"] == "warning"
    assert health["excess_return_delta"] <= -0.05
    assert health["forecast_brier_delta"] >= 0.03

    cross_version = dict(result.to_dict())
    cross_version.update({
        "data_end": "2100-01-01", "policy_version": "joint_oof_v4_test",
        "excess_return": -0.20, "forecast_brier": 0.90,
    })
    db.save_joint_oof_run(cross_version)
    version_health = db.get_joint_oof_health("TEST")
    assert version_health["policy_version"] == "joint_oof_v4_test"
    assert version_health["drift_status"] == "stable"
    assert "excess_return_delta" not in version_health


def test_forecast_and_trade_plan_are_persisted_and_deduplicated_separately():
    db = _fresh_db()
    forecasts = generate_forecasts(
        _prices(), code="TEST", market="US", mode="eod", targets=_targets(),
    )
    signals = [{
        "key": "A", "variant": "A_v1", "signal": "buy",
        "signal_intent": "alpha_entry", "execution_level": "B",
        "trigger_price": 120.0, "stop_loss": 114.0, "take_profit": 132.0,
        "position_pct": 0.1, "max_loss_amount": 600.0,
    }]
    first = persist_forecasts_and_plans(
        db, forecasts=forecasts, signals=signals, code="TEST", market="US", mode="eod",
    )
    second = persist_forecasts_and_plans(
        db, forecasts=forecasts, signals=signals, code="TEST", market="US", mode="eod",
    )

    assert first["forecast_ids"] == second["forecast_ids"]
    assert first["plan_ids"] == second["plan_ids"]
    assert db.execute("SELECT COUNT(*) n FROM forecast_log").fetchone()["n"] == 3
    assert db.execute("SELECT COUNT(*) n FROM trade_plan_log").fetchone()["n"] == 1
    assert db.get_forecast_champion("US", 1, "TEST") is None


def test_risk_exit_is_not_saved_as_a_directional_legacy_prediction():
    from services.analysis_service import AnalysisService

    pipeline = type("Pipeline", (), {"signal_check": [{
        "key": "Q", "signal": "sell", "signal_intent": "profit_lock",
        "execution_level": "A", "entry_price": 200.0,
    }]})()
    assert AnalysisService._prediction_signals(pipeline) == []


def test_forecast_report_states_target_date_and_separate_metrics():
    forecasts = generate_forecasts(
        _prices(), code="TEST", market="US", mode="eod", targets=_targets(),
    )
    current = build_forecast_section(forecasts)
    history = build_forecast_tracking_section([], {"samples": 0})

    assert "2025-08-01" in current
    assert "上涨" in current and "震荡" in current and "下跌" in current
    assert "Brier" in current
    assert "分离度=最高方向概率-第二高方向概率" in current
    assert "不是“80%概率赚到中位数”" in current
    assert "暂无到期" in history


def test_pipeline_generates_forecast_before_live_plan():
    result = run_pipeline(
        _prices(),
        market="US",
        stock_code="TEST",
        strategy_names=["P"],
        run_backtests=False,
        run_signals=True,
        expand_pool=False,
        forecast_targets=_targets(),
    )
    assert len(result.forecasts) == 3
    assert result.signal_check is not None
    assert result.decision_df is not None


def test_background_optimizer_uses_oof_and_keeps_a_champion():
    db = _fresh_db()
    forecasts = generate_forecasts(
        _prices(180), code="TEST", market="US", mode="eod", targets=_targets(),
    )
    persist_forecasts_and_plans(
        db, forecasts=forecasts, signals=[], code="TEST", market="US", mode="eod",
    )
    result = optimize_forecast_models(
        db, df=_prices(180), market="US", stock_code="TEST",
    )

    assert result["evaluated"] > 0
    for horizon in (1, 3, 5):
        champion = db.get_forecast_champion("US", horizon, "TEST")
        if champion is not None:
            assert champion.status == "champion"
            assert champion.stock_code == "TEST"
            assert champion.brier_score <= champion.baseline_brier * 0.98


def test_trade_plan_is_evaluated_separately_from_forecast_accuracy():
    db = _fresh_db()
    forecasts = generate_forecasts(
        _prices(), code="TEST", market="US", mode="eod", targets=_targets(),
    )
    persist_forecasts_and_plans(
        db,
        forecasts=forecasts,
        signals=[{
            "key": "A", "variant": "A", "signal": "buy",
            "signal_intent": "alpha_entry", "execution_level": "A",
            "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
            "position_pct": 0.1,
        }],
        code="TEST", market="US", mode="eod", reference_date="2026-07-02",
    )
    db.insert_prices([
        PriceData("TEST", "2026-07-06", 100, 103, 98, 102, 1000),
        PriceData("TEST", "2026-07-07", 102, 106, 101, 105, 1000),
        PriceData("TEST", "2026-07-08", 105, 111, 104, 110, 1000),
        PriceData("TEST", "2026-07-09", 110, 112, 108, 111, 1000),
        PriceData("TEST", "2026-07-10", 111, 113, 109, 112, 1000),
    ])

    assert db.verify_due_trade_plans(code="TEST") == 1
    metrics = db.get_trade_plan_metrics(code="TEST")
    assert metrics[0]["count"] == 1
    assert metrics[0]["avg_net_return"] > 0
    assert db.get_forecast_metrics(code="TEST")["samples"] == 0


def test_forecast_champions_are_isolated_by_stock():
    db = _fresh_db()
    for code, neighbors in (("AAPL", 40), ("AMD", 120)):
        version = f"forecast_v4_analog_n{neighbors}@{code}"
        db.save_forecast_model_version(ForecastModelVersion(
            stock_code=code, market="US", horizon=1, version=version,
            status="challenger", params_json=f'{{"neighbor_count": {neighbors}}}',
            created_at="2026-07-03T10:00:00",
        ))
        db.promote_forecast_model("US", 1, version, stock_code=code)

    assert get_forecast_configs(db, "US", "AAPL")[1]["neighbor_count"] == 40
    assert get_forecast_configs(db, "US", "AMD")[1]["neighbor_count"] == 120
    assert db.get_forecast_champion("US", 1, "AAPL").stock_code == "AAPL"


def test_legacy_forecast_champion_is_not_reused_under_v4_semantics():
    db = _fresh_db()
    version = "forecast_v3_analog_legacy@AAPL"
    db.save_forecast_model_version(ForecastModelVersion(
        stock_code="AAPL", market="US", horizon=1, version=version,
        status="challenger", params_json='{"model_type": "analog", "neighbor_count": 40}',
        created_at="2026-07-01T00:00:00",
    ))
    db.promote_forecast_model("US", 1, version, stock_code="AAPL")

    assert get_forecast_configs(db, "US", "AAPL") == {}
    assert db.get_forecast_champion("US", 1, "AAPL") is None


def test_legacy_logistic_solver_champion_requires_fresh_oof_validation():
    db = _fresh_db()
    version = "forecast_v4_logistic_legacy@AAPL"
    db.save_forecast_model_version(ForecastModelVersion(
        stock_code="AAPL", market="US", horizon=1, version=version,
        status="challenger",
        params_json='{"model_type": "logistic", "regularization": 0.2}',
        created_at="2026-07-01T00:00:00",
    ))
    db.promote_forecast_model("US", 1, version, stock_code="AAPL")

    assert not forecast_model_params_compatible({"model_type": "logistic"})
    assert get_forecast_configs(db, "US", "AAPL") == {}
    assert db.get_forecast_champion("US", 1, "AAPL") is None


def test_adaptive_logistic_solver_is_finite_deterministic_and_normalized():
    rng = np.random.default_rng(20260706)
    base = rng.normal(size=(120, 1))
    # Deliberately collinear and differently scaled features stress convergence.
    features = np.column_stack([
        base[:, 0], base[:, 0] * 1_000_000, base[:, 0] + 1e-10,
        rng.normal(scale=0.01, size=120),
    ])
    labels = np.where(base[:, 0] > 0.45, 0, np.where(base[:, 0] < -0.45, 2, 1))
    current = np.array([0.2, 200_000.0, 0.2, 0.0])

    first = _regularized_logistic_probabilities(
        features, labels, current, regularization=0.20,
    )
    second = _regularized_logistic_probabilities(
        features, labels, current, regularization=0.20,
    )

    assert np.isfinite(first).all()
    assert np.all(first > 0)
    assert np.isclose(first.sum(), 1.0)
    assert np.allclose(first, second)
    assert forecast_model_params_compatible({
        "model_type": "logistic", "solver_version": LOGISTIC_SOLVER_VERSION,
    })


def test_joint_oof_strategy_selection_enforces_risk_gate_and_stable_ranking():
    def result(**overrides):
        values = {
            "total_trades": 5, "total_return": 0.15, "sharpe_ratio": 1.2,
            "max_drawdown": 0.12, "calmar_ratio": 1.25, "win_rate": 0.55,
            "strategy_name": "test",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    unsafe = result(total_return=0.60, sharpe_ratio=2.0, max_drawdown=0.50)
    steady = result(total_return=0.18, sharpe_ratio=1.8, max_drawdown=0.05,
                    calmar_ratio=3.6)
    volatile = result(total_return=0.30, sharpe_ratio=0.6, max_drawdown=0.33,
                      calmar_ratio=0.9)
    steady_variant = SimpleNamespace(base_key="A", variant_label="A_steady", params={})
    volatile_variant = SimpleNamespace(base_key="B", variant_label="B_fast", params={})

    assert not _joint_strategy_eligible(unsafe)
    assert _joint_training_verdict(unsafe) == "CONDITIONAL"
    assert _joint_training_verdict(steady) == "PASS"
    ranked = _rank_joint_strategy_candidates([
        (volatile_variant, volatile), (steady_variant, steady),
    ])
    assert ranked[0][1].variant_label == "A_steady"

    candidate = SimpleNamespace(
        strategy=SimpleNamespace(live_signal_enabled=True, overlay_scope=""),
        base_key="A", variant_label="A_unsafe", params={},
    )
    with patch("core.joint_oof.generate_variants", return_value=[candidate]), patch(
        "core.joint_oof.BacktestEngine.run", return_value=unsafe,
    ):
        selected, audits = _select_training_strategies(_prices(80), ["A"], 100_000)
    assert selected == []
    assert audits == []


def test_trade_plan_deduplicates_without_forecast_and_intraday_waits_for_minute_evidence():
    db = _fresh_db()
    signal = [{
        "key": "A", "variant": "A", "signal": "buy",
        "signal_intent": "alpha_entry", "execution_level": "B",
        "entry_price": 100.0, "stop_loss": 95.0,
    }]
    first = persist_forecasts_and_plans(
        db, forecasts=[], signals=signal, code="TEST", market="US", mode="intraday",
    )
    second = persist_forecasts_and_plans(
        db, forecasts=[], signals=signal, code="TEST", market="US", mode="intraday",
    )
    row = db.execute("SELECT * FROM trade_plan_log").fetchone()

    assert first["plan_ids"] == second["plan_ids"]
    assert db.execute("SELECT COUNT(*) n FROM trade_plan_log").fetchone()["n"] == 1
    assert row["status"] == "pending_intraday"
    assert row["signal_timestamp_ms"] > 0
    assert db.verify_due_trade_plans(code="TEST") == 0


def test_trade_plan_versions_material_changes_and_uses_market_session_date():
    db = _fresh_db()
    signal_time = datetime(
        2026, 7, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"),
    )
    base = {
        "key": "A", "variant": "A", "signal": "buy",
        "signal_intent": "alpha_entry", "execution_level": "B",
        "entry_price": 100.0, "stop_loss": 95.0,
    }
    first = persist_forecasts_and_plans(
        db, forecasts=[], signals=[base], code="AAPL", market="US",
        mode="intraday", signal_timestamp_ms=int(signal_time.timestamp() * 1000),
    )
    changed = dict(base, entry_price=101.0, stop_loss=96.0)
    second = persist_forecasts_and_plans(
        db, forecasts=[], signals=[changed], code="AAPL", market="US",
        mode="intraday", signal_timestamp_ms=int(signal_time.timestamp() * 1000),
    )
    duplicate = persist_forecasts_and_plans(
        db, forecasts=[], signals=[changed], code="AAPL", market="US",
        mode="intraday", signal_timestamp_ms=int(signal_time.timestamp() * 1000),
    )

    assert first["plan_ids"] != second["plan_ids"]
    assert second["plan_ids"] == duplicate["plan_ids"]
    rows = db.execute(
        "SELECT decision_session_date FROM trade_plan_log ORDER BY id",
    ).fetchall()
    assert len(rows) == 2
    assert {row["decision_session_date"] for row in rows} == {"2026-07-01"}


def test_trade_plan_event_versions_account_snapshot_and_strategy_variant():
    db = _fresh_db()
    signal = [{
        "key": "Q", "variant": "Q_v1", "signal": "sell",
        "signal_intent": "risk_exit", "execution_level": "B",
        "entry_price": 100.0, "stop_loss": 105.0,
    }]
    first = persist_forecasts_and_plans(
        db, forecasts=[], signals=signal, code="AAPL", market="US", mode="eod",
        reference_date="2026-07-01", account_snapshot={"shares": 10, "cost_price": 80},
    )
    second = persist_forecasts_and_plans(
        db, forecasts=[], signals=signal, code="AAPL", market="US", mode="eod",
        reference_date="2026-07-01", account_snapshot={"shares": 5, "cost_price": 80},
    )
    variant_signal = [dict(signal[0], variant="Q_v2")]
    third = persist_forecasts_and_plans(
        db, forecasts=[], signals=variant_signal, code="AAPL", market="US", mode="eod",
        reference_date="2026-07-01", account_snapshot={"shares": 5, "cost_price": 80},
    )

    assert len({first["plan_ids"][0], second["plan_ids"][0], third["plan_ids"][0]}) == 3


def test_us_intraday_plan_is_verified_from_minutes_after_signal_only():
    db = _fresh_db()
    timezone = ZoneInfo("America/New_York")
    signal_time = datetime(2026, 7, 1, 9, 30, tzinfo=timezone)
    signal = [{
        "key": "A", "variant": "A", "signal": "buy",
        "signal_intent": "alpha_entry", "execution_level": "B",
        "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
        "position_pct": 0.1,
    }]
    persist_forecasts_and_plans(
        db, forecasts=[], signals=signal, code="AAPL", market="US",
        mode="intraday", signal_timestamp_ms=int(signal_time.timestamp() * 1000),
    )
    db.insert_intraday_bars([
        IntradayBar(
            "AAPL", "US", int(moment.timestamp() * 1000), "2026-07-01",
            open_price, high, low, close, 1000, "test", "2026-07-02T00:00:00", "provider",
        )
        for moment, open_price, high, low, close in (
            (datetime(2026, 7, 1, 9, 31, tzinfo=timezone), 100, 101, 99, 100),
            (datetime(2026, 7, 1, 10, 0, tzinfo=timezone), 99, 100, 94, 96),
            (datetime(2026, 7, 1, 15, 59, tzinfo=timezone), 96, 97, 95, 96),
        )
    ])

    assert db.verify_due_intraday_trade_plans(code="AAPL") == 1
    row = db.execute("SELECT * FROM trade_plan_log").fetchone()
    assert row["status"] == "evaluated"
    assert row["outcome"] == "stop_loss"
    assert row["entry_price"] == 100.0
    assert row["exit_price"] == 95.0
    assert row["evidence_sources"] == "test"
    assert row["evidence_quality"] == "provider"
    assert row["evidence_bar_count"] == 2
    metrics = db.get_trade_plan_metrics(code="AAPL")
    assert metrics[0]["intraday_count"] == 1
    assert metrics[0]["provider_evidence_count"] == 1
    assert metrics[0]["supplemental_evidence_count"] == 0


def test_provider_minute_bar_is_not_overwritten_by_supplemental_data():
    db = _fresh_db()
    timestamp = int(datetime(
        2026, 7, 1, 10, 0, tzinfo=ZoneInfo("America/New_York")
    ).timestamp() * 1000)
    db.insert_intraday_bars([IntradayBar(
        "AAPL", "US", timestamp, "2026-07-01",
        100, 102, 99, 101, 1000, "tickflow", "2026-07-01T14:01:00", "provider",
    )])
    db.insert_intraday_bars([IntradayBar(
        "AAPL", "US", timestamp, "2026-07-01",
        100, 110, 90, 95, 900, "yfinance", "2026-07-01T14:02:00", "supplemental",
    )])

    row = db.execute("SELECT * FROM intraday_price_history").fetchone()
    assert row["source"] == "tickflow"
    assert row["quality_status"] == "provider"
    assert row["close"] == 101.0


def test_existing_database_migrates_intraday_evidence_columns_on_reopen():
    db = _fresh_db()
    db_path = db._db_path
    for column in (
        "decision_session_date", "evidence_sources", "evidence_quality",
        "evidence_bar_count",
    ):
        db._execute_write(f"ALTER TABLE trade_plan_log DROP COLUMN {column}")
    db.conn.close()
    db._conn = None

    reopened = Database.init(db_path)
    columns = {
        row[1] for row in reopened.execute("PRAGMA table_info(trade_plan_log)").fetchall()
    }
    assert {
        "decision_session_date", "evidence_sources", "evidence_quality",
        "evidence_bar_count",
    } <= columns


def test_a_share_intraday_buy_enters_now_but_waits_until_next_session_to_exit():
    db = _fresh_db()
    timezone = ZoneInfo("Asia/Shanghai")
    signal_time = datetime(2026, 7, 1, 10, 0, tzinfo=timezone)
    persist_forecasts_and_plans(
        db, forecasts=[], signals=[{
            "key": "A", "signal": "buy", "signal_intent": "alpha_entry",
            "execution_level": "B", "entry_price": 100.0,
            "stop_loss": 95.0, "position_pct": 0.1,
        }], code="600000", market="A", mode="intraday",
        signal_timestamp_ms=int(signal_time.timestamp() * 1000),
    )
    same_day = [
        IntradayBar(
            "600000", "A", int(moment.timestamp() * 1000), "2026-07-01",
            100, 101, low, 100, 1000, "test", "2026-07-02T00:00:00", "provider",
        )
        for moment, low in (
            (datetime(2026, 7, 1, 10, 1, tzinfo=timezone), 94),
            (datetime(2026, 7, 1, 15, 0, tzinfo=timezone), 99),
        )
    ]
    db.insert_intraday_bars(same_day)
    assert db.verify_due_intraday_trade_plans(code="600000") == 0

    next_day = [
        IntradayBar(
            "600000", "A", int(moment.timestamp() * 1000), "2026-07-02",
            99, 100, low, close, 1000, "test", "2026-07-03T00:00:00", "provider",
        )
        for moment, low, close in (
            (datetime(2026, 7, 2, 9, 30, tzinfo=timezone), 98, 99),
            (datetime(2026, 7, 2, 15, 0, tzinfo=timezone), 97, 98),
        )
    ]
    db.insert_intraday_bars(next_day)
    assert db.verify_due_intraday_trade_plans(code="600000") == 1
    row = db.execute("SELECT * FROM trade_plan_log").fetchone()
    assert row["status"] == "evaluated"
    assert row["entry_price"] == 100.0
    assert row["exit_price"] == 98.0


def test_live_brier_degradation_rolls_champion_out_of_execution():
    db = _fresh_db()
    version = "forecast_v4_analog_n40@AAPL"
    db.save_forecast_model_version(ForecastModelVersion(
        stock_code="AAPL", market="US", horizon=1, version=version,
        status="challenger", params_json='{"neighbor_count": 40}',
        created_at="2026-01-01T00:00:00",
    ))
    db.promote_forecast_model("US", 1, version, stock_code="AAPL")
    for i in range(20):
        actual = "bearish" if i % 2 == 0 else "neutral"
        db.insert_forecast(ForecastResult(
            code="AAPL", market="US", mode="eod",
            generated_at=f"2026-01-{i+1:02d}T00:00:00",
            data_cutoff=f"2026-01-{i+1:02d}",
            target_session_date=f"2026-02-{i+1:02d}", horizon=1,
            reference_price=100.0, prob_up=0.90, prob_flat=0.05, prob_down=0.05,
            direction="bullish", confidence=0.8, model_version=version,
            feature_snapshot_hash=f"hash{i}", event_key=f"AAPL|eod|{i}|1",
            status="verified", actual_price=95.0, actual_return=-0.05,
            actual_direction=actual, correct=0,
            brier_score=1.715 if actual == "bearish" else 1.715,
            interval_hit=0, verified_at="2026-03-01T00:00:00",
        ))

    assert get_forecast_configs(db, "US", "AAPL") == {}
    assert db.get_forecast_champion("US", 1, "AAPL") is None


if __name__ == "__main__":
    tests = [
        test_forecast_is_independent_probabilistic_and_deterministic,
        test_future_rows_cannot_change_a_past_forecast,
        test_controlled_forecast_challengers_are_oof_and_future_safe,
        test_forecast_promotion_requires_paired_evidence_and_calibration,
        test_forecast_selection_window_embargoes_unmatured_horizon_labels,
        test_probability_and_return_interval_share_one_weighted_distribution,
        test_point_in_time_context_is_frozen_and_deduplicated_by_payload,
        test_oof_snapshot_uses_only_matured_labels,
        test_probability_diagnostics_include_logical_calibration_and_regimes,
        test_weekday_fallback_is_explicitly_unreliable,
        test_stale_daily_history_does_not_generate_retroactive_live_forecasts,
        test_retroactive_forecast_is_quarantined_before_metrics,
        test_forecast_from_stale_cutoff_is_quarantined_even_if_target_is_future,
        test_database_freezes_forecast_and_verifies_exact_target_close,
        test_joint_oof_replay_and_database_audit_are_separate_from_in_sample_backtest,
        test_forecast_and_trade_plan_are_persisted_and_deduplicated_separately,
        test_risk_exit_is_not_saved_as_a_directional_legacy_prediction,
        test_forecast_report_states_target_date_and_separate_metrics,
        test_pipeline_generates_forecast_before_live_plan,
        test_background_optimizer_uses_oof_and_keeps_a_champion,
        test_trade_plan_is_evaluated_separately_from_forecast_accuracy,
        test_forecast_champions_are_isolated_by_stock,
        test_legacy_forecast_champion_is_not_reused_under_v4_semantics,
        test_legacy_logistic_solver_champion_requires_fresh_oof_validation,
        test_adaptive_logistic_solver_is_finite_deterministic_and_normalized,
        test_joint_oof_strategy_selection_enforces_risk_gate_and_stable_ranking,
        test_trade_plan_deduplicates_without_forecast_and_intraday_waits_for_minute_evidence,
        test_trade_plan_versions_material_changes_and_uses_market_session_date,
        test_trade_plan_event_versions_account_snapshot_and_strategy_variant,
        test_us_intraday_plan_is_verified_from_minutes_after_signal_only,
        test_provider_minute_bar_is_not_overwritten_by_supplemental_data,
        test_existing_database_migrates_intraday_evidence_columns_on_reopen,
        test_a_share_intraday_buy_enters_now_but_waits_until_next_session_to_exit,
        test_live_brier_degradation_rolls_champion_out_of_execution,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
