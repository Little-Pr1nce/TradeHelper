"""Frozen registry for the first V2-5 strategy parameter candidates."""

from __future__ import annotations

from functools import lru_cache

from tradehelper_v2.contracts import PlanAction, ScenarioState, StrategyFamily, StrategySpec, stable_hash


def _spec(
    strategy_id: str,
    family: StrategyFamily,
    applicability: str,
    actions: tuple[PlanAction, ...],
    states: tuple[ScenarioState, ...],
    required: tuple[str, ...],
    optional: tuple[str, ...],
    parameters: dict[str, float],
) -> StrategySpec:
    return StrategySpec(
        strategy_id,
        "1",
        family,
        applicability,
        actions,
        states,
        required,
        optional,
        parameters,
        stable_hash(parameters),
    )


@lru_cache(maxsize=1)
def default_specs() -> tuple[StrategySpec, ...]:
    return (
        _spec(
            "trend_continuation_v1", StrategyFamily.TREND_CONTINUATION, "flat", (PlanAction.BUY,),
            (ScenarioState.BULLISH_CONTINUATION,),
            ("closed.ma_20", "closed.ma_60", "closed.atr_pct_14", "closed.macd_hist_pct", "current.price"), (),
            {"trigger_buffer": 0.002, "stop_atr_multiple": 1.5, "take_profit_r_multiple": 2.0},
        ),
        _spec(
            "trend_pullback_v1", StrategyFamily.PULLBACK_ENTRY, "flat", (PlanAction.BUY,),
            (ScenarioState.BULLISH_CONTINUATION, ScenarioState.BULLISH_PULLBACK),
            ("closed.ma_20", "closed.ma_60", "closed.atr_pct_14", "closed.rsi_14", "current.price"),
            ("closed.high_distance_20",),
            {"reclaim_buffer": 0.002, "zone_floor": 0.01, "zone_atr_multiple": 0.5,
             "rsi_min": 40.0, "rsi_max": 65.0, "stop_atr_multiple": 1.25,
             "take_profit_r_multiple": 2.0},
        ),
        _spec(
            "ma120_support_v1", StrategyFamily.SUPPORT_REBOUND, "both", (PlanAction.BUY, PlanAction.ADD),
            (ScenarioState.BULLISH_PULLBACK, ScenarioState.BEARISH_REBOUND, ScenarioState.RANGE_BOUND, ScenarioState.MIXED),
            ("closed.ma_120", "closed.atr_pct_14", "current.price"), (),
            {"reclaim_buffer": 0.005, "zone_floor": 0.015, "zone_atr_multiple": 0.75,
             "stop_floor": 0.02, "stop_atr_multiple": 1.25, "take_profit_r_multiple": 2.0},
        ),
        _spec(
            "range_mean_reversion_v1", StrategyFamily.RANGE_MEAN_REVERSION, "flat", (PlanAction.BUY,),
            (ScenarioState.RANGE_BOUND,),
            ("closed.ma_20", "closed.bb_pct_20", "closed.bb_width_20", "closed.rsi_14", "closed.atr_pct_14", "current.price"), (),
            {"reclaim_buffer": 0.005, "stop_atr_multiple": 1.0, "bb_pct_max": 0.2, "rsi_max": 40.0},
        ),
        _spec(
            "breakout_confirmation_v1", StrategyFamily.BREAKOUT_CONFIRMATION, "flat", (PlanAction.BUY,),
            (ScenarioState.BULLISH_CONTINUATION, ScenarioState.RANGE_BOUND),
            ("closed.high_distance_20", "closed.atr_pct_14", "current.price"),
            ("closed.volume_ratio_20", "current.volume_vs_daily_20"),
            {"breakout_buffer": 0.003, "stop_atr_multiple": 1.5, "volume_ratio_min": 1.2,
             "take_profit_r_multiple": 2.0},
        ),
        _spec(
            "profit_lock_v1", StrategyFamily.PROFIT_LOCK, "held", (PlanAction.REDUCE,), tuple(ScenarioState),
            ("closed.atr_pct_14", "current.price"),
            ("current.retreat_from_session_high", "closed.high_distance_20"),
            {"profit_floor": 0.08, "profit_atr_multiple": 3.0, "retreat_floor": 0.02,
             "retreat_atr_multiple": 1.0},
        ),
        _spec(
            "failed_rebound_exit_v1", StrategyFamily.FAILED_REBOUND_EXIT, "held", (PlanAction.REDUCE, PlanAction.SELL),
            (ScenarioState.BEARISH_CONTINUATION, ScenarioState.BEARISH_REBOUND),
            ("closed.ma_20", "current.price"), ("closed.ma_60",), {},
        ),
        _spec(
            "protective_exit_v1", StrategyFamily.PROTECTIVE_EXIT, "held", (PlanAction.SELL,), tuple(ScenarioState),
            ("current.price",), ("closed.ma_60",), {"cost_stop_pct": 0.08, "ma60_buffer": 0.01},
        ),
        _spec(
            "conditional_observation_v1", StrategyFamily.OBSERVATION, "both", (PlanAction.WATCH,), tuple(ScenarioState),
            (), (), {},
        ),
    )
