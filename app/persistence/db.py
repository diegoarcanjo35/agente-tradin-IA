from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str):
    """Correction v1.4 #1: the standard library `sqlite3` driver does NOT
    give SQLAlchemy real transactional DDL by default -- it implicitly
    issues its own COMMIT around statements it recognizes as DDL (ALTER
    TABLE, CREATE INDEX, CREATE TABLE, ...), regardless of an open
    `engine.begin()`/BEGIN block. That is exactly why an earlier version of
    the migration system could run `ALTER TABLE ... ADD COLUMN`, hit a
    later exception, and still find the column present afterward: the ADD
    COLUMN had already been auto-committed by the driver before the
    exception even happened, so there was nothing left for SQLAlchemy's
    rollback to undo.

    The documented fix (see the SQLAlchemy docs, "Serializable isolation /
    Savepoints / Transactional DDL" for pysqlite) is to disable the
    driver's own implicit transaction handling entirely (`isolation_level =
    None`, i.e. autocommit at the DBAPI level) and have SQLAlchemy issue an
    explicit `BEGIN` itself on every transaction -- including DDL. With
    this in place, `engine.begin()` wraps CREATE TABLE/ALTER TABLE/CREATE
    INDEX exactly like any DML, and a later exception genuinely rolls all
    of it back. Verified adversarially in
    tests/test_migrations.py::test_alter_table_add_column_is_rolled_back_on_later_failure.
    """
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_disable_driver_autocommit(dbapi_connection, connection_record):
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def _sqlite_emit_explicit_begin(conn):
            conn.exec_driver_sql("BEGIN")

    return engine


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db(engine) -> None:
    """Correction v1.3 #1: brings the schema up to date via the versioned
    migration system instead of a bare `create_all()` -- see
    app/persistence/migrations.py. Safe on a brand-new DB, a Fase 1 baseline
    DB, a v1.1 DB, or an already-current DB."""
    from app.persistence.migrations import run_migrations

    run_migrations(engine)


@contextmanager
def session_scope(session_factory: sessionmaker) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
