"""SQLite persistence boundary for the isolated V2 data store."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterable, Iterator, Mapping

from tradehelper_v2.contracts.account import AccountSnapshot, PositionSnapshot
from tradehelper_v2.contracts.enums import (
    AdjustmentMode,
    DecisionMode,
    Exchange,
    FreshnessStatus,
    Market,
    ProviderStatus,
    QualityStatus,
    TradingSession,
)
from tradehelper_v2.contracts.market_data import (
    CanonicalBar,
    ContractViolation,
    FundamentalSnapshot,
    FundamentalValue,
    InstrumentId,
    IntradayBar,
    NewsSnapshot,
    QuoteSnapshot,
    StockMetadata,
    canonical_json,
    ensure_utc,
    stable_hash,
    utc_iso,
)
from tradehelper_v2.contracts.providers import DailyBarsRequest, MigrationPreflight
from .migrations.schema import apply_schema


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _instrument_from_row(row: sqlite3.Row) -> InstrumentId:
    return InstrumentId(
        code=row["code"],
        market=Market(row["market"]),
        exchange=Exchange(row["exchange"]),
    )


def _bar_hash(bar: CanonicalBar) -> str:
    """Ignore fetched_at so a later identical source refresh stays idempotent."""
    return stable_hash(
        {
            "instrument": bar.instrument.to_dict(),
            "trading_date": bar.trading_date.isoformat(),
            "adjustment_mode": bar.adjustment_mode.value,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "source": bar.source,
            "corporate_action_version": bar.corporate_action_version,
            "schema_version": bar.schema_version,
        }
    )


def _fundamental_payload(snapshot: FundamentalSnapshot) -> str:
    values: dict[str, dict[str, object]] = {}
    for name, field in snapshot.fields.items():
        values[name] = {
            "value": field.value,
            "unit": field.unit,
            "period_end": field.period_end.isoformat() if field.period_end else None,
            "published_at": utc_iso(field.published_at) if field.published_at else None,
            "source": field.source,
        }
    return canonical_json(values)


@dataclass(frozen=True, slots=True)
class DailyBarWriteResult:
    inserted: int
    idempotent: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class QueuedDailyRefresh:
    queue_id: int
    request: DailyBarsRequest
    priority: int
    next_retry_at: datetime
    attempts: int


@dataclass(frozen=True, slots=True)
class DailyBarDriftRecord:
    instrument: InstrumentId
    trading_date: date
    primary_source: str
    comparator_source: str
    max_abs_price_diff: float
    volume_ratio: float | None
    status: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class QueuedProviderRefresh:
    queue_id: int
    task_type: str
    instrument: InstrumentId
    mode: DecisionMode | None
    next_retry_at: datetime
    attempts: int


class SQLiteRepository:
    """Repository that only writes the V2 database path supplied by the caller."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.database_path, timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        apply_schema(self._connection)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def reserve_provider_slot(
        self,
        provider: str,
        market: Market,
        data_type: str,
        as_of: datetime,
        *,
        limit: int,
        window: timedelta,
    ) -> datetime | None:
        """Reserve a durable rate-limit slot, or return its precise next retry time."""
        now = ensure_utc(as_of, "as_of")
        cutoff = utc_iso(now - window)
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM provider_rate_events WHERE provider=? AND market=? AND data_type=? AND requested_at <= ?",
                (provider, market.value, data_type, cutoff),
            )
            rows = connection.execute(
                """SELECT requested_at FROM provider_rate_events
                   WHERE provider=? AND market=? AND data_type=? ORDER BY requested_at""",
                (provider, market.value, data_type),
            ).fetchall()
            if len(rows) >= limit:
                return _parse_datetime(rows[0]["requested_at"]) + window
            connection.execute(
                "INSERT INTO provider_rate_events(provider, market, data_type, requested_at) VALUES (?, ?, ?, ?)",
                (provider, market.value, data_type, utc_iso(now)),
            )
        return None

    def reserve_provider_slots(
        self,
        provider: str,
        market: Market,
        data_type: str,
        as_of: datetime,
        *,
        count: int,
        limit: int,
        window: timedelta,
    ) -> datetime | None:
        """Atomically reserve several requests in one provider rate-limit window."""
        if count <= 0 or limit <= 0 or count > limit:
            raise ValueError("provider slot count must be between 1 and its limit")
        now = ensure_utc(as_of, "as_of")
        cutoff = utc_iso(now - window)
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM provider_rate_events WHERE provider=? AND market=? AND data_type=? AND requested_at <= ?",
                (provider, market.value, data_type, cutoff),
            )
            rows = connection.execute(
                """SELECT requested_at FROM provider_rate_events
                   WHERE provider=? AND market=? AND data_type=? ORDER BY requested_at""",
                (provider, market.value, data_type),
            ).fetchall()
            if len(rows) + count > limit:
                return _parse_datetime(rows[0]["requested_at"]) + window
            connection.executemany(
                "INSERT INTO provider_rate_events(provider, market, data_type, requested_at) VALUES (?, ?, ?, ?)",
                [(provider, market.value, data_type, utc_iso(now))] * count,
            )
        return None

    def enqueue_daily_refresh(
        self,
        request: DailyBarsRequest,
        next_retry_at: datetime,
        *,
        priority: int = 0,
        attempts: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO refresh_queue(
                       task_type, instrument_key, code, market, exchange, requested_start, requested_end,
                       listing_date, priority, next_retry_at, status, attempts, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                   ON CONFLICT(task_type, instrument_key, requested_start, requested_end)
                   DO UPDATE SET priority=MAX(priority, excluded.priority), next_retry_at=excluded.next_retry_at,
                                 status='pending', attempts=excluded.attempts, updated_at=excluded.updated_at""",
                (
                    "daily_bars", request.instrument.stable_key, request.instrument.code,
                    request.instrument.market.value, request.instrument.exchange.value,
                    request.requested_start.isoformat(), request.requested_end.isoformat(),
                    request.listing_date.isoformat() if request.listing_date else None,
                    priority, utc_iso(next_retry_at), attempts, utc_iso(now), utc_iso(now),
                ),
            )

    def due_daily_refreshes(self, as_of: datetime, *, limit: int) -> tuple[QueuedDailyRefresh, ...]:
        rows = self._fetchall(
            """SELECT * FROM refresh_queue WHERE task_type='daily_bars' AND status='pending' AND next_retry_at <= ?
               ORDER BY priority DESC, created_at, id LIMIT ?""",
            (utc_iso(as_of), limit),
        )
        return tuple(
            QueuedDailyRefresh(
                queue_id=int(row["id"]),
                request=DailyBarsRequest(
                    InstrumentId(row["code"], Market(row["market"]), Exchange(row["exchange"])),
                    date.fromisoformat(row["requested_start"]), date.fromisoformat(row["requested_end"]),
                    date.fromisoformat(row["listing_date"]) if row["listing_date"] else None,
                ),
                priority=int(row["priority"]), next_retry_at=_parse_datetime(row["next_retry_at"]), attempts=int(row["attempts"]),
            )
            for row in rows
        )

    def mark_daily_refresh_complete(self, queue_id: int) -> None:
        with self._transaction() as connection:
            connection.execute("UPDATE refresh_queue SET status='complete', updated_at=? WHERE id=?", (utc_iso(datetime.now(timezone.utc)), queue_id))

    def mark_daily_refresh_failed(self, queue_id: int, attempts: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE refresh_queue SET status='failed', attempts=?, updated_at=? WHERE id=?",
                (attempts, utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def reschedule_daily_refresh(self, queue_id: int, next_retry_at: datetime, attempts: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE refresh_queue SET next_retry_at=?, attempts=?, updated_at=? WHERE id=?",
                (utc_iso(next_retry_at), attempts, utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def enqueue_provider_refresh(
        self,
        task_type: str,
        instrument: InstrumentId,
        next_retry_at: datetime,
        *,
        mode: DecisionMode | None = None,
        attempts: int = 0,
    ) -> None:
        if task_type not in {"metadata", "listing_date", "fundamentals", "news"}:
            raise ValueError("unsupported provider refresh task")
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO provider_refresh_queue(
                       task_type, instrument_key, code, market, exchange, mode, next_retry_at, status, attempts, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                   ON CONFLICT(task_type, instrument_key, mode)
                   DO UPDATE SET next_retry_at=excluded.next_retry_at, status='pending',
                                 attempts=excluded.attempts, updated_at=excluded.updated_at""",
                (
                    task_type, instrument.stable_key, instrument.code, instrument.market.value, instrument.exchange.value,
                    mode.value if mode is not None else "", utc_iso(next_retry_at), attempts, utc_iso(now), utc_iso(now),
                ),
            )

    def due_provider_refreshes(self, as_of: datetime, *, limit: int) -> tuple[QueuedProviderRefresh, ...]:
        rows = self._fetchall(
            """SELECT * FROM provider_refresh_queue WHERE status='pending' AND next_retry_at <= ?
               ORDER BY created_at, id LIMIT ?""",
            (utc_iso(as_of), limit),
        )
        return tuple(
            QueuedProviderRefresh(
                queue_id=int(row["id"]), task_type=row["task_type"],
                instrument=InstrumentId(row["code"], Market(row["market"]), Exchange(row["exchange"])),
                mode=DecisionMode(row["mode"]) if row["mode"] else None,
                next_retry_at=_parse_datetime(row["next_retry_at"]), attempts=int(row["attempts"]),
            )
            for row in rows
        )

    def mark_provider_refresh_complete(self, queue_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE provider_refresh_queue SET status='complete', updated_at=? WHERE id=?",
                (utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def mark_provider_refresh_failed(self, queue_id: int, attempts: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE provider_refresh_queue SET status='failed', attempts=?, updated_at=? WHERE id=?",
                (attempts, utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def reschedule_provider_refresh(self, queue_id: int, next_retry_at: datetime, attempts: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE provider_refresh_queue SET next_retry_at=?, attempts=?, updated_at=? WHERE id=?",
                (utc_iso(next_retry_at), attempts, utc_iso(datetime.now(timezone.utc)), queue_id),
            )

    def record_daily_bar_drift(
        self,
        primary: CanonicalBar,
        comparator: CanonicalBar,
        *,
        max_abs_price_diff: float,
        volume_ratio: float | None,
        status: str,
        observed_at: datetime,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO daily_bar_drift_records(
                       instrument_key, trading_date, primary_source, comparator_source, max_abs_price_diff,
                       volume_ratio, status, primary_payload_json, comparator_payload_json, observed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(instrument_key, trading_date, primary_source, comparator_source, observed_at)
                   DO UPDATE SET max_abs_price_diff=excluded.max_abs_price_diff, volume_ratio=excluded.volume_ratio,
                                 status=excluded.status, primary_payload_json=excluded.primary_payload_json,
                                 comparator_payload_json=excluded.comparator_payload_json""",
                (
                    primary.instrument.stable_key, primary.trading_date.isoformat(), primary.source, comparator.source,
                    max_abs_price_diff, volume_ratio, status, canonical_json(primary.to_dict()),
                    canonical_json(comparator.to_dict()), utc_iso(observed_at),
                ),
            )

    def list_daily_bar_drift(self, instrument: InstrumentId) -> tuple[DailyBarDriftRecord, ...]:
        rows = self._fetchall(
            "SELECT * FROM daily_bar_drift_records WHERE instrument_key=? ORDER BY observed_at DESC, trading_date DESC",
            (instrument.stable_key,),
        )
        return tuple(
            DailyBarDriftRecord(
                instrument=instrument, trading_date=date.fromisoformat(row["trading_date"]),
                primary_source=row["primary_source"], comparator_source=row["comparator_source"],
                max_abs_price_diff=float(row["max_abs_price_diff"]), volume_ratio=row["volume_ratio"],
                status=row["status"], observed_at=_parse_datetime(row["observed_at"]),
            )
            for row in rows
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _fetchall(self, sql: str, parameters: tuple[object, ...]) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchall()

    def _fetchone(self, sql: str, parameters: tuple[object, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchone()

    def upsert_daily_bars(self, bars: Iterable[CanonicalBar]) -> DailyBarWriteResult:
        materialized = tuple(bars)
        if any(not isinstance(bar, CanonicalBar) for bar in materialized):
            raise ContractViolation("daily bar batch must contain CanonicalBar objects")
        # Reconstruct every object before opening the transaction.  This protects
        # the repository from deserialized or intentionally bypassed dataclass
        # construction and guarantees all-or-nothing batch validation.
        try:
            validated = tuple(
                CanonicalBar(
                    instrument=bar.instrument,
                    trading_date=bar.trading_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    adjustment_mode=bar.adjustment_mode,
                    source=bar.source,
                    fetched_at=bar.fetched_at,
                    corporate_action_version=bar.corporate_action_version,
                    schema_version=bar.schema_version,
                )
                for bar in materialized
            )
        except ContractViolation as exc:
            raise ContractViolation(f"daily bar batch validation failed: {exc}") from exc
        hashes = [(bar, _bar_hash(bar)) for bar in validated]
        inserted = idempotent = conflicts = 0
        with self._transaction() as connection:
            for bar, payload_hash in hashes:
                row = connection.execute(
                    """SELECT payload_hash FROM daily_bars
                       WHERE instrument_key=? AND trading_date=? AND adjustment_mode=?""",
                    (bar.instrument.stable_key, bar.trading_date.isoformat(), bar.adjustment_mode.value),
                ).fetchone()
                if row is not None:
                    if row["payload_hash"] == payload_hash:
                        idempotent += 1
                        continue
                    conflicts += 1
                    connection.execute(
                        """INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            "daily_bar_conflict",
                            bar.instrument.stable_key,
                            bar.trading_date.isoformat(),
                            "CONFLICTING_DUPLICATE_BAR",
                            canonical_json(bar.to_dict()),
                            utc_iso(datetime.now(timezone.utc)),
                        ),
                    )
                    continue
                connection.execute(
                    """INSERT INTO daily_bars(
                           instrument_key, code, market, exchange, trading_date, adjustment_mode,
                           open, high, low, close, volume, source, fetched_at,
                           corporate_action_version, payload_hash, schema_version
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bar.instrument.stable_key,
                        bar.instrument.code,
                        bar.instrument.market.value,
                        bar.instrument.exchange.value,
                        bar.trading_date.isoformat(),
                        bar.adjustment_mode.value,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.source,
                        utc_iso(bar.fetched_at),
                        bar.corporate_action_version,
                        payload_hash,
                        bar.schema_version,
                    ),
                )
                inserted += 1
        return DailyBarWriteResult(inserted=inserted, idempotent=idempotent, conflicts=conflicts)

    def list_daily_bars(
        self,
        instrument: InstrumentId,
        start: date,
        end: date,
        adjustment_mode: AdjustmentMode = AdjustmentMode.FRONT_ADJUSTED,
    ) -> tuple[CanonicalBar, ...]:
        rows = self._fetchall(
            """SELECT * FROM daily_bars
               WHERE instrument_key=? AND adjustment_mode=? AND trading_date BETWEEN ? AND ?
               ORDER BY trading_date""",
            (instrument.stable_key, adjustment_mode.value, start.isoformat(), end.isoformat()),
        )
        return tuple(
            CanonicalBar(
                instrument=_instrument_from_row(row),
                trading_date=date.fromisoformat(row["trading_date"]),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                adjustment_mode=AdjustmentMode(row["adjustment_mode"]),
                source=row["source"],
                fetched_at=_parse_datetime(row["fetched_at"]),
                corporate_action_version=row["corporate_action_version"],
                schema_version=row["schema_version"],
            )
            for row in rows
        )

    def quarantine_daily_bars(self, instrument: InstrumentId, before_date: date, reason: str) -> int:
        rows = self._fetchall(
            """SELECT * FROM daily_bars
               WHERE instrument_key=? AND trading_date < ?""",
            (instrument.stable_key, before_date.isoformat()),
        )
        if not rows:
            return 0
        with self._transaction() as connection:
            for row in rows:
                connection.execute(
                    """INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "daily_bar",
                        instrument.stable_key,
                        row["trading_date"],
                        reason,
                        canonical_json(dict(row)),
                        utc_iso(datetime.now(timezone.utc)),
                    ),
                )
            connection.execute(
                "DELETE FROM daily_bars WHERE instrument_key=? AND trading_date < ?",
                (instrument.stable_key, before_date.isoformat()),
            )
        return len(rows)

    def quarantine_received_daily_bars(
        self,
        bars: Iterable[CanonicalBar],
        reason: str,
    ) -> int:
        """Audit rejected provider bars without ever admitting them to daily_bars."""
        materialized = tuple(bars)
        if any(not isinstance(bar, CanonicalBar) for bar in materialized):
            raise ContractViolation("quarantined daily bars must contain CanonicalBar objects")
        if not materialized:
            return 0
        with self._transaction() as connection:
            for bar in materialized:
                connection.execute(
                    """INSERT INTO quarantine_records(record_type, instrument_key, trading_date, reason, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "daily_bar_rejected",
                        bar.instrument.stable_key,
                        bar.trading_date.isoformat(),
                        reason,
                        canonical_json(bar.to_dict()),
                        utc_iso(datetime.now(timezone.utc)),
                    ),
                )
        return len(materialized)

    def upsert_intraday_bars(self, bars: Iterable[IntradayBar]) -> int:
        materialized = tuple(bars)
        if any(not isinstance(bar, IntradayBar) for bar in materialized):
            raise ContractViolation("intraday bar batch must contain IntradayBar objects")
        with self._transaction() as connection:
            for bar in materialized:
                connection.execute(
                    """INSERT OR REPLACE INTO intraday_bars(
                           instrument_key, code, market, exchange, observed_at, session_date,
                           open, high, low, close, volume, source, evidence_quality, fetched_at, schema_version
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bar.instrument.stable_key,
                        bar.instrument.code,
                        bar.instrument.market.value,
                        bar.instrument.exchange.value,
                        utc_iso(bar.observed_at),
                        bar.session_date.isoformat(),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.source,
                        bar.evidence_quality,
                        utc_iso(bar.fetched_at),
                        bar.schema_version,
                    ),
                )
        return len(materialized)

    def list_intraday_bars(self, instrument: InstrumentId, start_at: datetime, end_at: datetime) -> tuple[IntradayBar, ...]:
        rows = self._fetchall(
            """SELECT * FROM intraday_bars
               WHERE instrument_key=? AND observed_at BETWEEN ? AND ? ORDER BY observed_at""",
            (instrument.stable_key, utc_iso(start_at), utc_iso(end_at)),
        )
        return tuple(
            IntradayBar(
                instrument=_instrument_from_row(row),
                observed_at=_parse_datetime(row["observed_at"]),
                session_date=date.fromisoformat(row["session_date"]),
                open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                volume=row["volume"], source=row["source"], evidence_quality=row["evidence_quality"],
                fetched_at=_parse_datetime(row["fetched_at"]), schema_version=row["schema_version"],
            )
            for row in rows
        )

    def save_quote_snapshot(self, quote: QuoteSnapshot) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO quote_snapshots(
                       instrument_key, code, market, exchange, session, price, prev_close, open, high, low,
                       volume, bid, ask, observed_at, fetched_at, source, freshness_status, schema_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    quote.instrument.stable_key, quote.instrument.code, quote.instrument.market.value,
                    quote.instrument.exchange.value, quote.session.value, quote.price, quote.prev_close,
                    quote.open, quote.high, quote.low, quote.volume, quote.bid, quote.ask,
                    utc_iso(quote.observed_at), utc_iso(quote.fetched_at), quote.source,
                    quote.freshness_status.value, quote.schema_version,
                ),
            )

    def get_latest_quote(self, instrument: InstrumentId, session: TradingSession) -> QuoteSnapshot | None:
        row = self._fetchone(
            """SELECT * FROM quote_snapshots WHERE instrument_key=? AND session=?
               ORDER BY observed_at DESC LIMIT 1""",
            (instrument.stable_key, session.value),
        )
        if row is None:
            return None
        return QuoteSnapshot(
            instrument=_instrument_from_row(row), session=TradingSession(row["session"]), price=row["price"],
            prev_close=row["prev_close"], open=row["open"], high=row["high"], low=row["low"],
            volume=row["volume"], bid=row["bid"], ask=row["ask"],
            observed_at=_parse_datetime(row["observed_at"]), fetched_at=_parse_datetime(row["fetched_at"]),
            source=row["source"], freshness_status=FreshnessStatus(row["freshness_status"]),
            schema_version=row["schema_version"],
        )

    def upsert_stock_metadata(self, metadata: StockMetadata) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO stock_metadata(
                       instrument_key, code, market, exchange, name, industry, description, listing_date,
                       source, fetched_at, schema_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    metadata.instrument.stable_key, metadata.instrument.code, metadata.instrument.market.value,
                    metadata.instrument.exchange.value, metadata.name, metadata.industry, metadata.description,
                    metadata.listing_date.isoformat() if metadata.listing_date else None, metadata.source,
                    utc_iso(metadata.fetched_at), metadata.schema_version,
                ),
            )

    def get_stock_metadata(self, instrument: InstrumentId) -> StockMetadata | None:
        row = self._fetchone(
            "SELECT * FROM stock_metadata WHERE instrument_key=?", (instrument.stable_key,)
        )
        if row is None:
            return None
        return StockMetadata(
            instrument=_instrument_from_row(row), name=row["name"], industry=row["industry"],
            description=row["description"], listing_date=date.fromisoformat(row["listing_date"]) if row["listing_date"] else None,
            source=row["source"], fetched_at=_parse_datetime(row["fetched_at"]), schema_version=row["schema_version"],
        )

    def upsert_news(self, items: Iterable[NewsSnapshot]) -> int:
        materialized = tuple(items)
        if any(not isinstance(item, NewsSnapshot) for item in materialized):
            raise ContractViolation("news batch must contain NewsSnapshot objects")
        with self._transaction() as connection:
            for item in materialized:
                connection.execute(
                    """INSERT INTO news_snapshots(
                           stable_key, instrument_key, code, market, exchange, title, source, published_at,
                           available_at, fetched_at, content, is_macro, finbert_label, finbert_score,
                           relevance, schema_version
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(stable_key) DO UPDATE SET
                           available_at=MIN(news_snapshots.available_at, excluded.available_at),
                           fetched_at=MAX(news_snapshots.fetched_at, excluded.fetched_at),
                           content=COALESCE(excluded.content, news_snapshots.content),
                           is_macro=MAX(news_snapshots.is_macro, excluded.is_macro),
                           finbert_label=COALESCE(excluded.finbert_label, news_snapshots.finbert_label),
                           finbert_score=COALESCE(excluded.finbert_score, news_snapshots.finbert_score),
                           relevance=COALESCE(excluded.relevance, news_snapshots.relevance),
                           schema_version=MAX(news_snapshots.schema_version, excluded.schema_version)""",
                    (
                        item.stable_key, item.instrument.stable_key, item.instrument.code, item.instrument.market.value,
                        item.instrument.exchange.value, item.title, item.source, utc_iso(item.published_at),
                        utc_iso(item.available_at), utc_iso(item.fetched_at), item.content, int(item.is_macro),
                        item.finbert_label, item.finbert_score, item.relevance, item.schema_version,
                    ),
                )
        return len(materialized)

    def list_news_as_of(self, instrument: InstrumentId, as_of: datetime) -> tuple[NewsSnapshot, ...]:
        rows = self._fetchall(
            """SELECT * FROM news_snapshots WHERE instrument_key=? AND available_at <= ?
               ORDER BY available_at, published_at""",
            (instrument.stable_key, utc_iso(as_of)),
        )
        return tuple(
            NewsSnapshot(
                instrument=_instrument_from_row(row), title=row["title"], source=row["source"],
                published_at=_parse_datetime(row["published_at"]), available_at=_parse_datetime(row["available_at"]),
                fetched_at=_parse_datetime(row["fetched_at"]), content=row["content"], is_macro=bool(row["is_macro"]),
                finbert_label=row["finbert_label"], finbert_score=row["finbert_score"], relevance=row["relevance"],
                schema_version=row["schema_version"],
            )
            for row in rows
        )

    def upsert_fundamental_snapshot(self, snapshot: FundamentalSnapshot) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO fundamental_snapshots(
                       instrument_key, code, market, exchange, fields_json, available_at, fetched_at,
                       provider, quality_status, schema_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.instrument.stable_key, snapshot.instrument.code, snapshot.instrument.market.value,
                    snapshot.instrument.exchange.value, _fundamental_payload(snapshot), utc_iso(snapshot.available_at),
                    utc_iso(snapshot.fetched_at), snapshot.provider, snapshot.quality_status.value,
                    snapshot.schema_version,
                ),
            )

    def get_fundamentals_as_of(self, instrument: InstrumentId, as_of: datetime) -> FundamentalSnapshot | None:
        row = self._fetchone(
            """SELECT * FROM fundamental_snapshots WHERE instrument_key=? AND available_at <= ?
               ORDER BY available_at DESC LIMIT 1""",
            (instrument.stable_key, utc_iso(as_of)),
        )
        if row is None:
            return None
        raw_fields = json.loads(row["fields_json"])
        fields = {
            name: FundamentalValue(
                value=value["value"], unit=value["unit"],
                period_end=date.fromisoformat(value["period_end"]) if value["period_end"] else None,
                published_at=_parse_datetime(value["published_at"]) if value["published_at"] else None,
                source=value["source"],
            )
            for name, value in raw_fields.items()
        }
        return FundamentalSnapshot(
            instrument=_instrument_from_row(row), fields=fields, available_at=_parse_datetime(row["available_at"]),
            fetched_at=_parse_datetime(row["fetched_at"]), provider=row["provider"],
            quality_status=QualityStatus(row["quality_status"]), schema_version=row["schema_version"],
        )

    def save_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO account_snapshots(market, currency, cash, captured_at, schema_version)
                   VALUES (?, ?, ?, ?, ?)""",
                (snapshot.market.value, snapshot.currency, str(snapshot.cash), utc_iso(snapshot.captured_at), snapshot.schema_version),
            )
            row = connection.execute(
                "SELECT id FROM account_snapshots WHERE market=? AND captured_at=?",
                (snapshot.market.value, utc_iso(snapshot.captured_at)),
            ).fetchone()
            snapshot_id = int(row["id"])
            connection.execute("DELETE FROM account_positions WHERE account_snapshot_id=?", (snapshot_id,))
            for position in snapshot.positions:
                connection.execute(
                    """INSERT INTO account_positions(
                           account_snapshot_id, instrument_key, code, market, exchange, shares, cost_price, captured_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id, position.instrument.stable_key, position.instrument.code,
                        position.instrument.market.value, position.instrument.exchange.value,
                        str(position.shares), str(position.cost_price), utc_iso(position.captured_at),
                    ),
                )

    def get_latest_account_snapshot(self, market: Market) -> AccountSnapshot | None:
        row = self._fetchone(
            "SELECT * FROM account_snapshots WHERE market=? ORDER BY captured_at DESC LIMIT 1", (market.value,)
        )
        if row is None:
            return None
        positions_rows = self._fetchall(
            "SELECT * FROM account_positions WHERE account_snapshot_id=? ORDER BY instrument_key", (row["id"],)
        )
        positions = tuple(
            PositionSnapshot(
                instrument=_instrument_from_row(position), shares=Decimal(position["shares"]),
                cost_price=Decimal(position["cost_price"]), captured_at=_parse_datetime(position["captured_at"]),
            )
            for position in positions_rows
        )
        return AccountSnapshot(
            market=Market(row["market"]), currency=row["currency"], cash=Decimal(row["cash"]),
            positions=positions, captured_at=_parse_datetime(row["captured_at"]), schema_version=row["schema_version"],
        )

    def migration_preflight(self, source_path: Path | str, as_of: datetime) -> MigrationPreflight:
        source = Path(source_path)
        if not source.exists():
            return MigrationPreflight(
                source_path=str(source), source_exists=False, source_schema_detected=False,
                table_counts={}, migratable_counts={}, conflict_counts={},
                warnings=("V1 database does not exist",), read_only=True, evaluated_at=as_of,
            )
        connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        try:
            table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            tables = {row[0] for row in table_rows}
            known = ("holdings", "watchlist", "account_balance", "price_history", "stocks")
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in known if table in tables
            }
            conflicts: dict[str, int] = {}
            if {"price_history", "stocks"}.issubset(tables):
                try:
                    conflicts["before_listing_date"] = int(
                        connection.execute(
                            """SELECT COUNT(*) FROM price_history p JOIN stocks s ON s.code=p.code
                               WHERE s.listing_date IS NOT NULL AND s.listing_date != '' AND p.date < s.listing_date"""
                        ).fetchone()[0]
                    )
                except sqlite3.DatabaseError:
                    conflicts["before_listing_date"] = 0
            return MigrationPreflight(
                source_path=str(source), source_exists=True, source_schema_detected=bool(tables.intersection(known)),
                table_counts=counts,
                migratable_counts={
                    "holdings": counts.get("holdings", 0),
                    "watchlist": counts.get("watchlist", 0),
                    "account_balance": counts.get("account_balance", 0),
                },
                conflict_counts=conflicts,
                warnings=(), read_only=True, evaluated_at=as_of,
            )
        finally:
            connection.close()
