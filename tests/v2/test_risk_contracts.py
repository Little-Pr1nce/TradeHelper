from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from tradehelper_v2.contracts import ContractViolation, RiskAdjustment, RiskPolicy, ValuationPrice, ValuationPriceKind, FreshnessStatus
from tradehelper_v2.risk import RiskOfficer
from risk_helpers import request_for


def test_rk00_risk_policy_hard_caps_are_immutable():
    with pytest.raises(ContractViolation):
        RiskPolicy(single_position_hard_cap=Decimal("0.30"))


def test_rk00_fresh_quote_requires_fresh_status(us_instrument, now):
    with pytest.raises(ContractViolation):
        ValuationPrice(us_instrument, Decimal("1"), now, "fixture", ValuationPriceKind.FRESH_QUOTE, FreshnessStatus.STALE)


def test_rk00_policy_rejects_inverted_profiles_and_capacity_increases():
    with pytest.raises(ContractViolation):
        RiskPolicy(conservative_risk_pct=Decimal("0.02"), aggressive_risk_pct=Decimal("0.01"))
    with pytest.raises(ContractViolation):
        RiskAdjustment("RISK_SMALL_SAMPLE", Decimal("1.01"))


def test_rk00_account_valuation_binding_and_generated_at_idempotency(us_instrument, now):
    request = request_for(us_instrument)
    wrong_account = replace(request.account_snapshot, cash=request.account_snapshot.cash + Decimal("1"))
    with pytest.raises(ContractViolation):
        replace(request, account_snapshot=wrong_account)
    later_valuation = replace(request.valuation, generated_at=now + timedelta(seconds=1))
    assert later_valuation.valuation_id == request.valuation.valuation_id
    first = RiskOfficer().assess(request, generated_at=now)
    later = RiskOfficer().assess(request, generated_at=now + timedelta(seconds=1))
    assert first.risk_bundle_id == later.risk_bundle_id
    assert tuple(item.decision_id for item in first.decisions) == tuple(item.decision_id for item in later.decisions)
