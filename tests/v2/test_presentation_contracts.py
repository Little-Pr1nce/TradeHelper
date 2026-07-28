"""UX00--UX09: strong presentation contracts, identity, and migration 16."""
from dataclasses import replace
from datetime import timedelta

import pytest

from presentation_helpers import single_presentation
from contracts import ContractViolation
from data.repository import SQLiteRepository
from data.migrations.schema import SCHEMA_VERSION
from presentation.report_builder import SingleStockReportBuilder


def _input(instrument, now):
    return single_presentation(instrument, now=now, calendar=None)


def _document(value):
    return SingleStockReportBuilder().build(value)


def test_ux00_single_stock_input_builds(us_instrument, now):
    value = _input(us_instrument, now)
    assert value.instrument == us_instrument
    assert tuple(item.horizon for item in value.forecasts) == (1, 3, 5, 10)


def test_input_built_before_asof_is_rejected(us_instrument, now):
    with pytest.raises(ContractViolation):
        replace(_input(us_instrument, now), built_at=now - timedelta(days=1))


def test_ux02_document_sources_close_over_every_block(us_instrument, now):
    document = _document(_input(us_instrument, now))
    block_refs = {
        ref for section in document.sections for block in section.blocks
        for ref in block.source_artifact_refs
    }
    row_refs = {
        ref for section in document.sections for block in section.blocks
        if hasattr(block.payload, "rows") for row in block.payload.rows
        for ref in row.source_artifact_refs
    }
    assert block_refs | row_refs == set(document.source_artifact_refs)


def test_ux03_document_is_deterministic(us_instrument, now):
    assert _document(_input(us_instrument, now)).report_id == _document(_input(us_instrument, now)).report_id


def test_ux09_migration_16_restarts(tmp_path):
    path = tmp_path / "p.sqlite"
    repo = SQLiteRepository(path); repo.close()
    repo = SQLiteRepository(path)
    try:
        assert repo._connection.execute("select max(version) from schema_migrations").fetchone()[0] == SCHEMA_VERSION
    finally:
        repo.close()
