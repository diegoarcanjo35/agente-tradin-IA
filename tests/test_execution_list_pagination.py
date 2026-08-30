"""Correção da Fase 2 v1.2 #2: `/v5/execution/list` is a paginated endpoint
-- a single unpaginated call can silently miss fills whenever an order has
more executions than fit in one page. `BybitDemoExecutionEngine.poll_order()`
must walk `nextPageCursor` to the end, with defensive detection of a
repeated cursor, a malformed page, or a page-count limit exceeded -- these
are unit-level tests directly against the engine (not through the full
orchestrator) to isolate the pagination contract itself.
"""
from __future__ import annotations

import pytest

from app.core.errors import ExchangeTimeoutError
from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.execution.order_state import OrderStatus
from tests.fakes.bybit_fake import FakeBybitTransport


def _make_engine(transport: FakeBybitTransport) -> BybitDemoExecutionEngine:
    return BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get, sleep=lambda s: None,
    )


def _exec_row(i: int) -> dict:
    return {"execId": f"EXEC-{i}", "execQty": "0.001", "execPrice": "100.0", "execFee": "0.00001"}


def test_more_than_one_page_and_more_than_50_fills_in_adversarial_response_order(tmp_path=None):
    """Mais de uma página e mais de 50 fills, na ordem de resposta
    adversarial (não crescente por execId) -- todos devem ser coletados."""
    transport = FakeBybitTransport()
    engine = _make_engine(transport)
    transport.queue_status("EX-PAGE-1", [{"orderStatus": "Filled"}])

    # 63 fills, spread across 3 pages, in a scrambled (non-sequential) order
    # within each page -- adversarial, not the tidy order a naive
    # implementation might assume.
    all_ids = list(range(63))
    page1_ids = all_ids[30:60][::-1]  # scrambled slice
    page2_ids = all_ids[0:30]
    page3_ids = all_ids[60:63][::-1]
    transport.queue_execution_pages("EX-PAGE-1", [
        {"list": [_exec_row(i) for i in page1_ids], "nextPageCursor": "cursor-A"},
        {"list": [_exec_row(i) for i in page2_ids], "nextPageCursor": "cursor-B"},
        {"list": [_exec_row(i) for i in page3_ids]},  # final page: no cursor
    ])

    snapshot = engine.poll_order("EX-PAGE-1")

    assert snapshot.status == OrderStatus.FILLED
    assert snapshot.fills_complete is True
    assert len(snapshot.fills) == 63
    assert {f.exchange_fill_id for f in snapshot.fills} == {f"EXEC-{i}" for i in all_ids}


def test_overlapping_repetition_between_pages_does_not_duplicate_after_ledger_dedup():
    """Repetição sobreposta entre páginas (a corretora reenvia o mesmo
    execId em duas páginas adjacentes, um cenário adversarial real) --
    poll_order() em si reporta a lista bruta (a deduplicação é
    responsabilidade do ledger, não deste método), mas prova que nenhum
    dado é perdido nessa sobreposição."""
    transport = FakeBybitTransport()
    engine = _make_engine(transport)
    transport.queue_status("EX-PAGE-2", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-PAGE-2", [
        {"list": [_exec_row(0), _exec_row(1)], "nextPageCursor": "cursor-X"},
        {"list": [_exec_row(1), _exec_row(2)]},  # EXEC-1 repeated across the boundary
    ])

    snapshot = engine.poll_order("EX-PAGE-2")

    assert snapshot.fills_complete is True
    ids = [f.exchange_fill_id for f in snapshot.fills]
    assert ids == ["EXEC-0", "EXEC-1", "EXEC-1", "EXEC-2"]  # raw, as reported
    assert len(set(ids)) == 3  # the ledger (fill_ledger.record_new_fills) is what dedupes this


def test_repeated_cursor_stops_pagination_and_reports_incomplete():
    transport = FakeBybitTransport()
    engine = _make_engine(transport)
    transport.queue_status("EX-PAGE-3", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-PAGE-3", [
        {"list": [_exec_row(0)], "nextPageCursor": "cursor-LOOP"},
        {"list": [_exec_row(1)], "nextPageCursor": "cursor-LOOP"},  # same cursor again -- adversarial loop
    ])

    snapshot = engine.poll_order("EX-PAGE-3")

    assert snapshot.fills_complete is False
    # Both pages' rows were legitimately parsed before the SECOND page's
    # cursor was found to repeat -- nothing already-validated is discarded
    # just because pagination stops here.
    assert {f.exchange_fill_id for f in snapshot.fills} == {"EXEC-0", "EXEC-1"}


def test_intermediate_failure_between_pages_reports_incomplete_but_keeps_earlier_fills():
    transport = FakeBybitTransport()
    engine = _make_engine(transport)
    transport.queue_status("EX-PAGE-4", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-PAGE-4", [
        {"list": [_exec_row(0), _exec_row(1)], "nextPageCursor": "cursor-Y"},
        ExchangeTimeoutError("timeout simulado na página seguinte"),
    ])

    snapshot = engine.poll_order("EX-PAGE-4")

    assert snapshot.fills_complete is False
    assert {f.exchange_fill_id for f in snapshot.fills} == {"EXEC-0", "EXEC-1"}


def test_malformed_page_reports_incomplete():
    transport = FakeBybitTransport()
    engine = _make_engine(transport)
    transport.queue_status("EX-PAGE-5", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-PAGE-5", ["MALFORMED"])

    snapshot = engine.poll_order("EX-PAGE-5")

    assert snapshot.fills_complete is False
    assert snapshot.fills == []


def test_page_count_limit_exceeded_reports_incomplete():
    transport = FakeBybitTransport()
    engine = _make_engine(transport)
    transport.queue_status("EX-PAGE-6", [{"orderStatus": "Filled"}])
    # 60 pages, each with its own cursor -- exceeds the engine's defensive
    # 50-page cap, so it must stop and report incomplete rather than loop
    # forever or trust an unbounded adversarial response.
    pages = [{"list": [_exec_row(i)], "nextPageCursor": f"cursor-{i}"} for i in range(60)]
    transport.queue_execution_pages("EX-PAGE-6", pages)

    snapshot = engine.poll_order("EX-PAGE-6")

    assert snapshot.fills_complete is False
    assert len(snapshot.fills) == 50  # exactly the page cap's worth gathered


def test_single_unpaginated_page_still_reports_complete_true():
    """Regression guard: the common case (one page, no cursor) must still
    report `fills_complete=True`, matching every pre-existing test's
    expectation."""
    transport = FakeBybitTransport()
    engine = _make_engine(transport)
    transport.queue_status("EX-PAGE-7", [{"orderStatus": "Filled"}])
    transport.queue_executions("EX-PAGE-7", [_exec_row(0)])

    snapshot = engine.poll_order("EX-PAGE-7")

    assert snapshot.fills_complete is True
    assert len(snapshot.fills) == 1
