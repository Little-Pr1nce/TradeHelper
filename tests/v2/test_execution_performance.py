"""V2-7 EX49：纯内存订单工厂的宽松性能边界。"""
from time import perf_counter

from risk_helpers import request_for
from contracts import ExecutionPolicy
from execution import OrderIntentFactory
from risk import RiskOfficer


def test_order_intent_factory_stays_in_memory_and_fast(us_instrument, calendar, now):
    request=request_for(us_instrument,as_of=now); risks=RiskOfficer().assess(request,generated_at=now)
    plans={plan.plan_id:plan for branch in (request.strategy_bundle.entry_or_add,request.strategy_bundle.reduce_or_exit,request.strategy_bundle.hold,request.strategy_bundle.invalidation) for plan in branch.plans if any(item.plan_id==plan.plan_id for item in risks.decisions)}
    factory=OrderIntentFactory(calendar); started=perf_counter()
    for _ in range(100): factory.build_bundle(risks,plans,{},now,ExecutionPolicy())
    assert perf_counter()-started < 5
