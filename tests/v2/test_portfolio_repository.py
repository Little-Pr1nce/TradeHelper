"""PO48：migration 12、原子写入、幂等、隔离与强类型恢复。"""
from datetime import timedelta

import pytest

from portfolio_helpers import portfolio_batch
from strategy_helpers import position
from tradehelper_v2.contracts import ContractViolation
from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.portfolio import PortfolioDecisionEngine


def test_po48_migration_atomic_idempotent_quarantine_and_restart(tmp_path, us_instrument, now):
    path = tmp_path / "portfolio.sqlite"
    batch = portfolio_batch(us_instrument, position=position(us_instrument))
    first_bundle = PortfolioDecisionEngine().decide(batch, now)
    later_bundle = PortfolioDecisionEngine().decide(batch, now + timedelta(minutes=1))
    repository = SQLiteRepository(path)
    try:
        version_count = repository._connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=12"
        ).fetchone()[0]
        assert version_count == 1
        first = repository.save_portfolio_result(batch, first_bundle)
        second = repository.save_portfolio_result(batch, later_bundle)
        assert first[0].inserted == first[1].inserted == 1
        assert second[0].idempotent == second[1].idempotent == 1
    finally:
        repository.close()

    reopened = SQLiteRepository(path)
    try:
        assert reopened.get_portfolio_input_batch(batch.batch_id) == batch
        assert reopened.get_portfolio_decision_bundle(first_bundle.portfolio_bundle_id) == first_bundle
        allocation_id = first_bundle.conservative.allocations[0].allocation_id
        reopened._connection.execute("UPDATE portfolio_allocations SET action='corrupted' WHERE allocation_id=?",
                                     (allocation_id,))
        reopened._connection.commit()
        with pytest.raises(ContractViolation):
            reopened.get_portfolio_decision_bundle(first_bundle.portfolio_bundle_id)
    finally:
        reopened.close()

    conflict_path = tmp_path / "portfolio-conflict.sqlite"
    conflict_repo = SQLiteRepository(conflict_path)
    try:
        group = first_bundle.conservative.reservation_groups[0]
        conflict_repo.save_portfolio_input_batch(batch)
        conflict_repo._save_portfolio_bundle(first_bundle)
        conflict_repo._connection.execute(
            """INSERT INTO portfolio_reservation_groups(
                   group_id,event_key,portfolio_bundle_id,profile,instrument_key,side,
                   max_aggregate_shares,payload_json,generated_at,schema_version
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (group.group_id, group.group_id, first_bundle.portfolio_bundle_id,
             group.profile.value, group.instrument.stable_key, group.side,
             str(group.max_aggregate_shares), "{}", now.isoformat(), 1),
        )
        conflict_repo._connection.commit()
        result = conflict_repo.save_portfolio_result(batch, first_bundle)
        assert result[0].conflicts == result[1].conflicts == 1
        assert conflict_repo._connection.execute(
            "SELECT COUNT(*) FROM portfolio_allocations"
        ).fetchone()[0] == 0
        quarantined = conflict_repo._connection.execute(
            "SELECT COUNT(*) FROM quarantine_records WHERE record_type='portfolio_group_conflict'"
        ).fetchone()[0]
        assert quarantined == 1
    finally:
        conflict_repo.close()
