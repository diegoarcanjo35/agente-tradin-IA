"""Correção da Fase 2 v1.3 #1/#2/#7: `Orchestrator._maybe_collect_funding`
used `repo.last_funding_occurred_at()` (the MAX `occurred_at` already
persisted) as its retomada `since`. Under a newest-first paginated
response, page 1 could persist a recent record and page 2 (older) could
then fail -- the next cycle's `since` would jump PAST the still-unfetched
backlog, making it permanently unreachable. This file proves the fix: an
explicit, separately-persisted `FundingCollectionCheckpoint` that only
advances once an entire window is proven complete (every page fetched,
every row valid), and never based on which records happened to come back
or in what order.
"""
from __future__ import annotations

from datetime import timedelta

from app.api.main import build_orchestrator
from app.core.clock import utcnow
from app.core.errors import ExchangeTimeoutError
from app.execution.funding import FUNDING_WINDOW_SECONDS, BybitFundingProvider
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import FundingEvent
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import make_bybit_demo_settings
from tests.test_funding_pagination_and_correctness import _funding_row


def _funding_ids(session_factory) -> set[str]:
    from sqlalchemy import select
    with session_scope(session_factory) as session:
        return set(session.execute(select(FundingEvent.funding_id)).scalars().all())


def _collect(orch, session_factory):
    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_funding_poll_at = None
        orch._maybe_collect_funding(session, state)


# --- 1/3: reprodução exata + retomada usando a mesma janela/checkpoint ------

def test_newest_first_page_persisted_then_older_page_timeout_retry_recovers_everything(tmp_path):
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'checkpoint_retry.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    # Newest-first page persists, older page (in the SAME window) times out.
    transport.queue_funding_pages([
        {"list": [_funding_row(2, 0.02)], "nextPageCursor": "cursor-older"},
        ExchangeTimeoutError("timeout na página mais antiga"),
    ])
    _collect(orch, orch.session_factory)

    with session_scope(orch.session_factory) as session:
        checkpoint = repo.get_funding_checkpoint(session, "BTCUSDT")
        # The checkpoint never advanced past this incomplete window...
        assert checkpoint is None
    # ...even though the partial progress (F-2) WAS persisted.
    assert "F-2" in _funding_ids(orch.session_factory)

    # Retry: same (unchanged) window is retried, and this time succeeds with
    # everything (both the already-persisted F-2 and the previously-missed
    # older F-1) in a single page.
    transport.queue_funding_pages([
        {"list": [_funding_row(2, 0.02), _funding_row(1, 0.01)]},
    ])
    _collect(orch, orch.session_factory)

    ids = _funding_ids(orch.session_factory)
    assert {"F-1", "F-2"} <= ids  # nothing lost, F-1 recovered
    with session_scope(orch.session_factory) as session:
        assert repo.get_funding_checkpoint(session, "BTCUSDT") is not None  # now genuinely covered


def test_partial_records_persisted_before_the_failure_are_never_duplicated_on_retry(tmp_path):
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'checkpoint_no_dup.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    transport.queue_funding_pages([
        {"list": [_funding_row(2, 0.02)], "nextPageCursor": "cursor-older"},
        ExchangeTimeoutError("timeout simulado"),
    ])
    _collect(orch, orch.session_factory)

    # Retry re-reports F-2 (as the real API would, since it's still within
    # the unchanged window) alongside the newly-available F-1.
    transport.queue_funding_pages([
        {"list": [_funding_row(2, 0.02), _funding_row(1, 0.01)]},
    ])
    _collect(orch, orch.session_factory)

    with session_scope(orch.session_factory) as session:
        assert repo.funding_total(session, "BTCUSDT") == 0.02 + 0.01  # never 0.04 + 0.01


# --- 2: reinício entre a falha e o retry ------------------------------------

def test_restart_between_failure_and_retry_resumes_purely_from_the_database(tmp_path):
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'checkpoint_restart.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch_before = build_orchestrator(settings, bybit_transport=transport)

    transport.queue_funding_pages([
        {"list": [_funding_row(2, 0.02)], "nextPageCursor": "cursor-older"},
        ExchangeTimeoutError("timeout simulado"),
    ])
    _collect(orch_before, orch_before.session_factory)

    # "Restart": a brand-new Orchestrator instance, sharing nothing in
    # memory, against the SAME database and the SAME (simulated) exchange.
    orch_after = build_orchestrator(settings, bybit_transport=transport)
    transport.queue_funding_pages([
        {"list": [_funding_row(2, 0.02), _funding_row(1, 0.01)]},
    ])
    _collect(orch_after, orch_after.session_factory)

    ids = _funding_ids(orch_after.session_factory)
    assert {"F-1", "F-2"} <= ids


# --- 4: cursor repetido e limite de páginas não avançam o checkpoint -------

def test_repeated_cursor_never_advances_the_checkpoint(tmp_path):
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'checkpoint_cursor_loop.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    transport.queue_funding_pages([
        {"list": [_funding_row(1, 0.01)], "nextPageCursor": "cursor-LOOP"},
        {"list": [_funding_row(2, 0.02)], "nextPageCursor": "cursor-LOOP"},
    ])
    _collect(orch, orch.session_factory)

    with session_scope(orch.session_factory) as session:
        assert repo.get_funding_checkpoint(session, "BTCUSDT") is None


def test_page_limit_exceeded_never_advances_the_checkpoint(tmp_path):
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'checkpoint_page_limit.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    pages = [{"list": [_funding_row(i, 0.01)], "nextPageCursor": f"cursor-{i}"} for i in range(60)]
    transport.queue_funding_pages(pages)
    _collect(orch, orch.session_factory)

    with session_scope(orch.session_factory) as session:
        assert repo.get_funding_checkpoint(session, "BTCUSDT") is None


# --- 5: linha inválida torna a janela incompleta ----------------------------

def test_row_missing_funding_field_keeps_window_incomplete():
    transport = FakeBybitTransport()
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")
    row = {"id": "F-X", "symbol": "BTCUSDT", "transactionTime": "1700000000000"}  # no "funding"
    transport.set_funding_events([row])
    records, complete = provider.list_funding("BTCUSDT")
    assert complete is False


def test_row_with_non_numeric_funding_keeps_window_incomplete():
    transport = FakeBybitTransport()
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")
    row = {"id": "F-X", "symbol": "BTCUSDT", "funding": "not-a-number", "transactionTime": "1700000000000"}
    transport.set_funding_events([row])
    records, complete = provider.list_funding("BTCUSDT")
    assert complete is False


def test_row_missing_id_keeps_window_incomplete():
    transport = FakeBybitTransport()
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")
    row = {"symbol": "BTCUSDT", "funding": "0.01", "transactionTime": "1700000000000"}  # no "id"
    transport.set_funding_events([row])
    records, complete = provider.list_funding("BTCUSDT")
    assert complete is False


def test_row_with_invalid_transaction_time_keeps_window_incomplete():
    transport = FakeBybitTransport()
    provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")
    row = {"id": "F-X", "symbol": "BTCUSDT", "funding": "0.01", "transactionTime": "not-a-timestamp"}
    transport.set_funding_events([row])
    records, complete = provider.list_funding("BTCUSDT")
    assert complete is False


def test_an_invalid_row_at_the_orchestrator_level_never_advances_the_checkpoint(tmp_path):
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'checkpoint_invalid_row.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    valid = _funding_row(1, 0.01)
    invalid = {"id": "F-BAD", "symbol": "BTCUSDT", "transactionTime": "1700000000000"}  # no funding
    transport.set_funding_events([valid, invalid])
    _collect(orch, orch.session_factory)

    with session_scope(orch.session_factory) as session:
        assert repo.get_funding_checkpoint(session, "BTCUSDT") is None
    assert "F-1" in _funding_ids(orch.session_factory)  # the valid one is still saved


# --- 6: resposta válida posterior fecha a janela, checkpoint avança 1 vez --

def test_checkpoint_advances_by_exactly_one_window_once_the_window_resolves(tmp_path):
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'checkpoint_once.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    start = utcnow()
    transport.queue_funding_pages([ExchangeTimeoutError("timeout simulado")])
    _collect(orch, orch.session_factory)
    with session_scope(orch.session_factory) as session:
        assert repo.get_funding_checkpoint(session, "BTCUSDT") is None

    transport.queue_funding_pages([{"list": []}])  # genuinely empty, valid, complete
    _collect(orch, orch.session_factory)

    with session_scope(orch.session_factory) as session:
        checkpoint = repo.get_funding_checkpoint(session, "BTCUSDT")
        assert checkpoint is not None
        # With no prior checkpoint, the single window walked is
        # [now - FUNDING_WINDOW_SECONDS, now] -- once it resolves,
        # covered_until lands at "now" (approximately `start`, since the
        # whole test runs in a fraction of a second), never a second
        # window's worth further ahead.
        assert abs((checkpoint.covered_until - start).total_seconds()) < 5


# --- 8: ordenação adversarial produz o mesmo conjunto final -----------------

def test_descending_ascending_and_shuffled_order_produce_the_same_final_set():
    rows = [_funding_row(i, 0.01 * i) for i in range(5)]
    descending = list(reversed(rows))
    ascending = list(rows)
    shuffled = [rows[2], rows[0], rows[4], rows[1], rows[3]]

    results = []
    for ordering in (descending, ascending, shuffled):
        transport = FakeBybitTransport()
        provider = BybitFundingProvider(transport.http_get, "https://api-demo.bybit.com")
        transport.set_funding_events(ordering)
        records, complete = provider.list_funding("BTCUSDT")
        assert complete is True
        results.append({(r.funding_id, r.amount) for r in records})

    assert results[0] == results[1] == results[2]


# --- 9: métrica líquida = soma idempotente dos eventos válidos persistidos -

def test_net_metric_stays_the_idempotent_sum_across_repeated_collections(tmp_path):
    settings = make_bybit_demo_settings(
        database_url=f"sqlite:///{tmp_path / 'checkpoint_metric.db'}", funding_poll_interval_seconds=0.0,
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    transport.set_funding_events([_funding_row(1, -0.5), _funding_row(2, 0.2)])
    _collect(orch, orch.session_factory)
    with session_scope(orch.session_factory) as session:
        first_total = repo.funding_total(session, "BTCUSDT")
    assert first_total == -0.3

    # Collecting again (same window re-reported, e.g. a slightly overlapping
    # boundary) must never change the total.
    transport.set_funding_events([_funding_row(1, -0.5), _funding_row(2, 0.2)])
    _collect(orch, orch.session_factory)
    with session_scope(orch.session_factory) as session:
        second_total = repo.funding_total(session, "BTCUSDT")
    assert second_total == first_total == -0.3
