from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from config.settings import V2Settings
from contracts import (
    DecisionMode,
    Exchange,
    InstrumentId,
    Market,
    RiskPolicy,
    SingleStockAnalysisCommand,
    ValidationStatus,
    stable_hash,
)
from data.repository import SQLiteRepository
from contracts.portfolio import PortfolioPolicy
from runtime import build_runtime_container
from strategies.registry import default_specs


def _container(tmp_path):
    return build_runtime_container(V2Settings.from_mapping({"work_dir": str(tmp_path)}))


def test_RL00_main_production_path_imports_only_v2():
    source = Path("main.py").read_text(encoding="utf-8")
    assert all(f"from {name}" in source for name in ("runtime", "data", "ui"))
    assert all(f"from {name}" not in source for name in ("alpha", "backtest", "core", "report", "services"))


def test_RL01_runtime_container_builds_all_frozen_layers(tmp_path):
    container = _container(tmp_path)
    try:
        assert all(getattr(container, name) is not None for name in (
            "data_refresh", "feature_builder", "forecast_engine", "scenario_planner",
            "strategy_engine", "risk_officer", "order_intent_factory",
            "portfolio_engine", "learning_engine", "analysis",
        ))
    finally:
        container.close()


def test_RL02_startup_order_is_settings_schema_migration_then_ui():
    source = Path("runtime/lifecycle.py").read_text(encoding="utf-8")
    source = source[source.index("def start_runtime"):]
    assert source.index("V2Settings.load") < source.index("ensure_work_dir")
    assert source.index("build_runtime_container") < source.index("find_completed_migration")
    main=Path("main.py").read_text(encoding="utf-8")
    body=main[main.index("def _main"):]
    assert body.index("start_runtime") < body.index("build_app")


def test_RL03_close_stops_executors_before_repository_use(tmp_path):
    container = _container(tmp_path)
    container.close()
    container.close()
    assert container.closed
    with pytest.raises(RuntimeError):
        container.background_executor.submit(lambda: None)


def test_RL04_command_identity_binds_account_mode_and_cutoff():
    instrument = InstrumentId("AAPL", Market.US, Exchange.XNAS)
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    def command(account, mode, requested):
        raw={"instrument":instrument,"mode":mode,"history":"3m","requested_at":requested,"account":account,"force_refresh":False}
        return SingleStockAnalysisCommand(stable_hash(raw),instrument,mode,"3m",requested,account)
    values={command("a",DecisionMode.EOD,now).command_id,command("b",DecisionMode.EOD,now).command_id,command("a",DecisionMode.PRE,now).command_id,command("a",DecisionMode.EOD,now.replace(hour=1)).command_id}
    assert len(values)==4


def test_RL05_analysis_versions_are_frozen_for_one_run():
    assert RiskPolicy().policy_version == "risk_policy_v1"
    assert PortfolioPolicy().policy_version == "portfolio_policy_v1"
    specs = default_specs()
    assert specs and len({item.strategy_id for item in specs}) == len(specs)


def test_RL06_missing_real_account_is_not_synthesized(tmp_path):
    container = _container(tmp_path)
    try:
        assert container.repository.get_latest_account_snapshot(Market.US) is None
        with pytest.raises(ValueError, match="真实账户"):
            container.analysis.start_single({"market":"US","symbol":"AAPL"})
    finally:
        container.close()


def test_RL07_runtime_and_application_do_not_import_v1_business_modules():
    forbidden=("alpha", "backtest", "core", "report", "services")
    for root in (Path("runtime"), Path("application")):
        for path in root.rglob("*.py"):
            source=path.read_text(encoding="utf-8",errors="ignore")
            assert all(f"from {name}." not in source and f"import {name}." not in source for name in forbidden)


def test_RL08_empty_workdir_starts_without_fake_account(tmp_path):
    container = _container(tmp_path / "new-user")
    try:
        assert container.settings.work_dir.is_dir()
        assert container.health().database_status == "ready"
        assert container.repository.get_latest_account_snapshot(Market.A) is None
    finally:
        container.close()


def test_RL09_migration_17_is_idempotent_across_restart(tmp_path):
    first = _container(tmp_path)
    first.close()
    second = _container(tmp_path)
    try:
        rows=second.repository._connection.execute("SELECT version,COUNT(*) FROM schema_migrations WHERE version=17 GROUP BY version").fetchone()
        assert tuple(rows)==(17,1)
    finally:
        second.close()


def test_runtime_restores_latest_forecast_validation_reason(tmp_path):
    settings = V2Settings.from_mapping({"work_dir": str(tmp_path)})
    repository = SQLiteRepository(settings.database_path)
    instrument = InstrumentId("AAPL", Market.US, Exchange.XNAS)
    repository.save_forecast_validation_summary(
        market=Market.US, scope_key=instrument.stable_key, horizon=3,
        status=ValidationStatus.CALIBRATION_FAILED,
        reason="confirmation calibration failed", data_hash=stable_hash("training"),
        created_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    repository.close()

    container = build_runtime_container(settings)
    try:
        assert container.forecast_registry.last_validation(
            market=Market.US, scope_key=instrument.stable_key, horizon=3,
        ) == (ValidationStatus.CALIBRATION_FAILED, "confirmation calibration failed")
    finally:
        container.close()
