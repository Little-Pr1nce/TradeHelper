import time

from tradehelper_v2.risk import RiskOfficer
from risk_helpers import request_for


def test_rk41_one_thousand_pure_memory_bundles_are_fast(us_instrument):
    request = request_for(us_instrument); officer = RiskOfficer(); start = time.perf_counter()
    for _ in range(1000):
        officer.assess(request, generated_at=request.as_of)
    assert time.perf_counter() - start < 2.0
