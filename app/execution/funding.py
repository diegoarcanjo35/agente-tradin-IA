"""Correção v1.1 #6: a genuinely testable funding-collection path. BYBIT_DEMO's
own transaction log (`/v5/account/transaction-log`, `type=SETTLEMENT`) is the
source of truth, reached through the exact same injectable `(url, params) ->
dict` transport shape used throughout `app/execution`/`app/market_data`
(never a second, divergent HTTP client) -- so this is testable with
`tests/fakes/bybit_fake.py`, zero real network. `PAPER_LIVE` has no private
credentials, so it is never paired with a `BybitFundingProvider` (see
`app/api/main.py::build_orchestrator`) -- funding there stays UNAVAILABLE
with an explicit reason rather than being simulated and possibly conflated
with real collected data.

Records are deduplicated by the exchange's own line-item identifier
(`funding_id`), the same idempotency guarantee pattern used for fills (see
`app/execution/fill_ledger.py`), enforced structurally by the
UNIQUE(funding_id) index added in migration v4.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.persistence.models import FundingEvent

HttpGet = Callable[[str, dict], dict]


@dataclass(frozen=True)
class FundingRecord:
    """One individual funding settlement -- `amount` follows Bybit's own
    sign convention (positive = credited to the account, negative =
    debited), so it can be added directly to net PnL without a caller
    having to know which side is which."""
    funding_id: str
    symbol: str
    amount: float
    occurred_at: datetime


class BybitFundingProvider:
    """Correção v1.1 #6: only ever constructed for BYBIT_DEMO."""

    def __init__(self, http_get: HttpGet, base_url: str):
        self._http_get = http_get
        self._base_url = base_url

    def list_funding(self, symbol: str, since: datetime | None = None) -> list[FundingRecord]:
        params: dict = {"category": "linear", "symbol": symbol, "type": "SETTLEMENT"}
        if since is not None:
            params["startTime"] = int(since.timestamp() * 1000)
        payload = self._http_get(f"{self._base_url}/v5/account/transaction-log", params)
        rows = (payload.get("result") or {}).get("list") or []

        records: list[FundingRecord] = []
        for row in rows:
            records.append(FundingRecord(
                funding_id=str(row["id"]),
                symbol=row.get("symbol", symbol),
                amount=float(row["change"]),
                occurred_at=datetime.fromtimestamp(int(row["transactionTime"]) / 1000, tz=timezone.utc),
            ))
        return records


def record_new_funding_events(session: Session, records: list[FundingRecord]) -> list[FundingEvent]:
    """Inserts only funding records not already recorded (deduped by
    `funding_id`, DB-enforced via a UNIQUE index) -- repeating the same
    line item across polls, or across a process restart with an
    overlapping `since` window, is always a safe no-op."""
    if not records:
        return []

    existing_ids = set(
        session.execute(
            select(FundingEvent.funding_id).where(
                FundingEvent.funding_id.in_([r.funding_id for r in records])
            )
        ).scalars().all()
    )

    new_rows: list[FundingEvent] = []
    for record in records:
        if record.funding_id in existing_ids:
            continue
        row = FundingEvent(
            funding_id=record.funding_id, symbol=record.symbol,
            amount=record.amount, occurred_at=record.occurred_at,
        )
        session.add(row)
        new_rows.append(row)
        existing_ids.add(record.funding_id)

    if new_rows:
        session.flush()
    return new_rows
