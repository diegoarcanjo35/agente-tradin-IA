"""Correction v1.2 #5: POST /api/kill-switch/disengage must remove ONLY the
manual kill-switch block. If any other block source is active (reconciliation
divergence/ambiguous state, clock out of sync, API failure limit reached),
trading must stay blocked and the response must say so in Portuguese.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_control
from app.persistence import repo
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from tests.test_price_correctness import build_test_orchestrator


def make_client(tmp_path) -> TestClient:
    # A real sqlite file (not ":memory:") is required here: TestClient runs
    # the request in FastAPI's threadpool, which opens a fresh connection --
    # an in-memory sqlite DB is not shared across connections/threads.
    db_path = tmp_path / "kill_switch_test.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    orch = build_test_orchestrator(session_factory, [])
    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.include_router(routes_control.router, prefix="/api")
    return TestClient(app), orch


def test_engage_then_disengage_with_no_other_blocks_fully_releases(tmp_path):
    client, orch = make_client(tmp_path)

    r1 = client.post("/api/kill-switch/engage")
    assert r1.status_code == 200
    assert r1.json()["trading_blocked"] is True

    r2 = client.post("/api/kill-switch/disengage")
    body = r2.json()
    assert body["kill_switch_engaged"] is False
    assert body["trading_blocked"] is False
    assert "liberadas" in body["mensagem"].lower()


@pytest.mark.parametrize("field,value", [
    ("state_ambiguous", True),
    ("clock_out_of_sync", True),
])
def test_disengage_does_not_clear_other_active_blocks(tmp_path, field, value):
    client, orch = make_client(tmp_path)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = True
        setattr(state, field, value)
        repo.recompute_trading_blocked(state, orch.settings.risk_max_api_failures)

    response = client.post("/api/kill-switch/disengage")
    body = response.json()
    assert body["kill_switch_engaged"] is False
    assert body["trading_blocked"] is True
    assert "bloqueadas" in body["mensagem"].lower()

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.kill_switch_engaged is False
        assert state.trading_blocked is True
        assert getattr(state, field) is True


def test_disengage_does_not_clear_block_from_api_failure_limit(tmp_path):
    client, orch = make_client(tmp_path)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = True
        state.api_failure_count = orch.settings.risk_max_api_failures
        repo.recompute_trading_blocked(state, orch.settings.risk_max_api_failures)

    response = client.post("/api/kill-switch/disengage")
    body = response.json()
    assert body["trading_blocked"] is True
    assert "falhas" in body["mensagem"].lower() or "api" in body["mensagem"].lower()


def test_engage_reports_portuguese_message(tmp_path):
    client, orch = make_client(tmp_path)
    response = client.post("/api/kill-switch/engage")
    body = response.json()
    assert "bloqueio de emergência" in body["mensagem"].lower()
