"""Correção da Fase 2 v1.2 #3: `BybitFundingProvider` used to read
`row["change"]` -- for a SETTLEMENT row that's the TOTAL account delta
(can fold in `cashFlow`/`fee`), not the funding amount itself, which is
`row["funding"]`. Also used to make a single unpaginated, unwindowed
request. This file proves: the correct field is used, full pagination is
walked, invalid rows are skipped (never coerced to zero), and the
orchestrator slices collection into multiple time windows, persisting
progress from each window as it's gathered.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.main import build_orchestrator
from app.core.clock import utcnow
from app.execution.funding import FUNDING_WINDOW_SECONDS, BybitFundingProvider
from app.persistence import repo
from app.persistence.db import session_scope
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import make_bybit_demo_settings


def _funding_row(i: int, funding: float, change: float = 999.0) -> dict:
    return {
        "id": f"F-{i}", "symbol": "BTCUSDT", "funding": str(funding), "change": str(change),
        "cashFlow": "5", "fee": "0", "transactionTime": str(1700000000000 + i * 1000),
    }


def test_funding_value_is_the_funding_field_never_change():
    """Reprodução exata do defeito: funding=-1, cashFlow=5, fee=0,
    change=4 -- deve registrar -1, nunca 4."""
    transport = FakeBybitTransport()
    transport.set_funding_events([_funding_row(1, funding=-1.0, change=4.0)])
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")

    records, complete = provider.list_funding("BTCUSDT")

    assert complete is True
    assert len(records) == 1
    assert records[0].amount == -1.0


def test_funding_paginates_more_than_20_records_across_multiple_pages():
    transport = FakeBybitTransport()
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")
    all_rows = [_funding_row(i, funding=0.1 * i) for i in range(27)]
    transport.queue_funding_pages([
        {"list": all_rows[0:10], "nextPageCursor": "cursor-1"},
        {"list": all_rows[10:20], "nextPageCursor": "cursor-2"},
        {"list": all_rows[20:27]},
    ])

    records, complete = provider.list_funding("BTCUSDT")

    assert complete is True
    assert len(records) == 27
    assert {r.funding_id for r in records} == {f"F-{i}" for i in range(27)}


def test_funding_failure_between_pages_reports_incomplete_and_keeps_earlier_records():
    transport = FakeBybitTransport()
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")
    from app.core.errors import ExchangeTimeoutError

    transport.queue_funding_pages([
        {"list": [_funding_row(1, funding=-1.0)], "nextPageCursor": "cursor-1"},
        ExchangeTimeoutError("timeout simulado"),
    ])

    records, complete = provider.list_funding("BTCUSDT")

    assert complete is False
    assert len(records) == 1
    assert records[0].funding_id == "F-1"


def test_funding_repeated_cursor_reports_incomplete():
    transport = FakeBybitTransport()
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")
    transport.queue_funding_pages([
        {"list": [_funding_row(1, -1.0)], "nextPageCursor": "cursor-LOOP"},
        {"list": [_funding_row(2, -2.0)], "nextPageCursor": "cursor-LOOP"},
    ])

    records, complete = provider.list_funding("BTCUSDT")

    assert complete is False


def test_funding_invalid_row_is_skipped_never_coerced_to_zero():
    transport = FakeBybitTransport()
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")
    valid = _funding_row(1, -1.0)
    missing_funding = {"id": "F-BAD-1", "symbol": "BTCUSDT", "transactionTime": "1700000000000"}
    non_numeric = {"id": "F-BAD-2", "symbol": "BTCUSDT", "funding": "not-a-number", "transactionTime": "1700000000000"}
    missing_id = {"symbol": "BTCUSDT", "funding": "1.0", "transactionTime": "1700000000000"}
    transport.set_funding_events([valid, missing_funding, non_numeric, missing_id])

    records, complete = provider.list_funding("BTCUSDT")

    assert complete is True
    assert len(records) == 1  # only the valid row -- never a fabricated zero for the bad ones
    assert records[0].amount == -1.0


def test_orchestrator_splits_a_large_gap_into_multiple_time_windows(tmp_path):
    """Coleta de mais de uma janela de tempo: sem `since` prévio, o
    orquestrador ancora em `now - FUNDING_WINDOW_SECONDS`; um `since` mais
    antigo que isso força múltiplas chamadas em janelas sucessivas."""
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'funding_windows.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    # Seed a funding event far in the past so `since` forces a multi-window walk.
    from app.execution.funding import FundingRecord, record_new_funding_events
    old_occurred_at = utcnow() - timedelta(seconds=FUNDING_WINDOW_SECONDS * 2.5)
    with session_scope(orch.session_factory) as session:
        record_new_funding_events(session, [
            FundingRecord("F-OLD", "BTCUSDT", -0.1, old_occurred_at),
        ])

    # Each window's fetch pops exactly one page (no cursor -> complete);
    # queue three distinct pages for the (at least) 3 windows this gap spans.
    transport.queue_funding_pages([
        {"list": [_funding_row(101, 0.01)]},
        {"list": [_funding_row(102, 0.02)]},
        {"list": [_funding_row(103, 0.03)]},
    ])

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_funding_poll_at = None
        orch._maybe_collect_funding(session, state)

    get_calls = [c for c in transport.get_calls if c[0].endswith("/v5/account/transaction-log")]
    assert len(get_calls) >= 3  # at least 3 windows walked

    with session_scope(orch.session_factory) as session:
        from sqlalchemy import select

        from app.persistence.models import FundingEvent

        # F-OLD (seed) + whatever windows successfully collected.
        total_ids = set(session.execute(select(FundingEvent.funding_id)).scalars().all())
        assert "F-OLD" in total_ids
        assert {"F-101", "F-102", "F-103"} <= total_ids


def test_orchestrator_stops_at_first_incomplete_window_and_never_skips_ahead(tmp_path):
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'funding_windows_fail.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    from app.execution.funding import FundingRecord, record_new_funding_events
    old_occurred_at = utcnow() - timedelta(seconds=FUNDING_WINDOW_SECONDS * 2.5)
    with session_scope(orch.session_factory) as session:
        record_new_funding_events(session, [FundingRecord("F-OLD-2", "BTCUSDT", -0.1, old_occurred_at)])

    from app.core.errors import ExchangeTimeoutError
    transport.queue_funding_pages([
        {"list": [_funding_row(201, 0.01)]},  # window 1: succeeds
        ExchangeTimeoutError("timeout na janela 2"),  # window 2: fails
        {"list": [_funding_row(203, 0.03)]},  # window 3: would succeed, but must never be reached
    ])

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_funding_poll_at = None
        orch._maybe_collect_funding(session, state)

    with session_scope(orch.session_factory) as session:
        failures = repo.recent_failures(session, limit=10)
        assert any(f.kind == "FAILURE" and not f.resolved for f in failures)
        events = repo.recent_security_events(session, limit=10)
        assert any(e.event_type == "FUNDING_COLLECTION_INCOMPLETE" for e in events)

        from sqlalchemy import select
        from app.persistence.models import FundingEvent
        ids = {r for r in session.execute(select(FundingEvent.funding_id)).scalars().all()}
        assert "F-201" in ids  # window 1's progress preserved
        assert "F-203" not in ids  # window 3 never collected -- no skipping ahead of the failure


def test_net_profit_metric_uses_exactly_the_sum_of_persisted_funding_field(session_factory):
    from app.execution.funding import FundingRecord, record_new_funding_events
    from app.metrics.engine import ClosedTrade, compute_metrics

    with session_scope(session_factory) as session:
        record_new_funding_events(session, [
            FundingRecord("F-M1", "BTCUSDT", -1.0, datetime(2024, 1, 1, tzinfo=timezone.utc)),
            FundingRecord("F-M2", "BTCUSDT", 0.4, datetime(2024, 1, 2, tzinfo=timezone.utc)),
        ])

    with session_scope(session_factory) as session:
        funding_total = repo.funding_total(session, "BTCUSDT")
    assert funding_total == -0.6

    trades = [ClosedTrade(realized_pnl=10.0, fees_paid=0.1, opened_at=datetime(2024, 1, 1), closed_at=datetime(2024, 1, 2))]
    result = compute_metrics(trades, starting_balance=1000.0, funding_total=funding_total)
    assert result.funding == -0.6
    assert result.net_profit == 10.0 - 0.1 - 0.6
