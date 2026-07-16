"""LL49：纯本地研究验证不能把网络时间混入预算。"""
from time import perf_counter
from test_research_parser import _context_response
from tradehelper_v2.research.validator import DeterministicHypothesisValidator
from types import SimpleNamespace
from tradehelper_v2.contracts import HypothesisKind

def test_local_research_validation_twenty_items_is_fast(us_instrument,now):
    context,_,fact=_context_response(us_instrument,now); items=tuple(SimpleNamespace(hypothesis_id=f"h{index}",context_id=context.context_id,evidence_refs=(fact.fact_id,),payload=(("predicate",{"op":"gte","fact_ref":fact.fact_id,"constant":1}),),kind=HypothesisKind.FORECAST_PATTERN) for index in range(20)); started=perf_counter()
    for item in items: DeterministicHypothesisValidator().validate(item,context,evaluated_at=now)
    assert perf_counter()-started<.2
