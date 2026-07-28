"""MA120 支撑反弹与区间均值回归模板。"""
from __future__ import annotations

from contracts import ConditionExpression, ConditionOperator, PlanAction, TakeProfitMode
from .common import Proposal, all_of, compare, constant, crossing, feature, level, parameter


def ma120_support_rebound(ctx, spec):
    ma120, atr = (ctx.number(name) for name in ("closed.ma_120", "closed.atr_pct_14"))
    if None in (ctx.p0, ma120, atr):
        return None
    trigger = ma120 * (1 + parameter(spec, "reclaim_buffer"))
    stop = ma120 * (1 - max(parameter(spec, "stop_floor"), parameter(spec, "stop_atr_multiple") * atr))
    zone = max(parameter(spec, "zone_floor"), parameter(spec, "zone_atr_multiple") * atr)
    risk_multiple = parameter(spec, "take_profit_r_multiple")
    zone_condition = ConditionExpression(
        "",
        ConditionOperator.BETWEEN,
        feature("current.price"),
        lower=level("ma120_zone_low", ma120 * (1 - zone), "closed.ma_120", "closed.atr_pct_14"),
        upper=level("ma120_zone_high", ma120 * (1 + zone), "closed.ma_120", "closed.atr_pct_14"),
        reason_code="MA120_SUPPORT_ZONE_REACHED",
    )
    reclaim = crossing(feature("current.price"), ConditionOperator.CROSSES_ABOVE, level("ma120_trigger", trigger, "closed.ma_120"), "MA120_RECLAIM_PENDING")
    distance = ctx.pobs / ma120 - 1 if ctx.pobs is not None else None
    reasons = (("MA120_SUPPORT_ZONE_REACHED",) if distance is not None and abs(distance) <= zone else ()) + ("MA120_RECLAIM_PENDING",)
    if ctx.bearish:
        reasons += ("COUNTERTREND_ONLY",)
    return Proposal(PlanAction.ADD if ctx.held else PlanAction.BUY, compare(feature("current.price"), ConditionOperator.GTE, level("ma120_trigger", trigger, "closed.ma_120"), "MA120_RECLAIM_PENDING"), all_of(zone_condition, reclaim, reason="MA120_RECLAIM_PENDING"), trigger, "ma120_trigger_v1", stop, "ma120_stop_v1", trigger + risk_multiple * (trigger - stop), TakeProfitMode.RISK_MULTIPLE, None, compare(feature("current.price"), ConditionOperator.LTE, level("ma120_stop", stop, "closed.ma_120", "closed.atr_pct_14"), "PROTECTIVE_EXIT_PENDING"), None, reasons, ("closed.ma_120", "closed.atr_pct_14", "current.price"))


def range_mean_reversion(ctx, spec):
    ma20, bb, width, rsi, atr = (ctx.number(name) for name in ("closed.ma_20", "closed.bb_pct_20", "closed.bb_width_20", "closed.rsi_14", "closed.atr_pct_14"))
    if None in (ctx.p0, ma20, bb, width, rsi, atr):
        return None
    lower, upper = ma20 * (1 - width / 2), ma20 * (1 + width / 2)
    trigger = lower * (1 + parameter(spec, "reclaim_buffer"))
    stop = lower - parameter(spec, "stop_atr_multiple") * ctx.p0 * atr
    confirmation = all_of(compare(feature("closed.bb_pct_20"), ConditionOperator.LTE, constant(parameter(spec, "bb_pct_max")), "RANGE_LOWER_ZONE_REACHED"), compare(feature("closed.rsi_14"), ConditionOperator.LTE, constant(parameter(spec, "rsi_max")), "RANGE_LOWER_ZONE_REACHED"), reason="RANGE_LOWER_ZONE_REACHED")
    conditional = compare(feature("current.price"), ConditionOperator.GTE, level("range_upper", upper, "closed.ma_20", "closed.bb_width_20"), "RANGE_RECLAIM_PENDING")
    reasons = (("RANGE_LOWER_ZONE_REACHED",) if bb <= parameter(spec, "bb_pct_max") and rsi <= parameter(spec, "rsi_max") else ()) + ("RANGE_RECLAIM_PENDING",)
    return Proposal(PlanAction.BUY, compare(feature("current.price"), ConditionOperator.GTE, level("range_trigger", trigger, "closed.ma_20", "closed.bb_width_20"), "RANGE_RECLAIM_PENDING"), confirmation, trigger, "range_trigger_v1", stop, "range_stop_v1", ma20, TakeProfitMode.FIXED, conditional, compare(feature("current.price"), ConditionOperator.LTE, level("range_stop", stop, "closed.ma_20", "closed.atr_pct_14"), "PROTECTIVE_EXIT_PENDING"), None, reasons, ("closed.ma_20", "closed.bb_pct_20", "closed.bb_width_20", "closed.rsi_14", "closed.atr_pct_14"))
