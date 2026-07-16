"""联合账的顺序组合回放和可复算路径指标。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt
from statistics import mean

from tradehelper_v2.contracts import (
    ContractViolation,
    EvidenceOrigin,
    JointOutcome,
    JointOutcomeKind,
    LearningEvidenceGrade,
    OutcomeStatus,
    stable_hash,
)


@dataclass(frozen=True, slots=True)
class EquityPoint:
    observed_at: datetime
    equity: Decimal
    external_cash_flow: Decimal = Decimal("0")

    def __post_init__(self):
        equity = Decimal(self.equity)
        flow = Decimal(self.external_cash_flow)
        if equity < 0:
            raise ContractViolation("equity path cannot be negative")
        object.__setattr__(self, "equity", equity)
        object.__setattr__(self, "external_cash_flow", flow)


def time_weighted_return(starting_equity, ending_equity, net_cash_flow=Decimal("0")):
    starting = Decimal(starting_equity)
    if starting <= 0:
        raise ValueError("starting equity must be positive")
    return (Decimal(ending_equity) - Decimal(net_cash_flow)) / starting - Decimal("1")


def time_weighted_return_path(points):
    ordered = tuple(sorted(points, key=lambda item: item.observed_at))
    if len(ordered) < 2 or ordered[0].equity <= 0:
        raise ContractViolation("TWR path requires at least two positive-equity observations")
    compounded = Decimal("1")
    for previous, current in zip(ordered, ordered[1:]):
        if previous.equity <= 0:
            raise ContractViolation("TWR path cannot cross zero equity")
        compounded *= (current.equity - current.external_cash_flow) / previous.equity
    return compounded - Decimal("1")


def _path_metrics(points):
    wealth = Decimal("1")
    peak = wealth
    max_drawdown = Decimal("0")
    returns = []
    for previous, point in zip(points, points[1:]):
        segment_return = (point.equity - point.external_cash_flow) / previous.equity - Decimal("1")
        returns.append(float(segment_return))
        wealth *= Decimal("1") + segment_return
        peak = max(peak, wealth)
        max_drawdown = min(max_drawdown, wealth / peak - Decimal("1"))
    if not returns:
        return max_drawdown, None, None, None
    average = mean(returns)
    variance = mean((value - average) ** 2 for value in returns)
    volatility = sqrt(variance) * sqrt(252.0)
    sharpe = None if volatility == 0 else average * 252.0 / volatility
    annualized = (1.0 + average) ** 252.0 - 1.0 if average > -1 else -1.0
    calmar = None if max_drawdown == 0 else annualized / abs(float(max_drawdown))
    return max_drawdown, Decimal(str(volatility)), None if sharpe is None else Decimal(str(sharpe)), None if calmar is None else Decimal(str(calmar))


def replay_joint(
    *,
    outcome_kind,
    portfolio_bundle_id,
    profile,
    batch_id,
    account_hash,
    valuation_id,
    market,
    currency,
    starting_cash,
    starting_positions,
    starting_prices,
    fills,
    ending_prices,
    evidence_origin,
    benchmark_return=None,
    planned_loss=None,
    generated_at,
    ordered_allocations=(),
    equity_points=(),
    external_cash_flows=(),
):
    """按 V2-8 allocation 顺序重放 V2-7 fill，不重新解释成交事实。"""
    kind = outcome_kind if isinstance(outcome_kind, JointOutcomeKind) else JointOutcomeKind(str(outcome_kind))
    origin = evidence_origin if isinstance(evidence_origin, EvidenceOrigin) else EvidenceOrigin(str(evidence_origin))
    if kind is JointOutcomeKind.BROKER_OBSERVED:
        raise ContractViolation("broker-observed outcomes require a future trusted broker connector")
    if kind is JointOutcomeKind.POLICY_OOF and origin is not EvidenceOrigin.RECONSTRUCTED_OOF:
        raise ContractViolation("policy OOF must be marked reconstructed")
    if kind is JointOutcomeKind.RECOMMENDATION_REPLAY and origin is EvidenceOrigin.RECONSTRUCTED_OOF:
        raise ContractViolation("reconstructed outcomes must use policy_oof")

    cash = Decimal(starting_cash)
    positions = {key: Decimal(value) for key, value in starting_positions.items()}
    profile_value = profile.value if hasattr(profile, "value") else profile
    planned = None if planned_loss is None else Decimal(str(planned_loss))
    missing = set(positions) - set(starting_prices)
    if missing:
        raise ContractViolation("starting prices are required for every starting position")
    allocations = tuple(ordered_allocations)
    if allocations and any(
        item.batch_id != batch_id
        or item.profile.value != profile_value
        or item.instrument.market is not market
        for item in allocations
    ):
        raise ContractViolation("joint replay allocations do not match batch, profile, or market")
    allocation_by_decision = {item.decision_id: item for item in allocations}
    if len(allocation_by_decision) != len(allocations):
        raise ContractViolation("joint replay allocations contain duplicate decisions")
    if fills and not allocations:
        raise ContractViolation("joint replay fills require frozen V2-8 allocations")
    allocation_order = {item.decision_id: index for index, item in enumerate(allocations)}

    friction = Decimal("0")
    intents = []
    runs = []
    entries = exits = rejected = 0
    ordered = tuple(
        sorted(
            fills,
            key=lambda item: (
                allocation_order.get(item.decision_id, len(allocation_order)),
                item.filled_at or item.generated_at,
                item.fill_id,
            ),
        )
    )
    for fill in ordered:
        allocation = allocation_by_decision.get(fill.decision_id)
        if (
            allocation is None
            or fill.instrument != allocation.instrument
            or fill.plan_id != allocation.plan_id
            or fill.action is not allocation.action
        ):
            raise ContractViolation("joint replay fill does not match portfolio allocation")
        if fill.requested_shares > allocation.final_requested_shares:
            raise ContractViolation("joint replay fill exceeds allocated shares")
        intents.append(fill.intent_id)
        runs.append(fill.run_id)
        if fill.outcome.value not in {"filled", "partial"}:
            rejected += 1
            continue
        delta = Decimal(fill.cash_delta)
        position_delta = fill.filled_shares if fill.side.value == "buy" else -fill.filled_shares
        if fill.side.value == "buy" and cash + delta < 0:
            raise ContractViolation("frozen buy fill is incompatible with replay cash")
        if fill.side.value == "sell" and positions.get(fill.instrument, Decimal("0")) + position_delta < 0:
            raise ContractViolation("frozen sell fill exceeds replay position")
        cash += delta
        positions[fill.instrument] = positions.get(fill.instrument, Decimal("0")) + position_delta
        friction += Decimal(fill.total_fee)
        if fill.side.value == "buy":
            entries += 1
        else:
            exits += 1

    flows = tuple((at, Decimal(amount)) for at, amount in external_cash_flows)
    net_flow = sum((amount for _, amount in flows), Decimal("0"))
    cash += net_flow
    if any(instrument not in ending_prices for instrument, shares in positions.items() if shares > 0):
        raise ContractViolation("ending prices are required for every open replay position")
    ending = cash + sum(
        (shares * Decimal(ending_prices[instrument]) for instrument, shares in positions.items() if shares > 0),
        Decimal("0"),
    )
    starting = Decimal(starting_cash) + sum(
        (Decimal(shares) * Decimal(starting_prices[instrument]) for instrument, shares in starting_positions.items() if Decimal(shares) > 0),
        Decimal("0"),
    )

    points = tuple(sorted(equity_points, key=lambda item: item.observed_at))
    if len({item.observed_at for item in points}) != len(points):
        raise ContractViolation("joint equity path contains duplicate timestamps")
    if points and points[0].external_cash_flow != 0:
        raise ContractViolation("joint equity path cannot apply cash flow to its opening point")
    if flows and not points:
        raise ContractViolation("external cash flows require an equity path for true TWR")
    reasons = ["LEARNING_PORTFOLIO_SEQUENTIAL_REPLAY"]
    if points:
        if points[0].equity != starting or points[-1].equity != ending:
            raise ContractViolation("joint equity path endpoints do not match replay")
        expected_flows = {}
        for at, amount in flows:
            key = at.date() if isinstance(at, datetime) else at
            expected_flows[key] = expected_flows.get(key, Decimal("0")) + amount
        observed_flows = {}
        for item in points:
            if item.external_cash_flow:
                key = item.observed_at.date()
                observed_flows[key] = observed_flows.get(key, Decimal("0")) + item.external_cash_flow
        if observed_flows != expected_flows:
            raise ContractViolation("joint equity path cash flows do not match replay policy")
        twr = time_weighted_return_path(points)
        max_drawdown, volatility, sharpe, calmar = _path_metrics(points)
        grade = LearningEvidenceGrade.HIGH
    else:
        twr = time_weighted_return(starting, ending, net_flow)
        max_drawdown = min(Decimal("0"), twr)
        volatility = sharpe = calmar = None
        grade = LearningEvidenceGrade.LOW
        reasons.append("LEARNING_PATH_METRICS_UNAVAILABLE")

    benchmark = None if benchmark_return is None else Decimal(str(benchmark_return))
    alpha = None if benchmark is None else twr - benchmark
    realized_loss = max(Decimal("0"), starting + net_flow - ending)
    replay_dates = [item.filled_at.date() for item in ordered if item.filled_at is not None]
    if points:
        replay_dates.extend(item.observed_at.date() for item in points)
    window = None if not replay_dates else (min(replay_dates), max(replay_dates))
    allocation_ids = tuple(item.allocation_id for item in allocations)
    identity = {
        "kind": kind,
        "bundle": portfolio_bundle_id,
        "profile": profile_value,
        "batch": batch_id,
        "account": account_hash,
        "valuation": valuation_id,
        "market": market,
        "currency": currency,
        "allocations": allocation_ids,
        "intents": tuple(intents),
        "runs": tuple(runs),
        "origin": origin,
        "start": starting,
        "end": ending,
        "cash_flow": net_flow,
        "twr": twr,
        "benchmark": benchmark,
        "alpha": alpha,
        "drawdown": max_drawdown,
        "volatility": volatility,
        "sharpe": sharpe,
        "calmar": calmar,
        "friction": friction,
        "planned_loss": planned,
        "realized_loss": realized_loss,
        "counts": (entries, exits, rejected),
        "window": window,
        "status": OutcomeStatus.MATURED,
        "grade": grade,
        "reasons": tuple(sorted(reasons)),
    }
    return JointOutcome(
        stable_hash(identity),
        kind,
        portfolio_bundle_id,
        profile_value,
        batch_id,
        account_hash,
        valuation_id,
        market,
        currency,
        allocation_ids,
        tuple(intents),
        tuple(runs),
        origin,
        starting,
        ending,
        net_flow,
        twr,
        benchmark,
        alpha,
        max_drawdown,
        volatility,
        sharpe,
        calmar,
        friction,
        planned,
        realized_loss,
        entries,
        exits,
        rejected,
        OutcomeStatus.MATURED,
        grade,
        tuple(sorted(reasons)),
        generated_at,
        window,
    )
