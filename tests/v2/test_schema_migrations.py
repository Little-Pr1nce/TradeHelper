from datetime import date
from hashlib import sha256
from pathlib import Path
import sqlite3

from tradehelper_v2.data.repository import SQLiteRepository
from tradehelper_v2.data.migrations.schema import apply_schema, schema_checksum, schema_v2_checksum, schema_v3_checksum, schema_v4_checksum, schema_v5_checksum, schema_v6_checksum, schema_v7_checksum


def test_g53_v1_database_remains_untouched(tmp_path, now) -> None:
    v1 = tmp_path / "tradehelper.db"
    v1.write_bytes(b"V1 immutable test bytes")
    before = sha256(v1.read_bytes()).hexdigest(), v1.stat().st_mtime_ns
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    after = sha256(v1.read_bytes()).hexdigest(), v1.stat().st_mtime_ns
    assert before == after and repo.database_path.name == "tradehelper_v2.db"
    repo.close()


def test_g54_schema_migrations_are_idempotent(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "tradehelper_v2.db")
    apply_schema(connection)
    apply_schema(connection)
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=1").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=2").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=3").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=4").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=5").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=6").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=7").fetchone()[0] == 1
    assert schema_checksum() == schema_checksum()
    assert schema_v2_checksum() == schema_v2_checksum()
    assert schema_v3_checksum() == schema_v3_checksum()
    assert schema_v4_checksum() == schema_v4_checksum()
    assert schema_v5_checksum() == schema_v5_checksum()
    assert schema_v6_checksum() == schema_v6_checksum()
    assert schema_v7_checksum() == schema_v7_checksum()
    connection.close()


def test_g55_v1_preflight_is_read_only(tmp_path, now) -> None:
    v1 = tmp_path / "tradehelper.db"
    connection = sqlite3.connect(v1)
    connection.execute("CREATE TABLE holdings(code TEXT)")
    connection.execute("CREATE TABLE watchlist(code TEXT)")
    connection.execute("CREATE TABLE account_balance(cash REAL)")
    connection.executemany("INSERT INTO holdings VALUES (?)", [("AAPL",), ("MU",)])
    connection.executemany("INSERT INTO watchlist VALUES (?)", [("AAPL",), ("MU",), ("FCX",)])
    connection.execute("INSERT INTO account_balance VALUES (1000)")
    connection.commit(); connection.close()
    before = sha256(v1.read_bytes()).hexdigest()
    repo = SQLiteRepository(tmp_path / "tradehelper_v2.db")
    result = repo.migration_preflight(v1, now)
    assert result.read_only and result.migratable_counts == {"holdings": 2, "watchlist": 3, "account_balance": 1}
    assert sha256(v1.read_bytes()).hexdigest() == before
    assert repo._connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0] == 0
    repo.close()
