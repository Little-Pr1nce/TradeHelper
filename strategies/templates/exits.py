"""不依赖预测证据的持仓保护、锁利和反抽失败退出。"""
from __future__ import annotations

from contracts import ConditionOperator, ExitPosture, PlanAction, ScenarioState, TakeProfitMode
from .common import Proposal, always_false, compare, constant, crossing, feature, level, parameter


def protective_exit(ctx, spec):
    evidence = []
    if ctx.cost and ctx.cost > 0:
        stop = ctx.cost * (1 - parameter(spec, "cost_stop_pct"))
    else:
        ma60 = ctx.number("closed.ma_60")
        if not ma60:
            return None
        stop = ma60 * (1 - parameter(spec, "ma60_buffer"))
        evidence.append("closed.ma_60")
    triggered = ctx.pobs is not None and ctx.pobs <= stop
    code = "PROTECTIVE_EXIT_TRIGGERED" if triggered else "PROTECTIVE_EXIT_PENDING"
    return Proposal(PlanAction.SELL, compare(feature("current.price"), ConditionOperator.LTE, level("protective_stop", stop, *evidence), code), None, stop, "protective_stop_v1", None, None, None, TakeProfitMode.NONE, None, always_false(), None, (code,), tuple(evidence))


def profit_lock(ctx, spec):
    atr = ctx.number("closed.atr_pct_14")
    if None in (ctx.cost, ctx.pobs, atr) or ctx.cost <= 0:
        return None
    retreat = ctx.number("current.retreat_from_session_high")
    retreat_feature = "current.retreat_from_session_high"
    if retreat is not None and 1 + retreat > 0:
        peak = ctx.pobs / (1 + retreat)
    else:
        high_distance = ctx.number("closed.high_distance_20")
        if high_distance is None or ctx.p0 is None or 1 + high_distance <= 0:
            return None
        peak = ctx.p0 / (1 + high_distance)
        retreat = ctx.pobs / peak - 1
        retreat_feature = "closed.high_distance_20"
    minimum_profit = max(parameter(spec, "profit_floor"), parameter(spec, "profit_atr_multiple") * atr)
    threshold = max(parameter(spec, "retreat_floor"), parameter(spec, "retreat_atr_multiple") * atr)
    # A trailing profit lock only exists after the observed high-water mark has
    # earned enough profit. Requiring the *retreated current price* to remain
    # above the activation level can create an impossible frozen interval
    # (price >= activation AND price <= lock level).
    enabled = peak / ctx.cost - 1 >= minimum_profit
    if not enabled:
        return None
    retreat_condition = compare(feature("current.retreat_from_session_high"), ConditionOperator.LTE, constant(-threshold), "PROFIT_LOCK_PENDING") if retreat_feature == "current.retreat_from_session_high" else compare(feature("current.price"), ConditionOperator.LTE, level("profit_lock_level", peak * (1 - threshold), "closed.high_distance_20"), "PROFIT_LOCK_PENDING")
    evidence = ("closed.atr_pct_14", retreat_feature)
    return Proposal(PlanAction.REDUCE, retreat_condition, None, peak * (1 - threshold), "profit_lock_level_v1", None, None, None, TakeProfitMode.NONE, None, always_false(), None, (("PROFIT_LOCK_TRIGGERED" if retreat <= -threshold else "PROFIT_LOCK_PENDING"),), evidence)


def failed_rebound_exit(ctx, spec):
    scenario = ctx.input.trading_scenario
    if scenario.state not in {ScenarioState.BEARISH_CONTINUATION, ScenarioState.BEARISH_REBOUND} and scenario.exit_posture is not ExitPosture.PRIORITIZE_PROTECTION:
        return None
    ma20, ma60 = (ctx.number(name) for name in ("closed.ma_20", "closed.ma_60"))
    if ma20 is None:
        return None
    reduce_condition = crossing(feature("current.price"), ConditionOperator.CROSSES_BELOW, level("ma20", ma20, "closed.ma_20"), "FAILED_REBOUND_PENDING")
    stop_condition = compare(feature("current.price"), ConditionOperator.LTE, level("ma60", ma60, "closed.ma_60"), "FAILED_REBOUND_TRIGGERED") if ma60 else reduce_condition
    action = PlanAction.SELL if ctx.pobs is not None and ma60 is not None and ctx.pobs <= ma60 else PlanAction.REDUCE
    return Proposal(action, stop_condition if action is PlanAction.SELL else reduce_condition, None, ma60 if action is PlanAction.SELL else ma20, "failed_rebound_v1", None, None, None, TakeProfitMode.NONE, None, always_false(), None, (("FAILED_REBOUND_TRIGGERED" if action is PlanAction.SELL else "FAILED_REBOUND_PENDING"),), ("closed.ma_20", "closed.ma_60"))
