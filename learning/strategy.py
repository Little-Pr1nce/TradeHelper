"""单计划隔离回放的策略账结果构造。"""
from __future__ import annotations

from decimal import Decimal

from contracts import (
    ContractViolation,
    EvidenceOrigin,
    LearningEvidenceGrade,
    OutcomeStatus,
    PlanAction,
    StrategyOutcome,
    stable_hash,
)


_COMPLETED = {"filled", "partial"}
_ENTRY_ACTIONS = {PlanAction.BUY, PlanAction.ADD}
_EXIT_ACTIONS = {PlanAction.SELL, PlanAction.REDUCE}


def _validate_fill(plan, decision, fill):
    if (
        fill.plan_id != plan.plan_id
        or fill.decision_id != decision.decision_id
        or fill.instrument != plan.instrument
        or fill.action is not plan.action
        or fill.filled_shares > decision.approved_shares
    ):
        raise ContractViolation("strategy outcome fill does not match frozen plan and decision")


def _path_returns(prices, reference, *, invert=False):
    values = []
    for price in prices:
        returned = Decimal(str(price)) / reference - Decimal("1")
        values.append(-returned if invert else returned)
    return tuple(values)


def strategy_outcome(
    *,
    plan,
    decision,
    horizon,
    target_session_date,
    evidence_origin,
    trigger_state,
    fill=None,
    target_close=None,
    benchmark_return=None,
    exit_type=None,
    exit_at=None,
    exit_price=None,
    post_exit_underlying_return=None,
    exit_friction=Decimal("0"),
    estimated_exit_cost=None,
    holding_sessions=None,
    generated_at,
    price_path=(),
    exit_fill=None,
    market_regime_key=None,
):
    """从冻结 V2-6 decision 与 V2-7 fill 证据生成策略结果。

    入场计划以成交价为起点，按明确退出成交或评价窗口收盘计算；退出计划
    使用退出后标的表现计算 avoided loss、opportunity cost 和 exit quality。
    """
    if plan.plan_id != decision.plan_id or plan.instrument != decision.instrument or plan.action is not decision.action:
        raise ContractViolation("strategy outcome plan and decision mismatch")
    if horizon not in {1, 3, 5, 10}:
        raise ContractViolation("unsupported strategy evaluation horizon")

    action = plan.action
    reasons = []
    status = OutcomeStatus.MATURED
    grade = LearningEvidenceGrade.INSUFFICIENT
    fill_outcome = "not_applicable"
    shares = Decimal("0")
    fill_price = gross = net = excess = mae = mfe = None
    commission = tax = slippage = None
    entry_fill_id = exit_fill_id = None

    if trigger_state == "not_triggered":
        reasons.append("LEARNING_PLAN_NOT_TRIGGERED")
    elif fill is None or fill.outcome.value not in _COMPLETED:
        fill_outcome = "rejected"
        reasons.append("LEARNING_ORDER_REJECTED")
    else:
        _validate_fill(plan, decision, fill)
        fill_outcome = fill.outcome.value
        shares = fill.filled_shares
        fill_price = fill.fill_price
        commission = fill.commission
        tax = fill.sell_tax
        slippage = fill.gross_value * fill.slippage_rate
        grade = LearningEvidenceGrade.LOW if fill.evidence_grade.value == "low" else LearningEvidenceGrade.HIGH
        reasons.extend(("LEARNING_PLAN_TRIGGERED", "LEARNING_ORDER_FILLED"))

        if action in _ENTRY_ACTIONS:
            if fill.side.value != "buy":
                raise ContractViolation("entry strategy outcome requires a buy fill")
            entry_fill_id = fill.fill_id
            evaluated_exit = None
            exit_fee = None
            if exit_fill is not None:
                if (
                    exit_fill.instrument != plan.instrument
                    or exit_fill.side.value != "sell"
                    or exit_fill.outcome.value not in _COMPLETED
                    or exit_fill.filled_at < fill.filled_at
                    or exit_fill.filled_shares != fill.filled_shares
                ):
                    raise ContractViolation("strategy exit fill is incompatible with entry plan")
                evaluated_exit = exit_fill.fill_price
                exit_fee = exit_fill.total_fee
                exit_fill_id = exit_fill.fill_id
                exit_price = evaluated_exit
                exit_at = exit_fill.filled_at
                exit_type = exit_type or "subsequent_trade_plan"
            elif exit_price is not None:
                evaluated_exit = Decimal(str(exit_price))
                exit_fee = None if estimated_exit_cost is None else Decimal(str(estimated_exit_cost))
            elif target_close is not None:
                evaluated_exit = Decimal(str(target_close))
                exit_fee = None if estimated_exit_cost is None else Decimal(str(estimated_exit_cost))
                exit_price = evaluated_exit
                exit_type = exit_type or "window_close"
                reasons.append("LEARNING_WINDOW_CLOSE")

            if evaluated_exit is None:
                status = OutcomeStatus.PENDING
                grade = LearningEvidenceGrade.INSUFFICIENT
            elif exit_fee is None:
                status = OutcomeStatus.UNVERIFIABLE
                grade = LearningEvidenceGrade.INSUFFICIENT
                reasons.append("LEARNING_EXIT_COST_UNAVAILABLE")
            else:
                gross = evaluated_exit / fill_price - Decimal("1")
                entry_cost = fill.gross_value + fill.total_fee
                exit_value = evaluated_exit * shares - exit_fee
                net = exit_value / entry_cost - Decimal("1")
                excess = None if benchmark_return is None else net - Decimal(str(benchmark_return))
                path = _path_returns((*price_path, evaluated_exit), fill_price)
                mae = min((*path, Decimal("0")))
                mfe = max((*path, Decimal("0")))
        elif action in _EXIT_ACTIONS:
            if fill.side.value != "sell":
                raise ContractViolation("exit strategy outcome requires a sell fill")
            exit_fill_id = fill.fill_id
            if post_exit_underlying_return is None and target_close is not None:
                post_exit_underlying_return = Decimal(str(target_close)) / fill_price - Decimal("1")
            if post_exit_underlying_return is None:
                status = OutcomeStatus.PENDING
                grade = LearningEvidenceGrade.INSUFFICIENT
            else:
                post = Decimal(str(post_exit_underlying_return))
                friction = Decimal(str(exit_friction))
                if fill.gross_value:
                    friction += fill.total_fee / fill.gross_value
                avoided = max(Decimal("0"), -post)
                opportunity = max(Decimal("0"), post)
                quality = avoided - opportunity - friction
                gross = -post
                net = quality
                excess = None if benchmark_return is None else net - Decimal(str(benchmark_return))
                path = _path_returns(price_path, fill_price, invert=True)
                if path:
                    mae = min((*path, Decimal("0")))
                    mfe = max((*path, Decimal("0")))
                exit_type = exit_type or "strategy_exit"
                exit_at = exit_at or fill.filled_at
                exit_price = exit_price or fill.fill_price
                reasons.append("LEARNING_EXIT_EVALUATED")
        else:
            raise ContractViolation("strategy outcome only supports executable entry or exit actions")

    post = None if post_exit_underlying_return is None else Decimal(str(post_exit_underlying_return))
    avoided = None if post is None else max(Decimal("0"), -post)
    opportunity = None if post is None else max(Decimal("0"), post)
    quality = None if post is None else avoided - opportunity - Decimal(str(exit_friction))
    if action in _EXIT_ACTIONS and fill is not None and getattr(fill, "gross_value", None):
        quality -= fill.total_fee / fill.gross_value

    identity = {
        "plan": plan.plan_id,
        "decision": decision.decision_id,
        "origin": evidence_origin,
        "horizon": horizon,
        "target": target_session_date,
        "trigger": trigger_state,
        "fill": fill_outcome,
        "entry_fill_id": entry_fill_id,
        "exit_fill_id": exit_fill_id,
        "exit_type": exit_type,
        "exit_at": exit_at,
        "exit_price": exit_price,
        "gross": gross,
        "net": net,
        "mae": mae,
        "mfe": mfe,
        "reasons": tuple(sorted(set(reasons))),
    }
    return StrategyOutcome(
        stable_hash(identity),
        plan.plan_id,
        plan.scenario_id,
        decision.decision_id,
        plan.instrument,
        plan.action.value,
        plan.family.value,
        plan.strategy_id,
        plan.strategy_version,
        plan.parameter_hash,
        decision.profile.value,
        evidence_origin,
        horizon,
        target_session_date,
        trigger_state,
        fill.triggered_at if fill else None,
        fill_outcome,
        fill_price,
        shares,
        gross,
        net,
        Decimal(str(benchmark_return)) if benchmark_return is not None else None,
        excess,
        mae,
        mfe,
        grade,
        status,
        tuple(sorted(set(reasons))),
        generated_at,
        plan.valid_from,
        plan.expires_at,
        exit_type,
        exit_at,
        exit_price,
        holding_sessions,
        commission,
        tax,
        slippage,
        avoided,
        opportunity,
        quality,
        entry_fill_id,
        exit_fill_id,
        market_regime_key,
        generated_at,
    )
