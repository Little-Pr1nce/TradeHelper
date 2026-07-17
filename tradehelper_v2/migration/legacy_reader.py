"""V1 数据只读访问器。

这里故意不 import 任何 V1 Python 模块；旧库只被 SQLite 的 ``mode=ro`` 连接读取。
在 preflight 和 execute 之间会重新计算 fingerprint，防止用户在确认后修改旧库。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from tradehelper_v2.contracts.providers import MigrationPreflight

@dataclass(frozen=True, slots=True)
class LegacySource:
    path: Path

    def __post_init__(self):
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def stat_fingerprint(self) -> Mapping[str, Any]:
        if not self.exists:
            return {"exists": False, "path": str(self.path), "sha256": "", "mtime_ns": None, "size": 0}
        stat = self.path.stat()
        digest = sha256(self.path.read_bytes()).hexdigest()
        return {"exists": True, "path": str(self.path), "sha256": digest, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}

    def fingerprint(self) -> str:
        info = self.stat_fingerprint()
        # 缺失源也需要稳定的 64 位身份，计划才能被审计而不使用空字符串。
        return str(info["sha256"] or sha256(f"missing:{self.path}".encode()).hexdigest())

    def backup_manifest(self) -> Mapping[str, Any]:
        """供迁移页展示的可验证清单，不泄露配置内容。"""
        info=self.stat_fingerprint()
        return {"path":str(self.path),"size":info["size"],"mtime_ns":info["mtime_ns"],"sha256":self.fingerprint()}

    def connect(self) -> sqlite3.Connection:
        if not self.exists:
            raise FileNotFoundError(self.path)
        # URI mode=ro 是 SQLite 层面的只读锁，不使用临时写库或 PRAGMA 改写源文件。
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

class LegacyReader:
    """V1 SQLite/JSON 的最小结构化读取器。"""
    ACCOUNT_TABLES = ("holdings", "watchlist", "account_balance")
    METADATA_TABLES = ("stocks",)
    REPORT_TABLES = ("reports",)
    EVIDENCE_TABLES = (
        "price_history", "intraday_price_history", "news_sentiment",
        "prediction_log", "bt_variant_cache", "per_stock_params",
        "research_observation_log", "strategy_param_candidates",
        "deep_optimization_runs", "news_refresh_state", "forecast_log",
        "forecast_model_versions", "trade_plan_log", "feature_context_snapshots",
        "joint_oof_runs", "portfolio_analyses",
    )
    KNOWN_TABLES = ACCOUNT_TABLES + METADATA_TABLES + REPORT_TABLES + EVIDENCE_TABLES
    def __init__(self, source: LegacySource | Path | str):
        self.source = source if isinstance(source, LegacySource) else LegacySource(Path(source))

    def tables(self) -> tuple[str, ...]:
        if not self.source.exists: return ()
        with self.source.connect() as db:
            return tuple(sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")))

    def rows(self, table: str) -> tuple[dict[str, Any], ...]:
        if table not in self.tables(): return ()
        # 表名来自 sqlite_master 的白名单结果，不能由外部字符串直接注入 SQL。
        with self.source.connect() as db:
            safe_table=table.replace('"','""')
            return tuple(dict(row) for row in db.execute(f' SELECT * FROM "{safe_table}"'))

    def read_config(self, path: Path | str | None = None) -> Mapping[str, Any]:
        candidate = Path(path) if path else self.source.path.with_name("config.json")
        if not candidate.is_file(): return {}
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
            return value if isinstance(value, Mapping) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def preflight(self, as_of: datetime | None = None) -> MigrationPreflight:
        now = as_of or datetime.now(timezone.utc)
        tables = set(self.tables())
        counts = {name: len(self.rows(name)) for name in sorted(tables.intersection(self.KNOWN_TABLES))}
        warnings = () if self.source.exists else ("V1 database does not exist",)
        return MigrationPreflight(source_path=str(self.source.path), source_exists=self.source.exists,
            source_schema_detected=bool(tables.intersection(self.KNOWN_TABLES)), table_counts=counts,
            migratable_counts={key: counts.get(key, 0) for key in ("holdings", "watchlist", "account_balance")},
            conflict_counts={}, warnings=warnings, read_only=True, evaluated_at=now)
