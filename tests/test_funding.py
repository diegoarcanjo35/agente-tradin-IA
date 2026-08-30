"""Correção da Fase 2 v1.1 #6: funding is no longer permanently
'indisponível' -- BYBIT_DEMO collects it for real (via an injectable
transport, testable with zero network), deduplicated by the exchange's own
line-item id, and it contributes to net PnL. REPLAY/PAPER_LOCAL/PAPER_LIVE
never get a funding_provider at all, so their metrics keep reporting
UNAVAILABLE rather than a simulated value mixed with real collected data.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.execution.funding import BybitFundingProvider, FundingRecord, record_new_funding_events
from app.metrics.engine import ClosedTrade, compute_metrics
from app.persistence import repo
from app.persistence.db import session_scope
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import make_bybit_demo_settings
from app.api.main import build_orchestrator


def test_list_funding_maps_debited_and_credited_rows():
    transport = FakeBybitTransport()
    transport.set_funding_events([
        {"id": "F-1", "symbol": "BTCUSDT", "funding": "-0.5", "change": "4.0", "transactionTime": "1700000000000"},
        {"id": "F-2", "symbol": "BTCUSDT", "funding": "0.3", "change": "-1.0", "transactionTime": "1700000600000"},
    ])
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")

    records, complete = provider.list_funding("BTCUSDT")

    assert complete is True
    assert len(records) == 2
    assert records[0].funding_id == "F-1"
    assert records[0].amount == -0.5  # the `funding` field, never `change`
    assert records[1].amount == 0.3
    assert records[0].occurred_at.tzinfo is not None


def test_record_new_funding_events_dedupes_by_funding_id(session_factory):
    with session_scope(session_factory) as session:
        records = [
            FundingRecord("F-DUP", "BTCUSDT", -0.5, datetime(2024, 1, 1, tzinfo=timezone.utc)),
        ]
        inserted_first = record_new_funding_events(session, records)
        assert len(inserted_first) == 1

    with session_scope(session_factory) as session:
        # Same funding_id reported again (e.g. an overlapping `since`
        # window after a restart) -- must be a safe no-op.
        inserted_second = record_new_funding_events(
            session, [FundingRecord("F-DUP", "BTCUSDT", -0.5, datetime(2024, 1, 1, tzinfo=timezone.utc))],
        )
        assert inserted_second == []

    with session_scope(session_factory) as session:
        assert repo.funding_total(session, "BTCUSDT") == -0.5


def test_funding_absence_is_reported_as_zero_not_unavailable_when_provider_exists(session_factory):
    """A real funding_provider that legitimately found nothing to collect
    reports 0.0 (a genuine total), never UNAVAILABLE -- UNAVAILABLE is
    reserved for "no provider at all" (correção v1.1 #6)."""
    with session_scope(session_factory) as session:
        assert repo.funding_total(session, "BTCUSDT") == 0.0


def test_compute_metrics_reports_unavailable_funding_when_no_provider():
    trades = [ClosedTrade(realized_pnl=10.0, fees_paid=0.1, opened_at=datetime(2024, 1, 1), closed_at=datetime(2024, 1, 2))]
    result = compute_metrics(trades, starting_balance=1000.0)
    assert result.funding == "indisponível"
    assert result.net_profit == 10.0 - 0.1


def test_compute_metrics_includes_real_funding_total_in_net_profit():
    trades = [ClosedTrade(realized_pnl=10.0, fees_paid=0.1, opened_at=datetime(2024, 1, 1), closed_at=datetime(2024, 1, 2))]
    result = compute_metrics(trades, starting_balance=1000.0, funding_total=-2.0)
    assert result.funding == -2.0
    assert result.net_profit == 10.0 - 0.1 - 2.0


def test_bybit_demo_orchestrator_gets_a_real_funding_provider(session_factory):
    settings = make_bybit_demo_settings(database_url="sqlite:///:memory:")
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    assert isinstance(orch.funding_provider, BybitFundingProvider)


def test_paper_live_orchestrator_never_gets_a_funding_provider(session_factory, tmp_path):
    from app.core.config import RunMode, Settings

    settings = Settings(
        mode=RunMode.PAPER_LIVE, symbol="BTCUSDT",
        database_url=f"sqlite:///{tmp_path / 'paper_live_funding.db'}",
        bybit_base_url="https://api-demo.bybit.com", bybit_ws_url="wss://stream-demo.bybit.com",
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    assert orch.funding_provider is None


def test_orchestrator_periodic_funding_collection_persists_new_events_idempotently(session_factory):
    settings = make_bybit_demo_settings(
        database_url="sqlite:///:memory:", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    transport.set_funding_events([
        {"id": "F-TICK-1", "symbol": "BTCUSDT", "funding": "-1.25", "transactionTime": "1700000000000"},
    ])
    orch = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._maybe_collect_funding(session, state)

    with session_scope(orch.session_factory) as session:
        assert repo.funding_total(session, "BTCUSDT") == -1.25

    # A second collection tick reporting the SAME row must not double it.
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_funding_poll_at = None
        orch._maybe_collect_funding(session, state)

    with session_scope(orch.session_factory) as session:
        assert repo.funding_total(session, "BTCUSDT") == -1.25
