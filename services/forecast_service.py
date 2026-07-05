"""独立预测与交易方案的持久化编排。"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from core.forecast_engine import (
    FEATURE_NAMES,
    FORECAST_MODEL_FAMILY,
    forecast_candidate_passes_baseline,
    paired_block_improvement,
)
from data.models import (
    FeatureContextSnapshot,
    ForecastModelVersion,
    ForecastResult,
    TradePlanLog,
)
from utils.trading_calendar import TradingCalendarUnavailable, forecast_target_dates


NON_DIRECTIONAL_EXIT_INTENTS = {"risk_exit", "profit_lock", "rebalance", "plan"}


def get_forecast_configs(db, market: str, stock_code: str) -> dict[int, dict]:
    """读取正式 Champion；前台只使用已晋升参数。"""
    configs = {}
    for horizon in (1, 3, 5):
        version = db.get_forecast_champion(market, horizon, stock_code)
        if version is None:
            continue
        if not str(version.version or "").startswith(f"{FORECAST_MODEL_FAMILY}_"):
            db.rollback_forecast_model(
                market, horizon, version.version, stock_code,
                reason="模型验证窗口/收益分布口径已升级，旧 Champion 必须重新通过 OOF",
            )
            continue
        metrics = db.get_forecast_metrics(
            market=market, code=stock_code, horizon=horizon,
            model_version=version.version,
        )
        if (
            int(metrics.get("samples", 0) or 0) >= 20
            and float(metrics.get("baseline_brier", 0.0) or 0.0) > 0
            and float(metrics.get("brier_score", 0.0) or 0.0)
                > float(metrics["baseline_brier"]) * 1.05
        ):
            db.rollback_forecast_model(
                market, horizon, version.version, stock_code,
                reason=(
                    f"在线Brier {metrics['brier_score']:.4f} 劣于基线 "
                    f"{metrics['baseline_brier']:.4f} 超过5%"
                ),
            )
            continue
        try:
            params = json.loads(version.params_json or "{}")
        except (TypeError, ValueError):
            params = {}
        params["model_version"] = version.version
        params["validated"] = True
        configs[horizon] = params
    return configs


def persist_forecasts_and_plans(
    db,
    *,
    forecasts: list[ForecastResult],
    signals: list[dict] | None,
    code: str,
    market: str,
    mode: str,
    account_snapshot: dict | None = None,
    reference_date: str = "",
    signal_timestamp_ms: int = 0,
    news_data=None,
    fundamental_data: dict | None = None,
) -> dict:
    """冻结预测并单独记录交易方案；同一事件重复运行不会重复计样本。"""
    ids = db.insert_forecasts(forecasts or [])
    forecast_id = next(
        (item.id for item in forecasts if item.horizon == 1 and item.id),
        ids[0] if ids else None,
    )
    now = datetime.now().isoformat()
    frozen_signal_timestamp_ms = int(
        signal_timestamp_ms or datetime.now().astimezone().timestamp() * 1000
    )
    plan_reference_date = str(
        reference_date
        or next((item.data_cutoff for item in forecasts if item.horizon == 1), "")
        or now
    )[:10]
    decision_session_date = _resolve_decision_session_date(
        forecasts=forecasts,
        market=market,
        mode=mode,
        reference_date=plan_reference_date,
        signal_timestamp_ms=frozen_signal_timestamp_ms,
    )

    plan_ids = []
    for signal in signals or []:
        action = str(signal.get("signal") or "").lower()
        level = str(signal.get("execution_level") or "C").upper()
        if action not in ("buy", "sell") or level not in ("A", "B"):
            continue
        intent = str(signal.get("signal_intent") or "")
        if not intent:
            intent = "alpha_entry" if action == "buy" else "alpha_exit"
        plan = TradePlanLog(
            forecast_id=forecast_id,
            code=code.upper(),
            market=market,
            mode=mode,
            created_at=now,
            reference_date=plan_reference_date,
            decision_session_date=decision_session_date,
            signal_timestamp_ms=(
                frozen_signal_timestamp_ms if mode == "intraday" else 0
            ),
            strategy_key=str(signal.get("key") or signal.get("variant") or ""),
            strategy_version=str(signal.get("variant") or ""),
            signal_intent=intent,
            action=action,
            execution_level=level,
            trigger_price=float(signal.get("trigger_price") or signal.get("entry_price") or 0.0),
            stop_loss=float(signal.get("stop_loss") or 0.0),
            take_profit=float(signal.get("take_profit") or 0.0),
            position_pct=float(signal.get("position_pct") or 0.0),
            max_loss_amount=float(signal.get("max_loss_amount") or 0.0),
            account_snapshot_json=json.dumps(
                account_snapshot or {}, ensure_ascii=False, sort_keys=True
            ),
            status=("pending_intraday" if mode == "intraday" else "pending"),
            outcome=(
                "等待信号时点后的独立分钟K证据；禁止用整日K线伪验证"
                if mode == "intraday" else ""
            ),
        )
        plan_ids.append(db.insert_trade_plan(plan))
    snapshot_id = persist_point_in_time_context(
        db, code=code, market=market, mode=mode,
        effective_date=plan_reference_date,
        news_data=news_data, fundamental_data=fundamental_data,
        captured_at=now,
    )
    return {
        "forecast_ids": ids, "plan_ids": plan_ids,
        "feature_context_snapshot_id": snapshot_id,
    }


def _resolve_decision_session_date(
    *,
    forecasts: list[ForecastResult],
    market: str,
    mode: str,
    reference_date: str,
    signal_timestamp_ms: int,
) -> str:
    """Return the market session in which the plan can first be acted on."""
    if mode == "intraday" and signal_timestamp_ms > 0:
        timezone = ZoneInfo(
            "Asia/Shanghai" if str(market).upper() == "A" else "America/New_York"
        )
        return datetime.fromtimestamp(
            signal_timestamp_ms / 1000, timezone,
        ).date().isoformat()

    target = next(
        (
            str(item.target_session_date or "")[:10]
            for item in forecasts
            if int(item.horizon) == 1 and item.target_session_date
        ),
        "",
    )
    if target:
        return target
    if reference_date:
        try:
            targets = forecast_target_dates(reference_date, market, (1,))
            return str(targets.dates.get(1) or reference_date)[:10]
        except (TradingCalendarUnavailable, ValueError):
            pass
    return str(reference_date or datetime.now().date().isoformat())[:10]


def persist_point_in_time_context(
    db,
    *,
    code: str,
    market: str,
    mode: str,
    effective_date: str,
    news_data=None,
    fundamental_data: dict | None = None,
    captured_at: str = "",
) -> int:
    """Freeze only context that was actually available at analysis time."""
    captured_at = captured_at or datetime.now().isoformat()
    news_score, news_count, latest_news, news_sources = _summarize_news(news_data)
    fundamental = _json_safe(fundamental_data or {})
    fundamental_source = str(fundamental.get("source") or "")
    has_fundamental = bool(
        fundamental_source and fundamental_source.lower() != "default"
        and (fundamental.get("style_factors") or fundamental.get("fundamental_factors"))
    )
    quality = (
        "complete" if news_count > 0 and has_fundamental
        else "partial" if news_count > 0 or has_fundamental
        else "empty"
    )
    payload = {
        "news_score": round(news_score, 8),
        "news_count": news_count,
        "news_latest_published_at": latest_news,
        "news_sources": news_sources,
        "fundamental": fundamental,
        "fundamental_source": fundamental_source,
    }
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    event_key = (
        f"{code.upper()}|{market}|{mode}|{str(effective_date)[:10]}|"
        f"{payload_hash[:20]}"
    )
    snapshot = FeatureContextSnapshot(
        code=code.upper(), market=market, mode=mode,
        captured_at=captured_at, effective_date=str(effective_date or captured_at)[:10],
        news_score=news_score, news_count=news_count,
        news_latest_published_at=latest_news,
        news_sources_json=json.dumps(news_sources, ensure_ascii=False),
        fundamental_json=json.dumps(fundamental, ensure_ascii=False, sort_keys=True),
        fundamental_source=fundamental_source,
        quality_status=quality, payload_hash=payload_hash, event_key=event_key,
    )
    return db.insert_feature_context_snapshot(snapshot)


def _summarize_news(news_data) -> tuple[float, int, str, list[str]]:
    if news_data is None:
        return 0.0, 0, "", []
    rows = []
    if hasattr(news_data, "empty") and hasattr(news_data, "to_dict"):
        if news_data.empty:
            return 0.0, 0, "", []
        rows = news_data.to_dict("records")
    else:
        for item in news_data or []:
            if isinstance(item, dict):
                rows.append(item)
            elif hasattr(item, "to_dict"):
                rows.append(item.to_dict())
    scores, sources, published = [], set(), []
    sentiment_sign = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
    for row in rows:
        raw_score = row.get("finbert_score")
        if raw_score is None:
            raw_score = sentiment_sign.get(str(row.get("sentiment") or "").lower(), 0.0) * float(
                row.get("confidence", 0.0) or 0.0
            )
        try:
            scores.append(float(raw_score or 0.0))
        except (TypeError, ValueError):
            scores.append(0.0)
        source = str(row.get("source") or "").strip()
        if source:
            sources.add(source)
        timestamp = str(row.get("published_at") or row.get("date") or "")
        if timestamp:
            published.append(timestamp)
    return (
        sum(scores) / len(scores) if scores else 0.0,
        len(rows), max(published) if published else "", sorted(sources),
    )


def _json_safe(value) -> dict:
    try:
        return json.loads(json.dumps(value or {}, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {}


def directional_legacy_signal(signal: dict) -> bool:
    """旧追踪兼容层：风险退出、锁利和调仓绝不能变成方向预测。"""
    intent = str(signal.get("signal_intent") or "")
    return intent not in NON_DIRECTIONAL_EXIT_INTENTS


def optimize_forecast_models(db, *, df, market: str, stock_code: str) -> dict:
    """用互不重叠的选择/确认窗口评估单股 Challenger。"""
    from core.forecast_engine import (
        evaluate_forecast_candidates,
        forecast_candidate_configs,
    )

    summary = {"evaluated": 0, "promoted": [], "kept": [], "joint_oof": None}
    for horizon in (1, 3, 5):
        incumbent = db.get_forecast_champion(market, horizon, stock_code)
        try:
            incumbent_params = json.loads(incumbent.params_json or "{}") if incumbent else {}
        except (TypeError, ValueError):
            incumbent_params = {}
        candidates = forecast_candidate_configs()
        normalized_incumbent = _normalized_model_params(incumbent_params)
        if normalized_incumbent not in candidates:
            candidates.append(normalized_incumbent)
        selection = evaluate_forecast_candidates(
            df, horizon=horizon, candidates=candidates,
            # Selection labels must mature no later than the first
            # confirmation origin. Multi-day horizons therefore need an
            # embargo of horizon-1 additional sessions.
            max_evaluations=40,
            evaluation_end_offset=40 + max(horizon - 1, 0),
        )
        confirmation = evaluate_forecast_candidates(
            df, horizon=horizon, candidates=candidates,
            max_evaluations=40, evaluation_end_offset=0,
        )
        summary["evaluated"] += len(selection) + len(confirmation)
        if not selection or not confirmation:
            summary["kept"].append(horizon)
            continue
        best_selection = selection[0]
        best_confirmation = next(
            (item for item in confirmation if item["params"] == best_selection["params"]),
            None,
        )
        current_selection = next(
            (item for item in selection if item["params"] == normalized_incumbent),
            None,
        )
        current_confirmation = next(
            (item for item in confirmation if item["params"] == normalized_incumbent),
            None,
        )
        if not best_confirmation or not current_selection or not current_confirmation:
            summary["kept"].append(horizon)
            continue
        params = best_selection["params"]
        signature = hashlib.sha256(
            json.dumps(params, sort_keys=True).encode("utf-8")
        ).hexdigest()[:10]
        version_name = (
            f"{FORECAST_MODEL_FAMILY}_{params.get('model_type', 'analog')}_"
            f"{signature}@{stock_code.upper()}"
        )
        beats_baseline = bool(
            forecast_candidate_passes_baseline(best_selection)
            and forecast_candidate_passes_baseline(best_confirmation)
        )
        selection_vs_incumbent = paired_block_improvement(
            best_selection.get("_brier_losses") or [],
            current_selection.get("_brier_losses") or [],
        )
        confirmation_vs_incumbent = paired_block_improvement(
            best_confirmation.get("_brier_losses") or [],
            current_confirmation.get("_brier_losses") or [],
        )
        improves_incumbent = bool(
            best_selection["brier_score"] <= current_selection["brier_score"] * 0.97
            and best_confirmation["brier_score"] <= current_confirmation["brier_score"] * 0.97
            and selection_vs_incumbent["lower_90"] > 0
            and confirmation_vs_incumbent["lower_90"] > 0
            and best_selection["ece"] <= current_selection["ece"] + 0.02
            and best_confirmation["ece"] <= current_confirmation["ece"] + 0.02
        )
        should_promote = bool(
            best_confirmation["samples"] >= 30
            and beats_baseline
            and (incumbent is None or (
                best_selection["params"] != normalized_incumbent
                and improves_incumbent
            ))
            and best_confirmation["accuracy"] >= current_confirmation["accuracy"] - 0.02
        )
        is_incumbent_version = bool(incumbent and incumbent.version == version_name)
        model = ForecastModelVersion(
            stock_code=stock_code.upper(),
            market=market,
            horizon=horizon,
            version=version_name,
            status="champion" if is_incumbent_version else "challenger",
            params_json=json.dumps(params, sort_keys=True),
            feature_set_json=json.dumps(list(FEATURE_NAMES)),
            train_start=str(df["date"].iloc[0])[:10] if "date" in df.columns else "",
            train_end=str(df["date"].iloc[-1])[:10] if "date" in df.columns else "",
            sample_count=best_confirmation["samples"],
            accuracy=best_confirmation["accuracy"],
            brier_score=best_confirmation["brier_score"],
            log_loss=best_confirmation["log_loss"],
            calibration_error=best_confirmation["ece"],
            baseline_brier=best_confirmation["baseline_brier"],
            created_at=datetime.now().isoformat(),
            promoted_at=incumbent.promoted_at if is_incumbent_version else "",
            reason=(
                f"双窗口 OOF: selection={best_selection['brier_score']:.4f}, "
                f"confirmation={best_confirmation['brier_score']:.4f}, "
                f"baseline={best_confirmation['baseline_brier']:.4f}, "
                f"LogLoss={best_confirmation['log_loss']:.4f}, "
                f"ECE={best_confirmation['ece']:.4f}, "
                f"80%区间覆盖={best_confirmation['interval_coverage']:.1%}, "
                f"baseline改善90%下界="
                f"{best_confirmation['brier_improvement_lower_90']:+.4f}, "
                f"相对冠军改善90%下界="
                f"{confirmation_vs_incumbent['lower_90']:+.4f}"
            ),
        )
        db.save_forecast_model_version(model)
        if should_promote:
            db.promote_forecast_model(
                market, horizon, version_name, stock_code=stock_code,
            )
            summary["promoted"].append(horizon)
        else:
            summary["kept"].append(horizon)
    try:
        from core.joint_oof import run_joint_oof_replay

        joint = run_joint_oof_replay(
            df, code=stock_code, market=market,
        )
        if joint is not None:
            db.save_joint_oof_run(joint.to_dict())
            summary["joint_oof"] = {
                "samples": joint.samples,
                "trades": joint.total_trades,
                "return": joint.total_return,
                "excess_return": joint.excess_return,
                "sharpe": joint.sharpe_ratio,
            }
    except Exception as exc:
        summary["joint_oof"] = {"error": str(exc)}
    return summary


def _normalized_model_params(params: dict | None) -> dict:
    source = params or {}
    model_type = str(source.get("model_type") or "analog").lower()
    result = {"model_type": model_type, "flat_threshold": 0.01}
    if model_type in ("analog", "ensemble"):
        result["neighbor_count"] = max(20, int(source.get("neighbor_count", 80)))
    if model_type in ("logistic", "ensemble"):
        result["regularization"] = float(source.get("regularization", 0.20))
    if model_type == "tree":
        result["max_depth"] = max(1, min(int(source.get("max_depth", 2)), 3))
        result["min_leaf"] = max(10, int(source.get("min_leaf", 20)))
    if model_type == "ensemble":
        result["blend_weight"] = float(source.get("blend_weight", 0.50))
    return result
