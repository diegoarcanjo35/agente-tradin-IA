from __future__ import annotations

import pytest

from app.persistence.db import init_db, make_engine, make_session_factory, session_scope


@pytest.fixture()
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture()
def db_session(session_factory):
    with session_scope(session_factory) as session:
        yield session
