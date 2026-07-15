"""V2 独立数据库的不可变、可重复执行迁移。

历史迁移 SQL 和 checksum 发布后不得修改；结构演进只能添加新版本，避免
部署后的 schema_migrations 无法判断旧迁移是否被篡改。
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import sqlite3

SCHEMA_VERSION = 10

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_metadata (
    instrument_key TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    name TEXT NOT NULL,
    industry TEXT,
    description TEXT,
    listing_date TEXT,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_bars (
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    adjustment_mode TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    corporate_action_version TEXT,
    payload_hash TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (instrument_key, trading_date, adjustment_mode)
);
CREATE INDEX IF NOT EXISTS idx_v2_daily_bars_scope
ON daily_bars(instrument_key, trading_date);

CREATE TABLE IF NOT EXISTS intraday_bars (
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    session_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER,
    source TEXT NOT NULL,
    evidence_quality TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (instrument_key, observed_at)
);

CREATE TABLE IF NOT EXISTS quote_snapshots (
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    session TEXT NOT NULL,
    price REAL NOT NULL,
    prev_close REAL,
    open REAL,
    high REAL,
    low REAL,
    volume INTEGER,
    bid REAL,
    ask REAL,
    observed_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    freshness_status TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (instrument_key, session, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_v2_quotes_latest
ON quote_snapshots(instrument_key, session, observed_at DESC);

CREATE TABLE IF NOT EXISTS news_snapshots (
    stable_key TEXT PRIMARY KEY,
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content TEXT,
    is_macro INTEGER NOT NULL,
    finbert_label TEXT,
    finbert_score REAL,
    relevance REAL,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_news_asof
ON news_snapshots(instrument_key, available_at);

CREATE TABLE IF NOT EXISTS fundamental_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    UNIQUE (instrument_key, available_at, provider)
);
CREATE INDEX IF NOT EXISTS idx_v2_fundamentals_asof
ON fundamental_snapshots(instrument_key, available_at DESC);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    currency TEXT NOT NULL,
    cash TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    UNIQUE (market, captured_at)
);
CREATE TABLE IF NOT EXISTS account_positions (
    account_snapshot_id INTEGER NOT NULL,
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    shares TEXT NOT NULL,
    cost_price TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (account_snapshot_id, instrument_key),
    FOREIGN KEY (account_snapshot_id) REFERENCES account_snapshots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS quarantine_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,
    instrument_key TEXT,
    trading_date TEXT,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_quarantine_scope
ON quarantine_records(instrument_key, trading_date);
""".strip()

_SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS provider_rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    market TEXT NOT NULL,
    data_type TEXT NOT NULL,
    requested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_provider_rate_scope
ON provider_rate_events(provider, market, data_type, requested_at);

CREATE TABLE IF NOT EXISTS refresh_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    listing_date TEXT,
    priority INTEGER NOT NULL,
    next_retry_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_type, instrument_key, requested_start, requested_end, status)
);
CREATE INDEX IF NOT EXISTS idx_v2_refresh_queue_due
ON refresh_queue(status, next_retry_at, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS daily_bar_drift_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    primary_source TEXT NOT NULL,
    comparator_source TEXT NOT NULL,
    max_abs_price_diff REAL NOT NULL,
    volume_ratio REAL,
    status TEXT NOT NULL,
    primary_payload_json TEXT NOT NULL,
    comparator_payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(instrument_key, trading_date, primary_source, comparator_source, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_v2_daily_drift_scope
ON daily_bar_drift_records(instrument_key, trading_date DESC);
""".strip()

_SCHEMA_V3_SQL = """
CREATE TABLE IF NOT EXISTS provider_refresh_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    mode TEXT,
    next_retry_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_type, instrument_key, mode, status)
);
CREATE INDEX IF NOT EXISTS idx_v2_provider_refresh_due
ON provider_refresh_queue(status, next_retry_at, created_at);
""".strip()

_SCHEMA_V4_SQL = """
DROP INDEX IF EXISTS idx_v2_refresh_queue_due;
ALTER TABLE refresh_queue RENAME TO refresh_queue_legacy_v3;
CREATE TABLE refresh_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    listing_date TEXT,
    priority INTEGER NOT NULL,
    next_retry_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_type, instrument_key, requested_start, requested_end)
);
INSERT OR REPLACE INTO refresh_queue
SELECT * FROM refresh_queue_legacy_v3
WHERE id IN (
    SELECT MAX(id) FROM refresh_queue_legacy_v3
    GROUP BY task_type, instrument_key, requested_start, requested_end
);
DROP TABLE refresh_queue_legacy_v3;
CREATE INDEX idx_v2_refresh_queue_due
ON refresh_queue(status, next_retry_at, priority DESC, created_at);

DROP INDEX IF EXISTS idx_v2_provider_refresh_due;
ALTER TABLE provider_refresh_queue RENAME TO provider_refresh_queue_legacy_v3;
CREATE TABLE provider_refresh_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT '',
    next_retry_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_type, instrument_key, mode)
);
INSERT OR REPLACE INTO provider_refresh_queue
SELECT id, task_type, instrument_key, code, market, exchange, COALESCE(mode, ''),
       next_retry_at, status, attempts, created_at, updated_at
FROM provider_refresh_queue_legacy_v3
WHERE id IN (
    SELECT MAX(id) FROM provider_refresh_queue_legacy_v3
    GROUP BY task_type, instrument_key, COALESCE(mode, '')
);
DROP TABLE provider_refresh_queue_legacy_v3;
CREATE INDEX idx_v2_provider_refresh_due
ON provider_refresh_queue(status, next_retry_at, created_at);

UPDATE news_snapshots SET available_at=fetched_at WHERE available_at < fetched_at;
""".strip()

_SCHEMA_V5_SQL = """
CREATE TABLE IF NOT EXISTS feature_snapshots (
    instrument_key TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    mode TEXT NOT NULL,
    cutoff_at TEXT NOT NULL,
    latest_bar_date TEXT,
    feature_set_version TEXT NOT NULL,
    evidence_mode TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    feature_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (instrument_key, mode, cutoff_at, feature_set_version, input_hash)
);
CREATE INDEX IF NOT EXISTS idx_v2_features_lookup
ON feature_snapshots(instrument_key, mode, cutoff_at DESC, feature_set_version);
""".strip()

_SCHEMA_V6_SQL = """
CREATE TABLE IF NOT EXISTS forecast_model_versions (
    version TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    training_start TEXT NOT NULL,
    training_end TEXT NOT NULL,
    selection_start TEXT,
    selection_end TEXT,
    confirmation_start TEXT,
    confirmation_end TEXT,
    training_data_hash TEXT NOT NULL,
    artifact_format TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    artifact BLOB NOT NULL,
    random_seed INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    promoted_at TEXT,
    schema_version INTEGER NOT NULL,
    UNIQUE(market, scope, scope_key, horizon, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_v2_forecast_one_champion
ON forecast_model_versions(market, scope, scope_key, horizon)
WHERE lifecycle = 'champion';

CREATE TABLE IF NOT EXISTS forecast_model_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version TEXT NOT NULL,
    phase TEXT NOT NULL,
    data_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(model_version, phase, data_hash),
    FOREIGN KEY(model_version) REFERENCES forecast_model_versions(version)
);

CREATE TABLE IF NOT EXISTS forecast_snapshots (
    event_key TEXT PRIMARY KEY,
    instrument_key TEXT NOT NULL,
    origin_session_date TEXT NOT NULL,
    target_session_date TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_forecast_snapshots_lookup
ON forecast_snapshots(instrument_key, origin_session_date, horizon);

CREATE TABLE IF NOT EXISTS forecast_promotion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    previous_version TEXT,
    promoted_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(promoted_version),
    FOREIGN KEY(promoted_version) REFERENCES forecast_model_versions(version)
);
""".strip()

_SCHEMA_V7_SQL = """
ALTER TABLE forecast_model_versions ADD COLUMN sample_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE forecast_model_versions ADD COLUMN oof_sample_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE forecast_snapshots RENAME TO forecast_snapshots_legacy_v6;
CREATE TABLE forecast_snapshots (
    event_key TEXT PRIMARY KEY,
    instrument_key TEXT NOT NULL,
    origin_session_date TEXT NOT NULL,
    target_session_date TEXT,
    horizon INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);
INSERT INTO forecast_snapshots
SELECT event_key, instrument_key, origin_session_date, target_session_date, horizon,
       model_version, payload_json, payload_hash, generated_at, schema_version
FROM forecast_snapshots_legacy_v6;
DROP TABLE forecast_snapshots_legacy_v6;
CREATE INDEX idx_v2_forecast_snapshots_lookup
ON forecast_snapshots(instrument_key, origin_session_date, horizon);
""".strip()

_SCHEMA_V8_SQL = """
CREATE TABLE IF NOT EXISTS trading_scenarios (
    scenario_id TEXT PRIMARY KEY,
    event_key TEXT UNIQUE NOT NULL,
    instrument_key TEXT NOT NULL,
    market TEXT NOT NULL,
    exchange TEXT NOT NULL,
    mode TEXT NOT NULL,
    origin_session_date TEXT NOT NULL,
    decision_session_date TEXT,
    forecast_bundle_hash TEXT NOT NULL,
    current_feature_hash TEXT NOT NULL,
    fact_update_hash TEXT NOT NULL,
    quality_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_scenarios_lookup ON trading_scenarios(instrument_key, mode, decision_session_date);
""".strip()

_SCHEMA_V9_SQL = """
CREATE TABLE IF NOT EXISTS trade_plans (
    plan_id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, instrument_key TEXT NOT NULL,
    scenario_id TEXT NOT NULL, strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    family TEXT NOT NULL, action TEXT NOT NULL, readiness TEXT NOT NULL,
    decision_session_date TEXT, payload_json TEXT NOT NULL, generated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_trade_plans_lookup ON trade_plans(instrument_key, scenario_id, action);
CREATE TABLE IF NOT EXISTS strategy_bundles (
    bundle_id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, instrument_key TEXT NOT NULL,
    scenario_id TEXT NOT NULL, position_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
    generated_at TEXT NOT NULL, schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_strategy_bundles_lookup ON strategy_bundles(instrument_key, scenario_id);
""".strip()

_SCHEMA_V10_SQL = """
CREATE TABLE IF NOT EXISTS frozen_account_valuations (
    valuation_id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, market TEXT NOT NULL,
    currency TEXT NOT NULL, account_hash TEXT NOT NULL, price_batch_hash TEXT NOT NULL,
    valuation_at TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
    generated_at TEXT NOT NULL, schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_frozen_valuations_lookup ON frozen_account_valuations(market, valuation_at);
CREATE TABLE IF NOT EXISTS execution_decisions (
    decision_id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, instrument_key TEXT NOT NULL,
    scenario_id TEXT NOT NULL, bundle_id TEXT NOT NULL, plan_id TEXT NOT NULL, profile TEXT NOT NULL,
    level TEXT NOT NULL, disposition TEXT NOT NULL, account_hash TEXT, valuation_id TEXT,
    payload_json TEXT NOT NULL, generated_at TEXT NOT NULL, schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_execution_decisions_lookup ON execution_decisions(instrument_key, scenario_id, plan_id);
CREATE TABLE IF NOT EXISTS risk_decision_bundles (
    risk_bundle_id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, instrument_key TEXT NOT NULL,
    scenario_id TEXT NOT NULL, strategy_bundle_id TEXT NOT NULL, account_hash TEXT, valuation_id TEXT,
    payload_json TEXT NOT NULL, generated_at TEXT NOT NULL, schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_risk_bundles_lookup ON risk_decision_bundles(instrument_key, scenario_id);
""".strip()


def schema_checksum() -> str:
    return sha256(_SCHEMA_SQL.encode("utf-8")).hexdigest()


def schema_v2_checksum() -> str:
    return sha256(_SCHEMA_V2_SQL.encode("utf-8")).hexdigest()


def schema_v3_checksum() -> str:
    return sha256(_SCHEMA_V3_SQL.encode("utf-8")).hexdigest()


def schema_v4_checksum() -> str:
    return sha256(_SCHEMA_V4_SQL.encode("utf-8")).hexdigest()


def schema_v5_checksum() -> str:
    return sha256(_SCHEMA_V5_SQL.encode("utf-8")).hexdigest()


def schema_v6_checksum() -> str:
    return sha256(_SCHEMA_V6_SQL.encode("utf-8")).hexdigest()


def schema_v7_checksum() -> str:
    return sha256(_SCHEMA_V7_SQL.encode("utf-8")).hexdigest()
def schema_v8_checksum() -> str:
    return sha256(_SCHEMA_V8_SQL.encode("utf-8")).hexdigest()
def schema_v9_checksum() -> str:
    return sha256(_SCHEMA_V9_SQL.encode("utf-8")).hexdigest()
def schema_v10_checksum() -> str:
    return sha256(_SCHEMA_V10_SQL.encode("utf-8")).hexdigest()


def _apply_migration(connection: sqlite3.Connection, version: int, sql: str, checksum: str) -> None:
    """执行一次迁移并固化 checksum；重复执行只校验，不重放旧 SQL。"""
    migrations_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if migrations_exists:
        existing = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if existing is not None:
            if existing[0] != checksum:
                raise RuntimeError(f"V2 schema version {version} checksum changed; create a new migration")
            return
    connection.executescript(sql)
    applied_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    connection.execute(
        "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
        (version, checksum, applied_at),
    )


def apply_schema(connection: sqlite3.Connection) -> None:
    """按序应用 V2 迁移，不读取、更不修改 V1 数据库。"""
    connection.execute("PRAGMA foreign_keys = ON")
    _apply_migration(connection, 1, _SCHEMA_SQL, schema_checksum())
    _apply_migration(connection, 2, _SCHEMA_V2_SQL, schema_v2_checksum())
    _apply_migration(connection, 3, _SCHEMA_V3_SQL, schema_v3_checksum())
    _apply_migration(connection, 4, _SCHEMA_V4_SQL, schema_v4_checksum())
    _apply_migration(connection, 5, _SCHEMA_V5_SQL, schema_v5_checksum())
    _apply_migration(connection, 6, _SCHEMA_V6_SQL, schema_v6_checksum())
    _apply_migration(connection, 7, _SCHEMA_V7_SQL, schema_v7_checksum())
    _apply_migration(connection, 8, _SCHEMA_V8_SQL, schema_v8_checksum())
    _apply_migration(connection, 9, _SCHEMA_V9_SQL, schema_v9_checksum())
    _apply_migration(connection, 10, _SCHEMA_V10_SQL, schema_v10_checksum())
    connection.commit()
