"""Correction v1.3 #1: small, dependency-free versioned migration system.

`Base.metadata.create_all()` only creates MISSING tables -- it never alters
an existing one. A database created by an earlier version of this app (Fase
1 baseline, or the v1.1 correction) has a schema that no longer matches
`app/persistence/models.py`, and starting the current app against it fails
hard (e.g. "no such column: system_state.clock_out_of_sync"). This module
fixes that: every schema change since the first public Fase 1 commit is
expressed as a numbered, idempotent, transactional migration, tracked in a
`schema_migrations` table.

Design goals (all required by the correction):
- deterministic: migrations run in a fixed numeric order, never reordered;
- transactional: the whole run happens inside one DB transaction -- any
  failure rolls back everything, never leaving a half-applied schema;
- idempotent: every individual ALTER is guarded by an existence check, so
  re-running an already-applied migration (or one whose target already
  exists in the DB for other reasons) is a safe no-op;
- testable: `run_migrations(engine)` is a plain function tests can call
  against any engine, including one pre-loaded with legacy-schema fixture
  data (see tests/test_migrations.py);
- version-aware: `current_schema_version(engine)` always reflects reality;
- never fakes success: an exception during any migration propagates after
  the transaction is rolled back -- nothing is ever recorded as applied
  unless it actually committed.

Schema history:
  v0 -> v1  (Fase 1 baseline -> correction v1.1): add
            system_state.state_ambiguous, orders.is_close, and relax
            orders.stop_loss from NOT NULL to nullable (close orders carry
            no stop-loss).
  v1 -> v2  (v1.1 -> correction v1.2): add system_state.clock_out_of_sync,
            and a unique index on candles(symbol, timeframe, open_time) --
            historical duplicate candle rows (if any) are deduplicated
            first, keeping the earliest-inserted (lowest id) row as the
            canonical record, deterministically and before the index is
            created.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.persistence.models import Base

CURRENT_SCHEMA_VERSION = 2


class MigrationError(Exception):
    """Raised when a migration cannot be applied. The triggering exception
    is always chained (`raise ... from exc`) so the root cause is visible;
    the message itself is in Portuguese since this can surface to an
    operator starting the app against an old database."""


@dataclass(frozen=True)
class MigrationReport:
    starting_version: int
    ending_version: int
    applied: list[int]
    stamped_only: bool  # True when a brand-new DB was created at head and merely stamped, not ALTERed


def _table_exists(conn: Connection, table: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
    ).fetchone()
    return row is not None


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def _index_exists(conn: Connection, index_name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"), {"n": index_name}
    ).fetchone()
    return row is not None


def _ensure_schema_migrations_table(conn: Connection) -> None:
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, "
        "description TEXT NOT NULL, "
        "applied_at TEXT NOT NULL"
        ")"
    ))


def _applied_versions(conn: Connection) -> set[int]:
    rows = conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
    return {r[0] for r in rows}


def _record_migration(conn: Connection, version: int, description: str) -> None:
    conn.execute(
        text("INSERT INTO schema_migrations (version, description, applied_at) VALUES (:v, :d, :t)"),
        {"v": version, "d": description, "t": datetime.now(timezone.utc).isoformat()},
    )


def _migrate_to_v1(conn: Connection) -> None:
    """Fase 1 baseline -> correction v1.1 schema."""
    if not _column_exists(conn, "system_state", "state_ambiguous"):
        conn.execute(text(
            "ALTER TABLE system_state ADD COLUMN state_ambiguous BOOLEAN NOT NULL DEFAULT 0"
        ))

    needs_orders_rebuild = not _column_exists(conn, "orders", "is_close")
    if needs_orders_rebuild:
        # SQLite cannot drop a NOT NULL constraint (orders.stop_loss) with a
        # plain ALTER TABLE, and orders.is_close is a new column -- both are
        # handled by rebuilding the table: create the new shape, copy every
        # existing row across (is_close defaults to 0 -- every pre-existing
        # order was an opening order, since close-via-risk-engine did not
        # exist yet), drop the old table, rename the new one into place.
        conn.execute(text(
            "CREATE TABLE orders_v1 ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "idempotency_key VARCHAR(128) NOT NULL, "
            "risk_evaluation_id INTEGER NOT NULL, "
            "symbol VARCHAR(32) NOT NULL, "
            "side VARCHAR(8) NOT NULL, "
            "qty FLOAT NOT NULL, "
            "stop_loss FLOAT, "
            "take_profit FLOAT, "
            "is_close BOOLEAN NOT NULL DEFAULT 0, "
            "status VARCHAR(16) NOT NULL, "
            "exchange_order_id VARCHAR(128), "
            "mode VARCHAR(16) NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL"
            ")"
        ))
        conn.execute(text(
            "INSERT INTO orders_v1 (id, idempotency_key, risk_evaluation_id, symbol, side, qty, "
            "stop_loss, take_profit, is_close, status, exchange_order_id, mode, created_at, updated_at) "
            "SELECT id, idempotency_key, risk_evaluation_id, symbol, side, qty, "
            "stop_loss, take_profit, 0, status, exchange_order_id, mode, created_at, updated_at "
            "FROM orders"
        ))
        conn.execute(text("DROP TABLE orders"))
        conn.execute(text("ALTER TABLE orders_v1 RENAME TO orders"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_orders_idempotency_key ON orders (idempotency_key)"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_symbol ON orders (symbol)"))


def _migrate_to_v2(conn: Connection) -> None:
    """Correction v1.1 -> correction v1.2 schema."""
    if not _column_exists(conn, "system_state", "clock_out_of_sync"):
        conn.execute(text(
            "ALTER TABLE system_state ADD COLUMN clock_out_of_sync BOOLEAN NOT NULL DEFAULT 0"
        ))

    if not _index_exists(conn, "uq_candle_symbol_timeframe_open_time"):
        # Deterministic, documented dedup strategy (required by the
        # correction): for any (symbol, timeframe, open_time) group with
        # more than one row, keep only the row with the lowest id (the
        # earliest one this app ever inserted) and delete the rest, before
        # the unique index is created.
        conn.execute(text(
            "DELETE FROM candles WHERE id NOT IN ("
            "SELECT MIN(id) FROM candles GROUP BY symbol, timeframe, open_time"
            ")"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_candle_symbol_timeframe_open_time "
            "ON candles (symbol, timeframe, open_time)"
        ))


# Order matters: applied strictly in ascending version order.
MIGRATIONS: list[tuple[int, str, Callable[[Connection], None]]] = [
    (1, "Adiciona system_state.state_ambiguous, orders.is_close; relaxa orders.stop_loss para opcional.", _migrate_to_v1),
    (2, "Adiciona system_state.clock_out_of_sync; cria índice único em candles (dedup determinístico antes).", _migrate_to_v2),
]


def _detect_legacy_version(conn: Connection) -> int:
    """For a database with tables but no schema_migrations history yet,
    inspects actual columns to determine which baseline it already matches."""
    if not _column_exists(conn, "system_state", "state_ambiguous"):
        return 0
    if not _column_exists(conn, "system_state", "clock_out_of_sync"):
        return 1
    return 2


def current_schema_version(engine: Engine) -> int:
    with engine.connect() as conn:
        if not _table_exists(conn, "schema_migrations"):
            if not _table_exists(conn, "system_state"):
                return 0
            return _detect_legacy_version(conn)
        applied = _applied_versions(conn)
        return max(applied) if applied else _detect_legacy_version(conn)


def run_migrations(engine: Engine) -> MigrationReport:
    """Brings the database at `engine` up to CURRENT_SCHEMA_VERSION. Safe to
    call on every app startup, on any of: a brand-new empty database, a
    Fase 1 baseline database, a v1.1 database, or an already-fully-migrated
    database -- idempotent in every case.
    """
    try:
        with engine.begin() as conn:
            _ensure_schema_migrations_table(conn)
            already_applied = _applied_versions(conn)
            starting_version = max(already_applied) if already_applied else None

            brand_new = not _table_exists(conn, "system_state")
            if brand_new:
                # Nothing to migrate FROM -- create every table at the
                # current model shape directly, then stamp every migration
                # version as satisfied (their target shape already exists;
                # running the ALTER statements would be redundant, and for
                # the orders-table-rebuild step, actively wrong to repeat).
                Base.metadata.create_all(conn)
                for version, description, _upgrade in MIGRATIONS:
                    if version not in already_applied:
                        _record_migration(conn, version, description)
                return MigrationReport(
                    starting_version=0, ending_version=CURRENT_SCHEMA_VERSION,
                    applied=[], stamped_only=True,
                )

            if starting_version is None:
                starting_version = _detect_legacy_version(conn)
                # Stamp any version the legacy schema already satisfies, so
                # we never try to re-run (e.g.) the orders table rebuild
                # against a database that already has the target shape for
                # an unrelated reason.
                for version, description, _upgrade in MIGRATIONS:
                    if version <= starting_version:
                        _record_migration(conn, version, description)

            applied_now: list[int] = []
            for version, description, upgrade in MIGRATIONS:
                if version <= starting_version or version in _applied_versions(conn):
                    continue
                try:
                    upgrade(conn)
                except Exception as exc:  # noqa: BLE001 - always wrapped and re-raised
                    raise MigrationError(
                        f"Falha ao aplicar a migração v{version} ({description}). "
                        f"Nenhuma alteração foi confirmada; o banco permanece na versão "
                        f"{starting_version}. Causa original: {exc}"
                    ) from exc
                _record_migration(conn, version, description)
                applied_now.append(version)

            ending_version = max(starting_version, max(applied_now, default=starting_version))
            return MigrationReport(
                starting_version=starting_version, ending_version=ending_version,
                applied=applied_now, stamped_only=False,
            )
    except MigrationError:
        # engine.begin()'s context manager already rolled back the
        # transaction on the exception above -- re-raise as-is so the
        # caller (app startup) stops safely instead of proceeding against a
        # half-migrated (or entirely unmigrated) schema.
        raise


if __name__ == "__main__":
    # Verification command (documented in docs/MIGRACOES.md):
    #   python -m app.persistence.migrations sqlite:///./agente_trader.db
    import sys

    from app.persistence.db import make_engine

    if len(sys.argv) != 2:
        print("uso: python -m app.persistence.migrations <DATABASE_URL>")
        raise SystemExit(2)

    _engine = make_engine(sys.argv[1])
    print(f"Versão atual do esquema: {current_schema_version(_engine)}")
    _report = run_migrations(_engine)
    print(
        f"Migração concluída: v{_report.starting_version} -> v{_report.ending_version} "
        f"(aplicadas: {_report.applied}, apenas registrada={_report.stamped_only})"
    )
