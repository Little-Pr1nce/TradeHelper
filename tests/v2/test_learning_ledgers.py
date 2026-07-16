"""LE30-LE44：三本账的基本分账和联合收益口径。"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from tradehelper_v2.contracts import Market, OutcomeStatus
from tradehelper_v2.learning.joint import time_weighted_return
from tradehelper_v2.learning.ledgers import joint_ledger, strategy_ledger

from test_learning_golden_cases import _joint, _strategy_row

def test_joint_time_weighted_return_keeps_missing_benchmark_separate():
    assert time_weighted_return(Decimal('100'),Decimal('110'))==Decimal('0.1')
    assert time_weighted_return(Decimal('100'),Decimal('115'),Decimal('5'))==Decimal('0.1')


def test_strategy_ledger_excludes_pending_return_from_trade_metrics(us_instrument, now):
    matured=_strategy_row(
        us_instrument,
        now,
        trigger_state="triggered",
        fill_outcome="filled",
        net_return=Decimal(".02"),
    )
    pending=SimpleNamespace(**{**vars(matured),"status":OutcomeStatus.PENDING,"net_return":Decimal(".90")})
    summary=next(iter(strategy_ledger((matured,pending),cutoff_at=now).values()))
    assert summary["net_returns"] == [Decimal(".02")]
    assert summary["pending"] == 1


def test_joint_ledger_keeps_market_profile_kind_and_origin_separate(now):
    us=_joint(Market.US,now,profile="conservative")
    a=_joint(Market.A,now,profile="aggressive")
    future=replace(us,generated_at=now+timedelta(days=1))
    result=joint_ledger((us,a,future),cutoff_at=now)
    assert len(result) == 2
    assert {key[:3] for key in result} == {
        ("US","USD","conservative"),
        ("A","CNY","aggressive"),
    }
