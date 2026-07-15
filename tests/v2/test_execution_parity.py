"""V2-7 EX06：当前与历史消费者必须引用同一业务 OrderIntent。"""
from risk_helpers import request_for
from tradehelper_v2.contracts import ExecutionPolicy
from tradehelper_v2.execution import OrderIntentFactory
from tradehelper_v2.risk import RiskOfficer


def test_same_plan_decision_and_requested_shares_produce_same_intent(us_instrument, calendar, now):
    request=request_for(us_instrument,as_of=now)
    risks=RiskOfficer().assess(request,generated_at=now)
    plans={plan.plan_id:plan for branch in (request.strategy_bundle.entry_or_add,request.strategy_bundle.reduce_or_exit,request.strategy_bundle.hold,request.strategy_bundle.invalidation) for plan in branch.plans if any(item.plan_id==plan.plan_id for item in risks.decisions)}
    factory=OrderIntentFactory(calendar)
    current=factory.build_bundle(risks,plans,{},now,ExecutionPolicy())
    historical=factory.build_bundle(risks,plans,{},now,ExecutionPolicy())
    assert tuple(item.intent_id for item in current.intents)==tuple(item.intent_id for item in historical.intents)
    assert tuple(item.build_id for item in current.records)==tuple(item.build_id for item in historical.records)
