"""趋势延续、回踩和突破模板的冻结公式。"""
from __future__ import annotations

from contracts import ConditionExpression, ConditionOperator, PlanAction, TakeProfitMode
from .common import Proposal, all_of, compare, constant, crossing, feature, level, parameter


def trend_continuation(ctx, spec):
    ma20, ma60, atr, hist = (ctx.number(name) for name in ("closed.ma_20", "closed.ma_60", "closed.atr_pct_14", "closed.macd_hist_pct"))
    if None in (ctx.p0, ma20, ma60, atr, hist):
        return None
    trigger = max(ctx.p0, ma20 * (1 + parameter(spec, "trigger_buffer")))
    stop = min(ma60, trigger - parameter(spec, "stop_atr_multiple") * ctx.p0 * atr)
    risk_multiple = parameter(spec, "take_profit_r_multiple")
    structure = all_of(compare(feature("closed.ma_20"), ConditionOperator.GT, feature("closed.ma_60"), "TREND_STRUCTURE_CONFIRMED"),
                       compare(feature("current.price"), ConditionOperator.GT, feature("closed.ma_20"), "TREND_STRUCTURE_CONFIRMED"),
                       compare(feature("closed.macd_hist_pct"), ConditionOperator.GTE, constant(0), "TREND_STRUCTURE_CONFIRMED"), reason="TREND_STRUCTURE_CONFIRMED")
    invalidation = crossing(
        feature("current.price"),
        ConditionOperator.CROSSES_BELOW,
        feature("closed.ma_20"),
        "PROTECTIVE_EXIT_PENDING",
    )
    confirmed = ctx.pobs is not None and ma20 > ma60 and ctx.pobs > ma20 and hist >= 0
    reasons = ("TREND_STRUCTURE_CONFIRMED",) if confirmed else ("TREND_REENTRY_PENDING",)
    return Proposal(PlanAction.BUY, compare(feature("current.price"), ConditionOperator.GTE, level("trend_trigger", trigger, "closed.ma_20"), "TREND_REENTRY_PENDING"), structure, trigger, "trend_trigger_v1", stop, "trend_stop_v1", trigger + risk_multiple * (trigger - stop), TakeProfitMode.RISK_MULTIPLE, None, invalidation, None, reasons, ("closed.ma_20", "closed.ma_60", "closed.atr_pct_14", "closed.macd_hist_pct"))


def trend_pullback(ctx, spec):
    ma20, ma60, atr, rsi = (ctx.number(name) for name in ("closed.ma_20", "closed.ma_60", "closed.atr_pct_14", "closed.rsi_14"))
    if None in (ctx.p0, ma20, ma60, atr, rsi):
        return None
    trigger = ma20 * (1 + parameter(spec, "reclaim_buffer"))
    stop = min(ma60, ma20 - parameter(spec, "stop_atr_multiple") * ctx.p0 * atr)
    zone = max(parameter(spec, "zone_floor"), parameter(spec, "zone_atr_multiple") * atr)
    risk_multiple = parameter(spec, "take_profit_r_multiple")
    take = trigger + risk_multiple * (trigger - stop)
    high_distance = ctx.number("closed.high_distance_20")
    if high_distance is not None and 1 + high_distance > 0:
        take = max(take, ctx.p0 / (1 + high_distance))
    lower, upper = ma20 * (1 - zone), ma20 * (1 + zone)
    confirmation = all_of(compare(feature("current.price"), ConditionOperator.GTE, feature("closed.ma_60"), "PULLBACK_ZONE_REACHED"),
                            ConditionExpression("", ConditionOperator.BETWEEN, feature("closed.rsi_14"), lower=constant(parameter(spec, "rsi_min")), upper=constant(parameter(spec, "rsi_max")), reason_code="PULLBACK_ZONE_REACHED"),
                            ConditionExpression("", ConditionOperator.BETWEEN, feature("current.price"), lower=level("pullback_zone_low", lower, "closed.ma_20", "closed.atr_pct_14"), upper=level("pullback_zone_high", upper, "closed.ma_20", "closed.atr_pct_14"), reason_code="PULLBACK_ZONE_REACHED"), reason="PULLBACK_ZONE_REACHED")
    return Proposal(PlanAction.BUY, compare(feature("current.price"), ConditionOperator.GTE, level("pullback_trigger", trigger, "closed.ma_20"), "PULLBACK_RECLAIM_PENDING"), confirmation, trigger, "pullback_trigger_v1", stop, "pullback_stop_v1", take, TakeProfitMode.RISK_MULTIPLE, None, compare(feature("current.price"), ConditionOperator.LTE, level("pullback_stop", stop, "closed.ma_20", "closed.ma_60"), "PROTECTIVE_EXIT_PENDING"), None, ("PULLBACK_RECLAIM_PENDING",), ("closed.ma_20", "closed.ma_60", "closed.atr_pct_14", "closed.rsi_14", "closed.high_distance_20", "current.price"))


def breakout_confirmation(ctx, spec):
    distance, atr = (ctx.number(name) for name in ("closed.high_distance_20", "closed.atr_pct_14"))
    if None in (ctx.p0, distance, atr) or 1 + distance <= 0:
        return None
    high20 = ctx.p0 / (1 + distance)
    trigger = high20 * (1 + parameter(spec, "breakout_buffer"))
    stop = trigger - parameter(spec, "stop_atr_multiple") * ctx.p0 * atr
    risk_multiple = parameter(spec, "take_profit_r_multiple")
    volume_name = "closed.volume_ratio_20" if ctx.is_eod else "current.volume_vs_daily_20"
    confirmation = compare(feature(volume_name), ConditionOperator.GTE, constant(parameter(spec, "volume_ratio_min")), "BREAKOUT_VOLUME_CONFIRMED")
    invalidation = crossing(
        feature("current.price"),
        ConditionOperator.CROSSES_BELOW,
        level("breakout_level", trigger, "closed.high_distance_20"),
        "PROTECTIVE_EXIT_PENDING",
    )
    return Proposal(PlanAction.BUY, compare(feature("current.price"), ConditionOperator.GTE, level("breakout_trigger", trigger, "closed.high_distance_20"), "BREAKOUT_LEVEL_PENDING"), confirmation, trigger, "breakout_trigger_v1", stop, "breakout_stop_v1", trigger + risk_multiple * (trigger - stop), TakeProfitMode.RISK_MULTIPLE, None, invalidation, None, ("BREAKOUT_LEVEL_PENDING",), ("closed.high_distance_20", "closed.atr_pct_14", volume_name))
