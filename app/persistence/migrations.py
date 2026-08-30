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


class SchemaDivergenceError(MigrationError):
    """Correction v1.4 #3: raised when `schema_migrations` records a
    version as applied, but the real schema does not actually satisfy that
    version's invariants (e.g. someone dropped an index by hand, or an
    older bug recorded success without actually altering everything). The
    system refuses to guess or silently "repair" this -- it stops safely
    and asks for manual intervention, since automatically altering a
    database in an already-unknown state risks making a real corruption
    worse or masking it."""


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


def _column_is_nullable(conn: Connection, table: str, column: str) -> bool:
    """False if the column doesn't exist OR is NOT NULL; True only if it
    exists and genuinely accepts NULL."""
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    for row in rows:
        # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
        if row[1] == column:
            return row[3] == 0
    return False


def _has_unique_index_on(conn: Connection, table: str, columns: set[str]) -> bool:
    """True if SOME unique index exists on `table` covering exactly
    `columns` -- matched by structure (the set of indexed columns), never by
    a hardcoded index name, so a differently-named index providing the same
    guarantee still counts (correction v1.4 #3)."""
    index_list = conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
    for index_row in index_list:
        # PRAGMA index_list columns: (seq, name, unique, origin, partial)
        index_name, is_unique = index_row[1], index_row[2]
        if not is_unique:
            continue
        index_info = conn.execute(text(f"PRAGMA index_info({index_name})")).fetchall()
        indexed_columns = {r[2] for r in index_info}  # (seqno, cid, name)
        if indexed_columns == columns:
            return True
    return False


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

    # Correction v1.4 #3: a database can have `is_close` already added by
    # some other means (or a prior partial/aborted attempt) while
    # `stop_loss` is STILL NOT NULL -- checking only the column's presence
    # missed that case entirely and left a database silently stuck without
    # a nullable stop_loss forever. Rebuild whenever EITHER invariant of
    # the target shape is not yet met.
    needs_orders_rebuild = (
        not _column_exists(conn, "orders", "is_close")
        or not _column_is_nullable(conn, "orders", "stop_loss")
    )
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
        # Preserve real is_close values if the column already existed on the
        # source table (e.g. a partially-migrated database); default new
        # rows to 0 (opening order) only when the column didn't exist yet.
        is_close_source_expr = "is_close" if _column_exists(conn, "orders", "is_close") else "0"
        conn.execute(text(
            "INSERT INTO orders_v1 (id, idempotency_key, risk_evaluation_id, symbol, side, qty, "
            "stop_loss, take_profit, is_close, status, exchange_order_id, mode, created_at, updated_at) "
            f"SELECT id, idempotency_key, risk_evaluation_id, symbol, side, qty, "
            f"stop_loss, take_profit, {is_close_source_expr}, status, exchange_order_id, mode, "
            f"created_at, updated_at "
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

    if not _has_unique_index_on(conn, "candles", {"symbol", "timeframe", "open_time"}):
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


def _v1_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v1 -- not just one sentinel column
    (correction v1.4 #3). A database only counts as "at least v1" if every
    one of these holds."""
    return (
        _column_exists(conn, "system_state", "state_ambiguous")
        and _column_exists(conn, "orders", "is_close")
        and _column_is_nullable(conn, "orders", "stop_loss")
    )


def _v2_invariants_satisfied(conn: Connection) -> bool:
    """ALL structural invariants of v2. The unique index check is
    structural (by columns), so an index with a different name providing
    the same guarantee still satisfies it."""
    return (
        _column_exists(conn, "system_state", "clock_out_of_sync")
        and _has_unique_index_on(conn, "candles", {"symbol", "timeframe", "open_time"})
    )


_VERSION_INVARIANTS: dict[int, Callable[[Connection], bool]] = {
    1: _v1_invariants_satisfied,
    2: _v2_invariants_satisfied,
}


def _invariants_satisfied_for_version(conn: Connection, version: int) -> bool:
    check = _VERSION_INVARIANTS.get(version)
    return check(conn) if check is not None else True


def _detect_legacy_version(conn: Connection) -> int:
    """For a database with tables but no schema_migrations history yet,
    validates ALL structural invariants of each version (never a single
    sentinel column), version by version in ascending order, to determine
    which baseline it already matches. Stops at the first version whose
    invariants don't hold -- since each version's own check already implies
    every earlier one held (this loop only ever advances after the
    previous version's check passed), the result is inherently the highest
    version whose CUMULATIVE invariants (1..that version) are satisfied."""
    version = 0
    for candidate in range(1, CURRENT_SCHEMA_VERSION + 1):
        if not _invariants_satisfied_for_version(conn, candidate):
            break
        version = candidate
    return version


def _cumulative_invariants_satisfied(conn: Connection, version: int) -> bool:
    """Correction v1.5 #2: a database only genuinely satisfies version N if
    EVERY invariant from v1 through vN holds -- not merely the invariants
    introduced at N itself. A database stamped only `version=2` with v2's
    own columns/index present but v1's `orders.is_close` missing must NOT
    be treated as valid v2."""
    return all(_invariants_satisfied_for_version(conn, v) for v in range(1, version + 1))


def _validate_recorded_history(conn: Connection, recorded_versions: set[int]) -> None:
    """Correction v1.5 #2: schema_migrations recording versions as applied
    is not, by itself, trusted. Unlike the earlier per-version check this
    replaces, this validates the FULL ancestral chain, not just whichever
    versions happen to have a row:

    - a recorded version newer than CURRENT_SCHEMA_VERSION halts safely
      (never treat an unknown/future version, or an implicit downgrade
      away from it, as valid);
    - the recorded set must be EXACTLY the contiguous range {1..N} for the
      max recorded version N -- a gap (e.g. only v2, or {1, 3}) is rejected
      as an incomplete history, never silently accepted just because the
      highest version has a row;
    - the CUMULATIVE structural invariants of 1..N must hold against the
      real schema, not just N's own.

    Never repairs anything automatically -- same policy as before
    (correction v1.4 #3): refuse and require manual intervention, since
    auto-"fixing" an already-divergent database risks masking real
    corruption."""
    if not recorded_versions:
        return
    max_version = max(recorded_versions)

    if max_version > CURRENT_SCHEMA_VERSION:
        raise SchemaDivergenceError(
            f"schema_migrations registra a versão v{max_version}, superior à versão máxima "
            f"conhecida por esta aplicação (v{CURRENT_SCHEMA_VERSION}). Isso indica que o banco foi "
            f"criado ou migrado por uma versão mais nova do sistema, ou por engano. Por segurança, "
            f"nenhuma alteração automática (incluindo qualquer downgrade implícito) foi feita -- "
            f"intervenção manual é necessária antes de reiniciar a aplicação. Ver docs/MIGRACOES.md, "
            f"seção 'Divergência de esquema'."
        )

    expected = set(range(1, max_version + 1))
    if recorded_versions != expected:
        missing = sorted(expected - recorded_versions)
        raise SchemaDivergenceError(
            f"Histórico de migrações não contíguo: schema_migrations registra as versões "
            f"{sorted(recorded_versions)}, mas a versão máxima registrada (v{max_version}) exigiria "
            f"exatamente {sorted(expected)} (faltando: {missing}). Um histórico incompleto nunca é "
            f"aceito só porque a versão mais alta tem uma linha registrada. Por segurança, nenhuma "
            f"alteração automática foi feita -- intervenção manual é necessária antes de reiniciar a "
            f"aplicação. Ver docs/MIGRACOES.md, seção 'Divergência de esquema'."
        )

    if not _cumulative_invariants_satisfied(conn, max_version):
        raise SchemaDivergenceError(
            f"Inconsistência de esquema detectada: schema_migrations registra até a versão "
            f"v{max_version} como aplicada, mas o esquema real do banco não satisfaz todos os "
            f"invariantes estruturais cumulativos de v1 até v{max_version} (colunas, nulabilidade ou "
            f"índices únicos ausentes/divergentes em alguma versão da cadeia -- não apenas na mais "
            f"recente). Isso pode indicar uma migração anterior malsucedida, uma alteração manual do "
            f"banco, ou corrupção. Por segurança, nenhuma alteração automática foi feita -- "
            f"intervenção manual é necessária antes de reiniciar a aplicação. Ver docs/MIGRACOES.md, "
            f"seção 'Divergência de esquema'."
        )


def current_schema_version(engine: Engine) -> int:
    """Correction v1.5 #2: never reports a recorded version as valid when
    the chain or the schema is actually divergent -- validates the full
    recorded history the same way run_migrations() does, rather than
    trusting `max(applied)` at face value."""
    with engine.connect() as conn:
        if not _table_exists(conn, "schema_migrations"):
            if not _table_exists(conn, "system_state"):
                return 0
            return _detect_legacy_version(conn)
        applied = _applied_versions(conn)
        if not applied:
            return _detect_legacy_version(conn)
        _validate_recorded_history(conn, applied)
        return max(applied)


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

            if already_applied:
                _validate_recorded_history(conn, already_applied)

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

                # Correction v1.5 #2: validate this version's CUMULATIVE
                # invariants (1..version) against the real schema BEFORE
                # recording it as applied -- a migration that runs without
                # raising but doesn't actually produce everything it
                # promises must never be stamped as successful.
                if not _cumulative_invariants_satisfied(conn, version):
                    raise MigrationError(
                        f"A migração v{version} ({description}) foi executada sem levantar "
                        f"exceção, mas o esquema resultante não satisfaz todos os invariantes "
                        f"estruturais cumulativos esperados até essa versão. Por segurança, a "
                        f"versão NÃO foi registrada como aplicada e toda a transação será "
                        f"revertida; o banco permanece na versão {starting_version}."
                    )
                _record_migration(conn, version, description)
                applied_now.append(version)

            ending_version = max(starting_version, max(applied_now, default=starting_version))

            # Final full re-validation before declaring success: the
            # schema actually reachable at `ending_version` must satisfy
            # every cumulative invariant from v1 through it.
            if not _cumulative_invariants_satisfied(conn, ending_version):
                raise MigrationError(
                    f"Validação final falhou: o esquema não satisfaz os invariantes estruturais "
                    f"cumulativos esperados até a versão v{ending_version} depois da execução das "
                    f"migrações. Nenhuma alteração foi confirmada."
                )

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
