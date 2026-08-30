"""Correção v1.1 #6 / v1.2 #3: a genuinely testable funding-collection path.
BYBIT_DEMO's own transaction log (`/v5/account/transaction-log`,
`type=SETTLEMENT`) is the source of truth, reached through the exact same
injectable `(url, params) -> dict` transport shape used throughout
`app/execution`/`app/market_data` (never a second, divergent HTTP client) --
so this is testable with `tests/fakes/bybit_fake.py`, zero real network.
`PAPER_LIVE` has no private credentials, so it is never paired with a
`BybitFundingProvider` (see `app/api/main.py::build_orchestrator`) --
funding there stays UNAVAILABLE with an explicit reason rather than being
simulated and possibly conflated with real collected data.

Records are deduplicated by the exchange's own line-item identifier
(`funding_id`), the same idempotency guarantee pattern used for fills (see
`app/execution/fill_ledger.py`), enforced structurally by the
UNIQUE(funding_id) index added in migration v4.

Correção v1.2 #3: `list_funding` used to read `row["change"]` -- for a
SETTLEMENT row that is the TOTAL account delta (which can also fold in
`cashFlow`/`fee`), not the funding amount itself. The funding value proper
is `row["funding"]`, and that is now the ONLY field ever persisted as the
funding amount. `list_funding` also never raises on a partial/incomplete
fetch (timeout, rate limit, malformed page, repeated cursor, page cap) --
it returns `(records_validated_so_far, complete)` so a caller can persist
real progress without ever claiming a period is fully reconciled when it
is not.

Correção v1.3 #1/#2: two further gaps closed. (1) `complete=True` used to
mean only "pagination itself finished cleanly" -- an invalid row (missing
`funding`, non-numeric, missing `id`/`transactionTime`) was silently
skipped and the page/window was STILL reported complete, letting a caller
advance its coverage checkpoint past a record it never actually collected.
Now ANY invalid row marks the whole window `complete=False` (a structured,
credential-free diagnostic is logged via `log_event` for each one -- never
raw payloads). (2) the caller (`Orchestrator._maybe_collect_funding`) no
longer derives its retomada `since` from the MAX `occurred_at` already
persisted (unsafe under newest-first pagination -- see
`app/persistence/repo.py::get_funding_checkpoint`'s docstring for the exact
reproduced failure) -- it now reads an explicit, separately-persisted
`FundingCollectionCheckpoint` that only ever advances once an entire
window is proven complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ExchangeTimeoutError, RateLimitError
from app.core.logging import get_logger, log_event
from app.persistence.models import FundingEvent

logger = get_logger(__name__)

HttpGet = Callable[[str, dict], dict]

_FUNDING_PAGE_LIMIT = 50
_MAX_FUNDING_PAGES = 50

# Correção v1.2 #3: the orchestrator slices [since, now] into windows of at
# most this many seconds (7 days) before calling list_funding for each --
# a real request-scoping strategy against a transaction-log API, not just a
# single unbounded startTime-only query.
FUNDING_WINDOW_SECONDS = 7 * 24 * 3600


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

    def list_funding(
        self, symbol: str, since: datetime | None = None, until: datetime | None = None,
    ) -> tuple[list[FundingRecord], bool]:
        """Correção v1.2 #3 / v1.3 #2: walks every page of
        `/v5/account/transaction-log` (`nextPageCursor`) for the
        `[since, until]` window, with an explicit page `limit`, a
        repeated-cursor guard, malformed-page detection, and a defensive
        page-count cap. Returns `(records, complete)` -- NEVER raises and
        never discards records already validated before an interruption,
        so a caller can safely persist partial progress.

        Correção v1.3 #2: a row missing/mistyping a required field (`id`,
        `symbol`, `funding`, `transactionTime`) is never turned into a
        fabricated record AND now marks the ENTIRE window `complete=False`
        -- before this correction, an invalid row was silently skipped
        while pagination itself still reported success, letting a caller
        advance its coverage checkpoint past a record it never actually
        collected. A structured (credential/payload-free) diagnostic is
        logged for each invalid row via `log_event`."""
        records: list[FundingRecord] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        complete = True

        base_params: dict = {"category": "linear", "symbol": symbol, "type": "SETTLEMENT"}
        if since is not None:
            base_params["startTime"] = int(since.timestamp() * 1000)
        if until is not None:
            base_params["endTime"] = int(until.timestamp() * 1000)

        for _ in range(_MAX_FUNDING_PAGES):
            params = {**base_params, "limit": _FUNDING_PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            try:
                payload = self._http_get(f"{self._base_url}/v5/account/transaction-log", params)
            except (ExchangeTimeoutError, RateLimitError):
                return records, False

            result = payload.get("result") or {}
            rows = result.get("list")
            if rows is None or not isinstance(rows, list):
                return records, False

            for row in rows:
                record, error_reason = self._parse_row(row, symbol)
                if error_reason is not None:
                    complete = False
                    log_event(logger, 30, "funding_row_invalid", reason=error_reason)
                    continue
                records.append(record)

            next_cursor = result.get("nextPageCursor")
            if not next_cursor:
                return records, complete
            if next_cursor in seen_cursors:
                return records, False
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return records, False

    @staticmethod
    def _parse_row(row: dict, symbol: str) -> tuple[FundingRecord | None, str | None]:
        """Correção v1.2 #3 / v1.3 #2: validates presence/type of every
        required field before ever producing a record. Returns
        `(record, None)` on success or `(None, reason)` on failure --
        `reason` is a short, structured, credential/payload-free diagnostic
        string (never the raw row) suitable for logging."""
        try:
            funding_id = str(row["id"])
        except (KeyError, TypeError):
            return None, "id ausente ou de tipo inválido"
        if not funding_id:
            return None, "id vazio"

        row_symbol = row.get("symbol") or symbol
        if not row_symbol:
            return None, f"symbol ausente (id={funding_id})"

        try:
            amount = float(row["funding"])
        except (KeyError, TypeError, ValueError):
            return None, f"campo funding ausente ou não numérico (id={funding_id})"

        try:
            occurred_at = datetime.fromtimestamp(int(row["transactionTime"]) / 1000, tz=timezone.utc)
        except (KeyError, TypeError, ValueError):
            return None, f"transactionTime ausente ou inválido (id={funding_id})"

        return FundingRecord(funding_id=funding_id, symbol=row_symbol, amount=amount, occurred_at=occurred_at), None


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
