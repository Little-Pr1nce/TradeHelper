"""以统一事件序列复核冻结条件，不回写未来特征。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from tradehelper_v2.contracts.execution import (
    ExecutionEvidenceGrade,
    ExecutionEvent,
    ExecutionPolicy,
    ExecutionState,
    EventGranularity,
    OrderIntent,
    OrderSide,
    PathAssumption,
    TriggerEvaluation,
    TriggerState,
)
from tradehelper_v2.contracts.market_data import ContractViolation, stable_hash
from tradehelper_v2.contracts.strategy import (
    ConditionExpression,
    ConditionOperator,
    EvidenceRequirement,
    OperandKind,
)


_CONFLICT = object()


class TriggerEngine:
    def __init__(self, policy: ExecutionPolicy) -> None:
        self.policy = policy

    @staticmethod
    def _events(events, replay_as_of: datetime | None):
        result = tuple(sorted(events, key=lambda item: (item.interval_start, item.interval_end, item.event_id)))
        last = None
        for event in result:
            if last is not None and event.interval_start < last:
                raise ContractViolation("execution events overlap")
            if replay_as_of is not None and event.available_at > replay_as_of:
                raise ContractViolation("future execution event is not replayable")
            last = event.interval_end
        return result

    @staticmethod
    def _frozen_values(intent: OrderIntent) -> dict[str, object]:
        values: dict[str, object] = {}
        for evaluation in intent.condition_evaluations:
            for observed in evaluation.observed_values:
                if observed.value is None:
                    continue
                value = Decimal(str(observed.value)) if isinstance(observed.value, (int, float)) and not isinstance(observed.value, bool) else observed.value
                if observed.key in values and values[observed.key] != value:
                    values[observed.key] = _CONFLICT
                else:
                    values[observed.key] = value
        return values

    @staticmethod
    def _value(operand, event: ExecutionEvent, frozen: dict[str, object]):
        if operand.kind in {OperandKind.CONSTANT, OperandKind.DERIVED_LEVEL}:
            return Decimal(str(operand.value)) if isinstance(operand.value, (float, int)) and not isinstance(operand.value, bool) else operand.value
        key = operand.key
        current = {
            "current.price": event.close,
            "current.close": event.close,
            "current.open": event.open,
            "current.high": event.high,
            "current.low": event.low,
            "current.volume": event.volume,
            "current.previous_close": event.previous_close,
        }
        if key == "current.retreat_from_session_high":
            return None if event.granularity is EventGranularity.QUOTE else event.close / event.high - Decimal("1")
        if key in current:
            return current[key]
        value = frozen.get(key)
        return None if value is _CONFLICT else value

    @staticmethod
    def _merge_paths(paths: list[PathAssumption]) -> PathAssumption:
        if PathAssumption.STRICT_UNKNOWN in paths:
            return PathAssumption.STRICT_UNKNOWN
        if PathAssumption.GAP_AT_OPEN in paths:
            return PathAssumption.GAP_AT_OPEN
        if PathAssumption.CONSERVATIVE_STOP_FIRST in paths:
            return PathAssumption.CONSERVATIVE_STOP_FIRST
        if PathAssumption.POINT_SNAPSHOT in paths:
            return PathAssumption.POINT_SNAPSHOT
        return PathAssumption.EXACT_SEQUENCE

    def _bar_price_condition(self, condition: ConditionExpression, event: ExecutionEvent, frozen: dict[str, object]):
        if event.granularity is EventGranularity.QUOTE or condition.left is None or condition.left.kind is not OperandKind.FEATURE or condition.left.key not in {"current.price", "current.close"}:
            return None
        operator = condition.operator
        if operator is ConditionOperator.BETWEEN:
            lower, upper = self._value(condition.lower, event, frozen), self._value(condition.upper, event, frozen)
            if lower is None or upper is None:
                return None, PathAssumption.STRICT_UNKNOWN
            if lower <= event.open <= upper:
                return True, PathAssumption.GAP_AT_OPEN
            return event.low <= upper and event.high >= lower, PathAssumption.POINT_SNAPSHOT
        if operator not in {ConditionOperator.GT, ConditionOperator.GTE, ConditionOperator.LT, ConditionOperator.LTE, ConditionOperator.EQUALS}:
            return None
        right = self._value(condition.right, event, frozen)
        if right is None:
            return None, PathAssumption.STRICT_UNKNOWN
        if operator is ConditionOperator.GT:
            return (True, PathAssumption.GAP_AT_OPEN) if event.open > right else (event.high > right, PathAssumption.POINT_SNAPSHOT)
        if operator is ConditionOperator.GTE:
            return (True, PathAssumption.GAP_AT_OPEN) if event.open >= right else (event.high >= right, PathAssumption.POINT_SNAPSHOT)
        if operator is ConditionOperator.LT:
            return (True, PathAssumption.GAP_AT_OPEN) if event.open < right else (event.low < right, PathAssumption.POINT_SNAPSHOT)
        if operator is ConditionOperator.LTE:
            return (True, PathAssumption.GAP_AT_OPEN) if event.open <= right else (event.low <= right, PathAssumption.POINT_SNAPSHOT)
        if event.low <= right <= event.high:
            return True, PathAssumption.GAP_AT_OPEN if event.open == right else PathAssumption.POINT_SNAPSHOT
        return False, PathAssumption.POINT_SNAPSHOT

    def _condition(self, condition: ConditionExpression, event: ExecutionEvent, previous: ExecutionEvent | None, frozen: dict[str, object]) -> tuple[bool | None, PathAssumption]:
        if condition.evidence_requirement is EvidenceRequirement.SESSION_OHLC and event.granularity is not EventGranularity.DAILY_BAR:
            return None, PathAssumption.STRICT_UNKNOWN
        if condition.evidence_requirement is EvidenceRequirement.SESSION_VOLUME and (event.granularity is not EventGranularity.DAILY_BAR or event.volume is None):
            return None, PathAssumption.STRICT_UNKNOWN
        operator = condition.operator
        if operator in {ConditionOperator.ALL, ConditionOperator.ANY}:
            results = [self._condition(item, event, previous, frozen) for item in condition.children]
            values, paths = [item[0] for item in results], [item[1] for item in results]
            value = (False if False in values else None if None in values else True) if operator is ConditionOperator.ALL else (True if True in values else None if None in values else False)
            return value, self._merge_paths(paths)
        if operator is ConditionOperator.NOT:
            value, path = self._condition(condition.children[0], event, previous, frozen)
            return (None if value is None else not value), path
        bar_result = self._bar_price_condition(condition, event, frozen)
        if bar_result is not None:
            return bar_result
        left = self._value(condition.left, event, frozen)
        right = self._value(condition.right, event, frozen) if condition.right else None
        if left is None or (operator is not ConditionOperator.BETWEEN and right is None):
            return None, PathAssumption.STRICT_UNKNOWN
        if operator is ConditionOperator.GT: return left > right, PathAssumption.POINT_SNAPSHOT
        if operator is ConditionOperator.GTE: return left >= right, PathAssumption.POINT_SNAPSHOT
        if operator is ConditionOperator.LT: return left < right, PathAssumption.POINT_SNAPSHOT
        if operator is ConditionOperator.LTE: return left <= right, PathAssumption.POINT_SNAPSHOT
        if operator is ConditionOperator.EQUALS: return left == right, PathAssumption.POINT_SNAPSHOT
        if operator is ConditionOperator.BETWEEN:
            lower, upper = self._value(condition.lower, event, frozen), self._value(condition.upper, event, frozen)
            return (None if lower is None or upper is None else lower <= left <= upper), PathAssumption.POINT_SNAPSHOT
        if operator in {ConditionOperator.CROSSES_ABOVE, ConditionOperator.CROSSES_BELOW}:
            if previous is None:
                return False, PathAssumption.STRICT_UNKNOWN
            prior = self._value(condition.left, previous, frozen)
            if prior is None:
                return None, PathAssumption.STRICT_UNKNOWN
            if operator is ConditionOperator.CROSSES_ABOVE:
                if event.open > right and prior <= right:
                    return True, PathAssumption.GAP_AT_OPEN
                if event.granularity is EventGranularity.DAILY_BAR and prior <= right < event.high:
                    return None, PathAssumption.STRICT_UNKNOWN
                return prior <= right and left > right, PathAssumption.EXACT_SEQUENCE
            if event.open < right and prior >= right:
                return True, PathAssumption.GAP_AT_OPEN
            if event.granularity is EventGranularity.DAILY_BAR and prior >= right > event.low:
                return None, PathAssumption.STRICT_UNKNOWN
            return prior >= right and left < right, PathAssumption.EXACT_SEQUENCE
        return None, PathAssumption.STRICT_UNKNOWN

    @staticmethod
    def _protective(event: ExecutionEvent, intent: OrderIntent, state: ExecutionState | None):
        if state is None or intent.side is not OrderSide.SELL or state.position_shares <= 0:
            return None
        stop, take = state.active_stop, state.active_take_profit
        if stop is None and take is None:
            return None
        if stop is not None and event.open <= stop:
            return TriggerState.TRIGGERED, PathAssumption.GAP_AT_OPEN, "EXEC_GAP_STOP"
        if take is not None and event.open >= take:
            return TriggerState.TRIGGERED, PathAssumption.GAP_AT_OPEN, "EXEC_TAKE_PROFIT_TRIGGERED"
        stop_hit = stop is not None and event.low <= stop
        take_hit = take is not None and event.high >= take
        if stop_hit and take_hit:
            if event.granularity is EventGranularity.DAILY_BAR:
                return TriggerState.TRIGGERED, PathAssumption.CONSERVATIVE_STOP_FIRST, "EXEC_STOP_FIRST_CONSERVATIVE"
            return TriggerState.UNVERIFIABLE, PathAssumption.STRICT_UNKNOWN, "EXEC_SEQUENCE_AMBIGUOUS"
        if stop_hit:
            return TriggerState.TRIGGERED, PathAssumption.POINT_SNAPSHOT, "EXEC_STOP_TRIGGERED"
        if take_hit:
            return TriggerState.TRIGGERED, PathAssumption.POINT_SNAPSHOT, "EXEC_TAKE_PROFIT_TRIGGERED"
        return None

    @staticmethod
    def _grade(event: ExecutionEvent | None, path: PathAssumption) -> ExecutionEvidenceGrade:
        if event is None or path is PathAssumption.STRICT_UNKNOWN: return ExecutionEvidenceGrade.INSUFFICIENT
        if path is PathAssumption.CONSERVATIVE_STOP_FIRST: return ExecutionEvidenceGrade.LOW
        if event.granularity is EventGranularity.INTRADAY_BAR and path is PathAssumption.EXACT_SEQUENCE: return ExecutionEvidenceGrade.HIGH
        if event.granularity is EventGranularity.DAILY_BAR: return ExecutionEvidenceGrade.MEDIUM
        return ExecutionEvidenceGrade.LOW

    def evaluate(self, intent: OrderIntent, events, *, execution_state: ExecutionState | None = None, replay_as_of: datetime | None = None, generated_at: datetime | None = None) -> TriggerEvaluation:
        values = self._events(events, replay_as_of); frozen = self._frozen_values(intent)
        observed: list[str] = []; state = TriggerState.NOT_TRIGGERED; trigger_event = invalidation_event = None
        path = PathAssumption.STRICT_UNKNOWN; selected = None; previous = None; detail_reason = None; saw_unknown = False
        for event in values:
            if event.instrument != intent.instrument: raise ContractViolation("event instrument differs from intent")
            if event.interval_end < intent.earliest_execution_at:
                previous = event; continue
            observed.append(event.event_id)
            if event.interval_start >= intent.expires_at:
                state = TriggerState.EXPIRED; selected = event; break
            protective = self._protective(event, intent, execution_state)
            if protective is not None:
                state, path, detail_reason = protective; selected = event
                if state is TriggerState.TRIGGERED: trigger_event = event
                break
            invalid, invalid_path = self._condition(intent.invalidation_condition, event, previous, frozen)
            trigger, trigger_path = self._condition(intent.trigger_condition, event, previous, frozen)
            confirmation, confirmation_path = True, trigger_path
            if intent.confirmation_condition:
                confirmation, confirmation_path = self._condition(intent.confirmation_condition, event, previous, frozen)
            if invalid is True and trigger is True:
                state = TriggerState.UNVERIFIABLE; path = PathAssumption.STRICT_UNKNOWN; selected = event; detail_reason = "EXEC_SEQUENCE_AMBIGUOUS"; break
            if invalid is True:
                state = TriggerState.INVALIDATED; invalidation_event = event; path = invalid_path; selected = event; break
            if invalid is None or trigger is None or confirmation is None:
                saw_unknown = True; selected = event; path = PathAssumption.STRICT_UNKNOWN
                if trigger_path is PathAssumption.STRICT_UNKNOWN and trigger is None:
                    break
                previous = event; continue
            if trigger and confirmation:
                # A range bar cannot prove whether a new long entry preceded a
                # same-bar stop touch.  Filling only the entry would introduce
                # optimistic path knowledge that the event does not contain.
                if (intent.side is OrderSide.BUY and intent.stop is not None and
                        event.granularity is not EventGranularity.QUOTE and event.low <= intent.stop):
                    state = TriggerState.UNVERIFIABLE
                    path = PathAssumption.STRICT_UNKNOWN
                    selected = event
                    detail_reason = "EXEC_SEQUENCE_AMBIGUOUS"
                    break
                state = TriggerState.TRIGGERED; trigger_event = event; path = self._merge_paths([trigger_path, confirmation_path]); selected = event; break
            previous = event
        if state is TriggerState.NOT_TRIGGERED and ((values and values[-1].interval_end >= intent.expires_at) or (replay_as_of is not None and replay_as_of >= intent.expires_at)):
            state = TriggerState.EXPIRED; selected = values[-1] if values else None
        elif state is TriggerState.NOT_TRIGGERED and saw_unknown:
            state = TriggerState.UNVERIFIABLE
        reason = detail_reason or {TriggerState.TRIGGERED: "EXEC_TRIGGERED", TriggerState.INVALIDATED: "EXEC_INVALIDATED", TriggerState.EXPIRED: "EXEC_PLAN_EXPIRED", TriggerState.UNVERIFIABLE: "EXEC_SEQUENCE_AMBIGUOUS", TriggerState.NOT_TRIGGERED: "EXEC_NOT_TRIGGERED", TriggerState.READY: "EXEC_NOT_TRIGGERED"}[state]
        batch = stable_hash(tuple(item.event_id for item in values)); grade = self._grade(selected, path)
        grades = {ExecutionEvidenceGrade.HIGH:"EXEC_EVIDENCE_HIGH",ExecutionEvidenceGrade.MEDIUM:"EXEC_EVIDENCE_MEDIUM",ExecutionEvidenceGrade.LOW:"EXEC_EVIDENCE_LOW",ExecutionEvidenceGrade.INSUFFICIENT:"EXEC_EVIDENCE_INSUFFICIENT"}
        identity = {"intent_id": intent.intent_id, "event_batch_hash": batch, "state": state, "evaluated_event_ids": tuple(observed), "trigger_event_id": trigger_event.event_id if trigger_event else None, "invalidation_event_id": invalidation_event.event_id if invalidation_event else None, "path": path, "grade": grade, "policy": self.policy.policy_version}
        identifier = stable_hash(identity); created = generated_at or (replay_as_of or intent.generated_at)
        trigger_time = None if trigger_event is None else (trigger_event.interval_start if path is PathAssumption.GAP_AT_OPEN else trigger_event.interval_end)
        invalidation_time = None if invalidation_event is None else (invalidation_event.interval_start if path is PathAssumption.GAP_AT_OPEN else invalidation_event.interval_end)
        return TriggerEvaluation(identifier, f"{intent.instrument.stable_key}|{identifier}", intent.intent_id, state, tuple(observed), batch, trigger_event.event_id if trigger_event else None, invalidation_event.event_id if invalidation_event else None, values[0].interval_start if values else None, selected.interval_end if selected else None, trigger_time, invalidation_time, selected.source if selected else "execution_event_sequence", selected.granularity if selected else None, path, grade, self.policy.policy_version, tuple(sorted({reason, grades[grade]})), created)
