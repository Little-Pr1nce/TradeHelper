import time
from datetime import timedelta

from tradehelper_v2.strategies import StrategyEngine
from strategy_helpers import strategy_input


def test_sp27_one_thousand_pure_memory_bundles_are_fast(us_instrument):
    input = strategy_input(us_instrument); engine = StrategyEngine(); start = time.perf_counter()
    for index in range(1000):
        # Different issuance timestamps bypass the replay cache and exercise full bundle construction.
        engine.build(input, generated_at=input.as_of + timedelta(microseconds=index))
    assert time.perf_counter() - start < 1.5
