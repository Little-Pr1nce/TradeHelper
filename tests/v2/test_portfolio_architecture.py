"""PO49：双市场隔离、架构边界与大组合性能。"""
import ast
from pathlib import Path
from time import perf_counter

from portfolio_helpers import portfolio_batch, portfolio_batch_many
from tradehelper_v2.contracts import InstrumentId, Market
from tradehelper_v2.portfolio import PortfolioDecisionEngine


def test_po49_dual_market_architecture_and_500_candidate_performance(
    us_instrument, a_instrument, now,
):
    us = PortfolioDecisionEngine().decide(portfolio_batch(us_instrument), now)
    china = PortfolioDecisionEngine().decide(portfolio_batch(a_instrument), now)
    assert us.market is Market.US and china.market is Market.A
    assert us.account_hash != china.account_hash

    portfolio_dir = Path(__file__).resolve().parents[2] / "tradehelper_v2" / "portfolio"
    forbidden = ("services", "tradehelper_v2.ui", "tradehelper_v2.report",
                 "tradehelper_v2.learning", "tradehelper_v2.data.providers")
    for path in portfolio_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any(name.startswith(forbidden) for name in imports)

    instruments = tuple(InstrumentId.from_code(f"S{index:03}", Market.US, "XNAS")
                        for index in range(100))
    batch = portfolio_batch_many(instruments)
    assert len(batch.candidates) >= 500
    started = perf_counter()
    result = PortfolioDecisionEngine().decide(batch, now)
    elapsed = perf_counter() - started
    assert len(result.conservative.allocations) + len(result.aggressive.allocations) == len(batch.candidates)
    assert elapsed < 1.5
