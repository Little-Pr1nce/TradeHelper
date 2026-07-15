from decimal import Decimal

from tradehelper_v2.contracts import PositionSnapshot
from tradehelper_v2.risk import RiskOfficer
from risk_helpers import NOW, request_for


def test_rk32_held_input_preserves_exit_decisions(us_instrument):
    position = PositionSnapshot(us_instrument, Decimal("10"), Decimal("100"), NOW)
    request = request_for(us_instrument, position=position)
    # 无持仓估值是现实缺失，退出决策仍必须出现在 bundle，不被静默删除。
    decisions = RiskOfficer().assess(request, generated_at=NOW).decisions
    assert any(item.action.value in {"sell", "reduce"} for item in decisions)
