"""LE59：学习指标在冻结合成数据上的性能边界。"""
from time import perf_counter
from tradehelper_v2.learning.metrics import strategy_summary

def test_learning_ledger_ten_thousand_returns_finishes_within_budget():
    values=tuple(((index%17)-8)/1000 for index in range(10_000)); started=perf_counter(); summary=strategy_summary(values)
    assert summary['sample_count']==10_000 and perf_counter()-started<1.0
