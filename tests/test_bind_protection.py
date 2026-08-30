"""Correction v1.3 #3: `Settings.api_host` must actually control the real
bind, not just an internal variable nobody consults -- and the kill-switch
control endpoints must stay safe even if the process was started outside
the official launcher (bypassing that host validation entirely).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import routes_control
from app.api.routes_control import LOCAL_CLIENT_HOSTS, require_local_or_authenticated
from app.core.config import RunMode, Settings, UnsafeBindHostError
from tests.test_price_correctness import build_test_orchestrator


# --- Opção A: o launcher oficial realmente controla o host do uvicorn -----

def test_launcher_passes_validated_settings_host_and_port_to_uvicorn(monkeypatch):
    """Reaches the actual call boundary to uvicorn.run() -- not just
    Settings(api_host=...) in isolation -- and proves the host/port that
    reach the server are exactly the validated ones from Settings."""
    import uvicorn

    import app.run as run_module

    captured = {}

    def fake_uvicorn_run(app_path, **kwargs):
        captured["app_path"] = app_path
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setattr(
        run_module, "get_settings",
        lambda: Settings(mode=RunMode.REPLAY, api_host="127.0.0.1", api_port=9001),
    )

    run_module.run()

    assert captured["app_path"] == "app.api.main:app"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9001


def test_launcher_default_host_is_local():
    assert Settings().api_host == "127.0.0.1"


def test_launcher_never_calls_uvicorn_when_settings_validation_fails(monkeypatch):
    """If Settings itself would refuse an unsafe host, the launcher must
    never reach uvicorn.run() at all."""
    import uvicorn

    import app.run as run_module

    called = {"n": 0}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    def raise_unsafe():
        raise UnsafeBindHostError("host externo não autorizado")

    monkeypatch.setattr(run_module, "get_settings", raise_unsafe)

    with pytest.raises(UnsafeBindHostError):
        run_module.run()

    assert called["n"] == 0


def test_external_bind_without_opt_in_is_still_refused_by_settings():
    settings = Settings(api_host="0.0.0.0")
    with pytest.raises(UnsafeBindHostError):
        settings.assert_safe_bind_host()


def test_external_bind_with_explicit_opt_in_is_allowed_by_settings():
    settings = Settings(api_host="0.0.0.0", api_allow_external_bind=True)
    settings.assert_safe_bind_host()  # must not raise


# --- Opção B/C: proteção independente do bind real, nos endpoints -------

class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeApp:
    def __init__(self, settings):
        self.state = SimpleNamespace(settings=settings)


class _FakeRequest:
    def __init__(self, client_host, settings, headers=None):
        self.client = _FakeClient(client_host) if client_host is not None else None
        self.headers = headers or {}
        self.app = _FakeApp(settings)


def test_require_local_or_authenticated_allows_local_client_without_token():
    settings = Settings(control_api_token="")
    request = _FakeRequest("127.0.0.1", settings)
    require_local_or_authenticated(request)  # must not raise


def test_require_local_or_authenticated_denies_external_client_without_token():
    settings = Settings(control_api_token="")
    request = _FakeRequest("203.0.113.5", settings)  # TEST-NET-3, definitely non-local
    with pytest.raises(HTTPException) as excinfo:
        require_local_or_authenticated(request)
    assert excinfo.value.status_code == 403


def test_require_local_or_authenticated_denies_external_client_with_wrong_token():
    settings = Settings(control_api_token="s3cr3t")
    request = _FakeRequest("203.0.113.5", settings, headers={"X-Control-Token": "wrong"})
    with pytest.raises(HTTPException) as excinfo:
        require_local_or_authenticated(request)
    assert excinfo.value.status_code == 403


def test_require_local_or_authenticated_allows_external_client_with_correct_token():
    settings = Settings(control_api_token="s3cr3t")
    request = _FakeRequest("203.0.113.5", settings, headers={"X-Control-Token": "s3cr3t"})
    require_local_or_authenticated(request)  # must not raise


def test_require_local_or_authenticated_requires_token_even_for_local_once_configured():
    """Once an operator opts into token auth, it applies uniformly -- a
    local client without the header is no longer silently exempted."""
    settings = Settings(control_api_token="s3cr3t")
    request = _FakeRequest("127.0.0.1", settings, headers={})
    with pytest.raises(HTTPException):
        require_local_or_authenticated(request)


# --- Integração real via TestClient (rota completa, não só a dependência) -

def _make_client(tmp_path, **settings_overrides):
    from app.persistence.db import init_db, make_engine, make_session_factory

    db_path = tmp_path / "bind_protection_test.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    orch = build_test_orchestrator(session_factory, [])
    for key, value in settings_overrides.items():
        setattr(orch.settings, key, value)

    app = FastAPI()
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.include_router(routes_control.router, prefix="/api")
    return TestClient(app), orch


def test_engage_never_requires_authentication(tmp_path):
    client, _ = _make_client(tmp_path, control_api_token="s3cr3t")
    response = client.post("/api/kill-switch/engage")  # no token header at all
    assert response.status_code == 200
    assert response.json()["kill_switch_engaged"] is True


def test_disengage_denied_end_to_end_when_token_configured_and_missing(tmp_path):
    client, _ = _make_client(tmp_path, control_api_token="s3cr3t")
    client.post("/api/kill-switch/engage")
    response = client.post("/api/kill-switch/disengage")  # no X-Control-Token header
    assert response.status_code == 403


def test_disengage_allowed_end_to_end_with_correct_token(tmp_path):
    client, _ = _make_client(tmp_path, control_api_token="s3cr3t")
    client.post("/api/kill-switch/engage")
    response = client.post(
        "/api/kill-switch/disengage", headers={"X-Control-Token": "s3cr3t"}
    )
    assert response.status_code == 200
    assert response.json()["kill_switch_engaged"] is False
