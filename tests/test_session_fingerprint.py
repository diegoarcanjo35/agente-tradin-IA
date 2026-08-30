"""Correção da Fase 2 v1.1 #8: `start_or_resume_session` only matched by
mode+symbol, ignoring changes to strategy version, timeframe, or risk
limits -- a resumed session could silently keep operating under stale
config. Now a deterministic config fingerprint gates resumption: an exact
match resumes, anything else ends the old session and starts a new one.
"""
from __future__ import annotations

from app.core.config import RunMode, Settings
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence.models import OperationalSession
from app.risk.config import RiskLimits
from app.sessions import _config_fingerprint, start_or_resume_session

_BASE_LIMITS = RiskLimits(
    max_position_usd=50.0, max_concurrent_positions=1, max_daily_loss_usd=25.0,
    max_total_exposure_usd=50.0, cooldown_after_losses=3, cooldown_minutes=30,
    max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0,
)


def _make_session_factory(tmp_path, name="fingerprint.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    init_db(engine)
    return make_session_factory(engine)


def _settings(**overrides) -> Settings:
    defaults = dict(mode=RunMode.REPLAY, symbol="BTCUSDT", database_url="sqlite:///:memory:")
    defaults.update(overrides)
    return Settings(**defaults)


def test_identical_config_resumes_the_same_session(tmp_path):
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()

    with session_scope(session_factory) as session:
        first = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        first_id = first.id
        first_uid = first.session_uid

    with session_scope(session_factory) as session:
        second = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        assert second.id == first_id
        assert second.session_uid == first_uid
        assert second.ended_at is None

    with session_scope(session_factory) as session:
        from sqlalchemy import select
        all_sessions = session.execute(select(OperationalSession)).scalars().all()
        assert len(all_sessions) == 1  # never a second row for identical config


def test_strategy_version_change_ends_old_session_and_starts_a_new_one(tmp_path):
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        old_id = old.id

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings, "v2", _BASE_LIMITS)
        assert new.id != old_id
        assert new.ended_at is None

    with session_scope(session_factory) as session:
        old_row = session.get(OperationalSession, old_id)
        assert old_row.ended_at is not None
        assert "configuração" in old_row.end_reason.lower() or "fingerprint" in old_row.end_reason.lower()


def test_risk_limits_change_ends_old_session_and_starts_a_new_one(tmp_path):
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()
    changed_limits = RiskLimits(
        max_position_usd=999.0, max_concurrent_positions=1, max_daily_loss_usd=25.0,
        max_total_exposure_usd=50.0, cooldown_after_losses=3, cooldown_minutes=30,
        max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0,
    )

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        old_id = old.id

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings, "v1", changed_limits)
        assert new.id != old_id

    with session_scope(session_factory) as session:
        old_row = session.get(OperationalSession, old_id)
        assert old_row.ended_at is not None


def test_timeframe_component_of_the_fingerprint_differs_when_declared_differently():
    """The fingerprint is sensitive to the timeframe component even though
    every current caller passes the same literal "1" -- proven directly at
    the fingerprint-function level rather than needing a second timeframe
    plumbed all the way through Settings."""
    settings = _settings()
    fp_a = _config_fingerprint(settings, "v1", _BASE_LIMITS)

    import app.sessions as sessions_module

    real_snapshot_fn = sessions_module._sanitized_config_snapshot
    try:
        sessions_module._sanitized_config_snapshot = lambda s: {**real_snapshot_fn(s), "_tf_marker": "5"}
        fp_b = sessions_module._config_fingerprint(settings, "v1", _BASE_LIMITS)
    finally:
        sessions_module._sanitized_config_snapshot = real_snapshot_fn

    assert fp_a != fp_b


def test_a_session_with_no_fingerprint_at_all_is_never_silently_resumed(tmp_path):
    """A pre-correção-v1.1 row (created before config_fingerprint existed)
    must not be trusted implicitly -- treated exactly like a mismatch."""
    session_factory = _make_session_factory(tmp_path)
    settings = _settings()

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        old.config_fingerprint = None  # simulate a legacy row
        old_id = old.id

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings, "v1", _BASE_LIMITS)
        assert new.id != old_id

    with session_scope(session_factory) as session:
        old_row = session.get(OperationalSession, old_id)
        assert old_row.ended_at is not None


def test_fingerprint_and_snapshot_never_contain_bybit_credentials():
    settings = _settings(
        mode=RunMode.BYBIT_DEMO, bybit_api_key="super-secret-key", bybit_api_secret="super-secret-secret",
        bybit_base_url="https://api-demo.bybit.com", bybit_ws_url="wss://stream-demo.bybit.com",
    )
    fingerprint = _config_fingerprint(settings, "v1", _BASE_LIMITS)
    assert "super-secret-key" not in fingerprint
    assert "super-secret-secret" not in fingerprint

    import app.sessions as sessions_module
    snapshot = sessions_module._sanitized_config_snapshot(settings)
    import json
    snapshot_text = json.dumps(snapshot)
    assert "super-secret-key" not in snapshot_text
    assert "super-secret-secret" not in snapshot_text
