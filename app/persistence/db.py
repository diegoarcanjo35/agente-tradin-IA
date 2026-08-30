from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


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
