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


def test_require_local_or_authenticated_allows_local_even_with_token_configured_and_no_header():
    """Correction v1.4 #4: configuring CONTROL_API_TOKEN protects REMOTE
    access -- it must never lock out the local panel, which never sends
    (and must never need to know) the token."""
    settings = Settings(control_api_token="s3cr3t")
    request = _FakeRequest("127.0.0.1", settings, headers={})
    require_local_or_authenticated(request)  # must not raise


def test_require_local_or_authenticated_denies_when_client_is_absent():
    """request.client can be None (e.g. certain raw ASGI transports) --
    treated as untrusted/non-local, never as an implicit bypass."""
    settings = Settings(control_api_token="")
    request = _FakeRequest(None, settings)
    with pytest.raises(HTTPException) as excinfo:
        require_local_or_authenticated(request)
    assert excinfo.value.status_code == 403


def test_spoofed_forwarded_headers_alone_never_grant_access():
    """A remote client cannot talk its way into "local" by sending common
    reverse-proxy headers -- only the real, ASGI-server-reported
    request.client.host is ever consulted for the trust decision."""
    settings = Settings(control_api_token="")
    request = _FakeRequest(
        "203.0.113.5", settings,
        headers={
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
            "X-Forwarded-Host": "127.0.0.1",
        },
    )
    with pytest.raises(HTTPException) as excinfo:
        require_local_or_authenticated(request)
    assert excinfo.value.status_code == 403


# --- Integração real via TestClient (rota completa, não só a dependência) -
# TestClient always presents as client_host="testclient" (included in
# LOCAL_CLIENT_HOSTS) -- these exercise the actual HTTP route stack for the
# LOCAL-access policy; the REMOTE-access policy (deny without token, allow
# with correct token, deny with wrong token, deny with no request.client,
# deny on spoofed forwarded headers) is proven directly against
# require_local_or_authenticated() above, since TestClient has no supported
# way to present a different peer address.

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


def test_local_panel_engage_and_disengage_work_with_token_configured(tmp_path):
    """Item 8 of the required proof: the LOCAL panel flow (engage then
    disengage) must keep working end to end even with CONTROL_API_TOKEN
    configured for remote protection -- reproducing the exact bug the
    audit found (token configured => local panel locked out)."""
    client, _ = _make_client(tmp_path, control_api_token="s3cr3t")

    engage_response = client.post("/api/kill-switch/engage")
    assert engage_response.status_code == 200
    assert engage_response.json()["kill_switch_engaged"] is True

    disengage_response = client.post("/api/kill-switch/disengage")  # no header, still local
    assert disengage_response.status_code == 200
    assert disengage_response.json()["kill_switch_engaged"] is False


def test_local_panel_disengage_works_without_any_token_configured(tmp_path):
    client, _ = _make_client(tmp_path, control_api_token="")
    client.post("/api/kill-switch/engage")
    response = client.post("/api/kill-switch/disengage")
    assert response.status_code == 200
    assert response.json()["kill_switch_engaged"] is False


def test_frontend_never_sends_or_embeds_the_control_token():
    """The token must never be exposed in JS/HTML -- the local panel simply
    doesn't participate in the token scheme at all (see module docstring in
    app/api/routes_control.py)."""
    from pathlib import Path

    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    app_js = (frontend_dir / "app.js").read_text(encoding="utf-8")
    index_html = (frontend_dir / "index.html").read_text(encoding="utf-8")
    assert "X-Control-Token" not in app_js
    assert "CONTROL_API_TOKEN" not in app_js
    assert "X-Control-Token" not in index_html
    assert "CONTROL_API_TOKEN" not in index_html
