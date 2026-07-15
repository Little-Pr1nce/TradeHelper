"""V2-7 EX10--EX19：触发、失效、跳空和时间边界。"""
from datetime import timedelta
from decimal import Decimal

import pytest

from execution_helpers import intent_for
from tradehelper_v2.contracts import ExecutionEvent, ExecutionPolicy, EventGranularity, TradingStatus
from tradehelper_v2.contracts.market_data import ContractViolation
from tradehelper_v2.execution import TriggerEngine


def event(instrument, now, name, price, *, available=None):
    return ExecutionEvent(name, instrument, now.date(), now, now, EventGranularity.QUOTE, Decimal(price), Decimal(price), Decimal(price), Decimal(price), Decimal("100"), Decimal("99"), None, None, TradingStatus.OPEN, "fixture", "high", available or now, now)


def test_snapshot_trigger_and_invalidation_order(us_instrument, now):
    intent = intent_for(us_instrument, now)
    triggered = TriggerEngine(ExecutionPolicy()).evaluate(intent, (event(us_instrument, now, "up", "101"),), replay_as_of=now)
    invalidated = TriggerEngine(ExecutionPolicy()).evaluate(intent, (event(us_instrument, now, "down", "89"),), replay_as_of=now)
    assert triggered.state.value == "triggered"
    assert invalidated.state.value == "invalidated"


def test_future_event_and_expiry_are_not_silently_accepted(us_instrument, now):
    intent = intent_for(us_instrument, now)
    with pytest.raises(ContractViolation):
        TriggerEngine(ExecutionPolicy()).evaluate(intent, (event(us_instrument, now, "future", "101", available=now + timedelta(seconds=1)),), replay_as_of=now)
    expired = TriggerEngine(ExecutionPolicy()).evaluate(intent, (), replay_as_of=intent.expires_at)
    assert expired.state.value == "expired"
