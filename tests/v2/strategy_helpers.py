"""V2-5 的纯内存固定输入，避免策略测试依赖外部行情或 V1。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from tradehelper_v2.contracts import (
    DecisionMode, FeatureEvidenceMode, FeatureSnapshot, FeatureStatus, FeatureValue,
    PositionSnapshot, StrategyInput, TradingSession, stable_hash,
)
from tradehelper_v2.scenario import ScenarioPlanner
from tradehelper_v2.strategies.registry import default_specs
from tradehelper_v2.contracts.scenario import TradingScenario

from test_scenario_planner import NOW, _forecast, _mode_request, _quote, _request


def _scenario_with_hash(scenario: TradingScenario, feature_hash: str) -> TradingScenario:
    identity = {"instrument": scenario.instrument, "mode": scenario.mode.value, "as_of": scenario.as_of,
                "origin_session_date": scenario.origin_session_date, "decision_session": scenario.decision_session,
                "valid_from": scenario.valid_from, "expires_at": scenario.expires_at,
                "forecast_bundle_hash": scenario.forecast_bundle_hash, "current_feature_hash": feature_hash,
                "fact_update_hash": scenario.fact_update_hash, "quality_hash": scenario.quality_hash,
                "policy_version": scenario.policy_version}
    scenario_id = stable_hash(identity)
    session = scenario.decision_session.session_date.isoformat() if scenario.decision_session else "calendar-unavailable"
    return replace(scenario, scenario_id=scenario_id, event_key=f"{scenario.instrument.stable_key}|{scenario.mode.value}|{session}|{scenario_id}", current_feature_hash=feature_hash)


def position(instrument, *, shares: str = "10", cost: str = "100") -> PositionSnapshot:
    return PositionSnapshot(instrument, Decimal(shares), Decimal(cost), NOW)


def strategy_input(
    instrument,
    *,
    position=None,
    directions=None,
    reference_price: float = 100.0,
    feature_overrides: dict[str, float] | None = None,
    feature_statuses: dict[str, FeatureStatus] | None = None,
    mode: DecisionMode = DecisionMode.EOD,
    quote_price: float | None = None,
    confirmed: bool = True,
    specs=None,
) -> StrategyInput:
    directions = directions or {1: "bullish", 3: "bullish", 5: "bullish", 10: "bullish"}
    forecasts = tuple(replace(_forecast(instrument, horizon, directions[horizon], confirmed=confirmed), reference_price=reference_price) for horizon in (1, 3, 5, 10))
    request = _request(instrument, forecasts)
    request = replace(request, forecasts=tuple(replace(item, reference_price=reference_price) for item in request.forecasts))
    if mode is not DecisionMode.EOD:
        quote = _quote(instrument, price=quote_price) if quote_price is not None else None
        request = _mode_request(request, mode, quote=quote)
    scenario = ScenarioPlanner().build(request)
    numbers = {
        "closed.ma_20": 101.0, "closed.ma_60": 98.0, "closed.ma_120": 95.0,
        "closed.ma_distance_5": 0.0, "closed.ma_distance_10": 0.0,
        "closed.ma_distance_20": reference_price / 101 - 1, "closed.ma_distance_60": reference_price / 98 - 1,
        "closed.ma_distance_120": reference_price / 95 - 1, "closed.atr_pct_14": .02,
        "closed.macd_hist_pct": .01, "closed.rsi_14": 35.0, "closed.bb_pct_20": .15,
        "closed.bb_width_20": .08, "closed.high_distance_20": reference_price / 105 - 1,
        "closed.volume_ratio_20": 1.3, "current.price": scenario.current_overlay.current_price or reference_price,
        "current.retreat_from_session_high": -.03, "current.volume_vs_daily_20": 1.3,
    }
    numbers.update(feature_overrides or {})
    statuses = feature_statuses or {}
    values = tuple(
        FeatureValue(name, value if statuses.get(name, FeatureStatus.AVAILABLE) is FeatureStatus.AVAILABLE else None,
                     statuses.get(name, FeatureStatus.AVAILABLE), None, None, NOW, ("fixture",), False, None)
        for name, value in numbers.items()
    )
    snapshot = FeatureSnapshot(instrument, mode, NOW, request.current_snapshot.latest_bar_date, None,
                               "2.2.0", FeatureEvidenceMode.RECONSTRUCTED_HISTORY, values, "a" * 64, "b" * 64, NOW)
    scenario = _scenario_with_hash(scenario, snapshot.feature_hash)
    return StrategyInput(instrument, snapshot, scenario, position, specs or default_specs(), "strategy_policy_v1", NOW)
