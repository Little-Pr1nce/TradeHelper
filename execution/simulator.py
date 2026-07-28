"""纯内存历史成交仿真，不访问网络、数据库或修改账户快照。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from contracts.enums import Market
from contracts.execution import (
    ExecutionEvidenceGrade,
    ExecutionEvent,
    ExecutionMode,
    ExecutionPolicy,
    ExecutionRun,
    ExecutionState,
    ExecutionStateDelta,
    FillEvidence,
    FillOutcome,
    LiquidityEvidence,
    OrderIntent,
    OrderSide,
    PathAssumption,
    TriggerState,
)
from contracts.market_data import ContractViolation, ensure_utc, stable_hash
from contracts.risk import MarketRuleSet
from .costs import CostModel
from .market_rules import ExecutionMarketRules
from .trigger_engine import TriggerEngine


@dataclass(frozen=True, slots=True)
class HistoricalSimulationRequest:
    order_intent: OrderIntent
    execution_state: ExecutionState
    events: tuple[ExecutionEvent, ...]
    market_rules: MarketRuleSet
    execution_policy: ExecutionPolicy
    liquidity_evidence: LiquidityEvidence
    replay_as_of: datetime

    def __post_init__(self):
        intent, state, rules = self.order_intent, self.execution_state, self.market_rules
        replay = ensure_utc(self.replay_as_of, "replay_as_of")
        if (intent.instrument.market is not state.market or rules.market is not state.market or
                intent.instrument.exchange is not rules.exchange or rules.rule_version != intent.market_rule_version or
                self.execution_policy.policy_version != intent.execution_policy_version):
            raise ContractViolation("historical request identities are inconsistent")
        if intent.account_hash is not None and state.account_hash != intent.account_hash:
            raise ContractViolation("historical state does not match the approved account")
        if state.captured_at > replay or self.liquidity_evidence.cutoff_at > replay:
            raise ContractViolation("historical request contains future state or liquidity evidence")
        object.__setattr__(self, "replay_as_of", replay)


@dataclass(frozen=True, slots=True)
class HistoricalSimulationResult:
    trigger_evaluation: object
    fills: tuple[FillEvidence, ...]
    run: ExecutionRun


class HistoricalFillSimulator:
    @staticmethod
    def _empty_delta(state: ExecutionState, reasons: tuple[str, ...]) -> ExecutionStateDelta:
        return ExecutionStateDelta(Decimal("0"), Decimal("0"), None, state.average_cost, state.active_stop, state.active_take_profit, reasons, state.acquired_session_date)

    @staticmethod
    def _raw_price(intent: OrderIntent, state: ExecutionState, event: ExecutionEvent, trigger) -> Decimal:
        if trigger.path_assumption is PathAssumption.GAP_AT_OPEN:
            return event.open
        if trigger.path_assumption is PathAssumption.CONSERVATIVE_STOP_FIRST:
            if state.active_stop is None: raise ContractViolation("stop-first path needs an active stop")
            return state.active_stop
        if "EXEC_STOP_TRIGGERED" in trigger.reason_codes and state.active_stop is not None:
            return state.active_stop
        if "EXEC_TAKE_PROFIT_TRIGGERED" in trigger.reason_codes and state.active_take_profit is not None:
            return state.active_take_profit
        return intent.trigger_level or event.close

    @staticmethod
    def _state_delta(intent: OrderIntent, state: ExecutionState, event: ExecutionEvent, estimate, reasons: tuple[str, ...]) -> ExecutionStateDelta:
        filled = estimate.fillable_shares
        if intent.side is OrderSide.BUY:
            new_shares = state.position_shares + filled
            old_basis = Decimal("0") if state.average_cost is None else state.average_cost * state.position_shares
            average = (old_basis - estimate.cash_delta) / new_shares
            sellable_delta = None if state.sellable_shares is None else (Decimal("0") if state.market is Market.A else filled)
            acquired = event.session_date if state.market is Market.A else state.acquired_session_date
            return ExecutionStateDelta(estimate.cash_delta, filled, sellable_delta, average, intent.stop, intent.take_profit, reasons, acquired)
        remaining = state.position_shares - filled
        full_exit = remaining <= 0
        sellable_delta = None if state.sellable_shares is None else -filled
        return ExecutionStateDelta(
            estimate.cash_delta,
            -filled,
            sellable_delta,
            None if full_exit else state.average_cost,
            None if full_exit else state.active_stop,
            None if full_exit else state.active_take_profit,
            reasons,
            None if full_exit else state.acquired_session_date,
        )

    def simulate(self, request: HistoricalSimulationRequest) -> HistoricalSimulationResult:
        intent, state, rules, policy = request.order_intent, request.execution_state, request.market_rules, request.execution_policy
        events = tuple(sorted(request.events, key=lambda item: (item.interval_start, item.interval_end, item.event_id)))
        if any(event.available_at > request.replay_as_of for event in events): raise ContractViolation("historical replay cannot read future events")
        if any(not (rules.effective_from <= event.interval_start and (rules.effective_to is None or event.interval_start < rules.effective_to)) for event in events):
            raise ContractViolation("market rules are not effective for every execution event")
        trigger = TriggerEngine(policy).evaluate(intent, events, execution_state=state, replay_as_of=request.replay_as_of, generated_at=request.replay_as_of)
        initial_hash, batch = state.source_hash, stable_hash(tuple(item.event_id for item in events))
        run_id = stable_hash({"intent_id": intent.intent_id, "mode": ExecutionMode.HISTORICAL_REPLAY, "initial_state_hash": initial_hash, "event_batch_hash": batch, "replay_as_of": request.replay_as_of, "market_rule_version": rules.rule_version, "execution_policy_version": policy.policy_version})
        if trigger.state is not TriggerState.TRIGGERED:
            mapping = {TriggerState.NOT_TRIGGERED: FillOutcome.NOT_TRIGGERED, TriggerState.INVALIDATED: FillOutcome.INVALIDATED, TriggerState.EXPIRED: FillOutcome.EXPIRED, TriggerState.UNVERIFIABLE: FillOutcome.UNVERIFIABLE, TriggerState.READY: FillOutcome.NOT_TRIGGERED}
            outcome = mapping[trigger.state]
            fill = self._empty_fill(run_id, intent, outcome, trigger, request.replay_as_of, "execution_event_sequence")
            delta = self._empty_delta(state, fill.reason_codes)
            run = ExecutionRun(run_id, intent.intent_id, ExecutionMode.HISTORICAL_REPLAY, initial_hash, batch, request.replay_as_of, rules.rule_version, policy.policy_version, trigger.trigger_evaluation_id, (fill.fill_id,), delta, outcome, trigger.evidence_grade, fill.reason_codes, request.replay_as_of)
            return HistoricalSimulationResult(trigger, (fill,), run)
        event = next(item for item in events if item.event_id == trigger.trigger_event_id)
        check = ExecutionMarketRules.check(intent, state, event, rules)
        if check.outcome:
            fill = self._empty_fill(run_id, intent, check.outcome, trigger, request.replay_as_of, event.source, check.reason_codes, event)
            delta = self._empty_delta(state, fill.reason_codes)
            run = ExecutionRun(run_id, intent.intent_id, ExecutionMode.HISTORICAL_REPLAY, initial_hash, batch, request.replay_as_of, rules.rule_version, policy.policy_version, trigger.trigger_evaluation_id, (fill.fill_id,), delta, check.outcome, trigger.evidence_grade, fill.reason_codes, request.replay_as_of)
            return HistoricalSimulationResult(trigger, (fill,), run)
        raw = self._raw_price(intent, state, event, trigger)
        shares = check.permitted_shares
        if intent.side is OrderSide.BUY:
            lot = rules.lot_size
            while shares > 0:
                estimate = CostModel.estimate(side=intent.side, raw_price=raw, requested_shares=shares, market_rules=rules, policy=policy, liquidity=request.liquidity_evidence, event_at=event.interval_start, evidence_grade=trigger.evidence_grade)
                if estimate.fillable_shares > 0 and -estimate.cash_delta <= state.cash: break
                shares -= lot
            if shares <= 0:
                fill = self._empty_fill(run_id, intent, FillOutcome.REJECTED, trigger, request.replay_as_of, event.source, ("EXEC_CASH_INSUFFICIENT",), event)
                delta = self._empty_delta(state, fill.reason_codes)
                run = ExecutionRun(run_id, intent.intent_id, ExecutionMode.HISTORICAL_REPLAY, initial_hash, batch, request.replay_as_of, rules.rule_version, policy.policy_version, trigger.trigger_evaluation_id, (fill.fill_id,), delta, fill.outcome, trigger.evidence_grade, fill.reason_codes, request.replay_as_of)
                return HistoricalSimulationResult(trigger, (fill,), run)
        estimate = CostModel.estimate(side=intent.side, raw_price=raw, requested_shares=shares, market_rules=rules, policy=policy, liquidity=request.liquidity_evidence, event_at=event.interval_start, evidence_grade=trigger.evidence_grade)
        if estimate.fillable_shares <= 0:
            fill = self._empty_fill(run_id, intent, FillOutcome.REJECTED, trigger, request.replay_as_of, event.source, ("EXEC_NO_TRADABLE_VOLUME",), event)
            delta = self._empty_delta(state, fill.reason_codes)
            run = ExecutionRun(run_id, intent.intent_id, ExecutionMode.HISTORICAL_REPLAY, initial_hash, batch, request.replay_as_of, rules.rule_version, policy.policy_version, trigger.trigger_evaluation_id, (fill.fill_id,), delta, fill.outcome, trigger.evidence_grade, fill.reason_codes, request.replay_as_of)
            return HistoricalSimulationResult(trigger, (fill,), run)
        unfilled = intent.requested_shares - estimate.fillable_shares
        outcome = FillOutcome.FILLED if unfilled == 0 else FillOutcome.PARTIAL
        reasons = set(check.reason_codes) | set(estimate.reason_codes) | set(trigger.reason_codes)
        reasons.add("EXEC_FULL_FILL" if outcome is FillOutcome.FILLED else "EXEC_PARTIAL_FILL")
        if shares != check.permitted_shares: reasons.add("EXEC_CASH_REDUCED")
        reason_tuple = tuple(sorted(reasons))
        delta = self._state_delta(intent, state, event, estimate, reason_tuple)
        fill = self._fill(run_id, intent, outcome, trigger, event, raw, estimate, unfilled, reason_tuple, request.replay_as_of)
        run = ExecutionRun(run_id, intent.intent_id, ExecutionMode.HISTORICAL_REPLAY, initial_hash, batch, request.replay_as_of, rules.rule_version, policy.policy_version, trigger.trigger_evaluation_id, (fill.fill_id,), delta, outcome, estimate.evidence_grade, reason_tuple, request.replay_as_of)
        return HistoricalSimulationResult(trigger, (fill,), run)

    def _empty_fill(self, run_id, intent, outcome, trigger, generated, source, reasons=(), event=None):
        reasons = tuple(sorted(set(reasons) | set(trigger.reason_codes) | {"EXEC_EVIDENCE_INSUFFICIENT"}))
        triggered_at = trigger.triggered_at if trigger.state is TriggerState.TRIGGERED else None
        payload = {"run_id":run_id,"intent_id":intent.intent_id,"decision_id":intent.decision_id,"plan_id":intent.plan_id,"instrument":intent.instrument,"action":intent.action,"side":intent.side,"outcome":outcome,"requested":intent.requested_shares,"filled":Decimal("0"),"unfilled":intent.requested_shares,"raw":None,"slippage":None,"fill":None,"gross":None,"commission":None,"sell_tax":None,"fee":None,"cash":None,"triggered_at":triggered_at,"filled_at":None,"source":source,"granularity":event.granularity if event else None,"path":trigger.path_assumption,"grade":ExecutionEvidenceGrade.INSUFFICIENT,"rule":intent.market_rule_version,"policy":intent.execution_policy_version,"reasons":reasons}
        identifier=stable_hash(payload)
        return FillEvidence(fill_id=identifier,event_key=f"{intent.instrument.stable_key}|{identifier}",run_id=run_id,intent_id=intent.intent_id,decision_id=intent.decision_id,plan_id=intent.plan_id,instrument=intent.instrument,action=intent.action,side=intent.side,outcome=outcome,requested_shares=intent.requested_shares,filled_shares=Decimal("0"),unfilled_shares=intent.requested_shares,raw_price=None,slippage_rate=None,fill_price=None,gross_value=None,commission=None,sell_tax=None,total_fee=None,cash_delta=None,triggered_at=triggered_at,filled_at=None,source=source,granularity=event.granularity if event else None,path_assumption=trigger.path_assumption,evidence_grade=ExecutionEvidenceGrade.INSUFFICIENT,market_rule_version=intent.market_rule_version,execution_policy_version=intent.execution_policy_version,reason_codes=reasons,generated_at=generated)

    def _fill(self, run_id, intent, outcome, trigger, event, raw, estimate, unfilled, reasons, generated):
        filled_at = trigger.triggered_at or event.interval_end
        payload={"run_id":run_id,"intent_id":intent.intent_id,"decision_id":intent.decision_id,"plan_id":intent.plan_id,"instrument":intent.instrument,"action":intent.action,"side":intent.side,"outcome":outcome,"requested":intent.requested_shares,"filled":estimate.fillable_shares,"unfilled":unfilled,"raw":raw,"slippage":estimate.slippage_rate,"fill":estimate.fill_price,"gross":estimate.gross_value,"commission":estimate.commission,"sell_tax":estimate.sell_tax,"fee":estimate.total_fee,"cash":estimate.cash_delta,"triggered_at":trigger.triggered_at,"filled_at":filled_at,"source":event.source,"granularity":event.granularity,"path":trigger.path_assumption,"grade":estimate.evidence_grade,"rule":intent.market_rule_version,"policy":intent.execution_policy_version,"reasons":reasons}
        identifier=stable_hash(payload)
        return FillEvidence(fill_id=identifier,event_key=f"{intent.instrument.stable_key}|{identifier}",run_id=run_id,intent_id=intent.intent_id,decision_id=intent.decision_id,plan_id=intent.plan_id,instrument=intent.instrument,action=intent.action,side=intent.side,outcome=outcome,requested_shares=intent.requested_shares,filled_shares=estimate.fillable_shares,unfilled_shares=unfilled,raw_price=raw,slippage_rate=estimate.slippage_rate,fill_price=estimate.fill_price,gross_value=estimate.gross_value,commission=estimate.commission,sell_tax=estimate.sell_tax,total_fee=estimate.total_fee,cash_delta=estimate.cash_delta,triggered_at=trigger.triggered_at,filled_at=filled_at,source=event.source,granularity=event.granularity,path_assumption=trigger.path_assumption,evidence_grade=estimate.evidence_grade,market_rule_version=intent.market_rule_version,execution_policy_version=intent.execution_policy_version,reason_codes=reasons,generated_at=generated)
