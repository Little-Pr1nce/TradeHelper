"""所有情景下的显式条件观察计划。"""
from __future__ import annotations

from contracts import ConditionOperator, PlanAction, TakeProfitMode
from .common import Proposal, all_of, always_false, compare, feature


def conditional_observation(ctx, spec, missing_conditions=(), reason_codes=()):
    missing = tuple(sorted(set(missing_conditions)))
    if missing:
        checks = tuple(compare(feature(name), ConditionOperator.EQUALS, feature(name), "FEATURE_MISSING") for name in missing)
        condition = checks[0] if len(checks) == 1 else all_of(*checks, reason="FEATURE_MISSING")
    else:
        condition = always_false("PLAN_OBSERVATION_ONLY")
    reasons = tuple(sorted(set(("PLAN_OBSERVATION_ONLY",) + tuple(reason_codes) + (("FEATURE_MISSING",) if missing else ()))))
    return Proposal(PlanAction.WATCH, condition, None, None, None, None, None, None, TakeProfitMode.NONE, None, always_false("PLAN_OBSERVATION_ONLY"), None, reasons, missing, explicit_missing_conditions=missing)
