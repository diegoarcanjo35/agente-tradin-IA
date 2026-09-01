"""Adapts the official `pybit` client to the `(url, params) -> dict` callable
shape used throughout app/market_data/bybit_provider.py and
app/execution/bybit_demo.py, so those modules stay fully testable with a fake
transport (tests/fakes/bybit_fake.py, zero network) while production wiring
goes through pybit for real, signed requests against Bybit Demo Trading.

This module is only imported when MODE=BYBIT_DEMO actually builds the
orchestrator (see app/api/main.py); importing it never makes a network call
by itself -- `pybit.unified_trading.HTTP(...)` only stores credentials, it
doesn't hit the network until a method is actually called.
"""
from __future__ import annotations

from pybit.unified_trading import HTTP

from app.core.config import assert_consistent_bybit_environment
from app.core.errors import ExchangeTimeoutError, ProductionEndpointBlockedError, RateLimitError

_RATE_LIMIT_MARKERS = ("rate limit", "too many visits", "10006", "10018", "10016")
_TIMEOUT_MARKERS = ("timed out", "timeout", "connection aborted", "read timed out")

# Correction v1.2 #6: the pybit kwargs for each environment this phase
# supports. Only "demo" is currently allowed by the config allowlist -- see
# app/core/config.py::BYBIT_HOST_ENVIRONMENTS for the full rationale.
_PYBIT_ENV_KWARGS = {
    "demo": {"demo": True, "testnet": False},
}


def _pybit_timeout(timeout_seconds: float) -> int:
    """Correção Operacional do Poll Loop v1.1 (validação complementar):
    `pybit`'s `HTTP(timeout=...)` takes an int; a naive `int(0.5)` truncates
    to `0`, which pybit/requests would treat as "no timeout" -- silently
    undoing an explicitly-configured, genuinely positive timeout. Rounds
    instead, and never returns less than 1 for a positive input."""
    return max(1, round(timeout_seconds))


def build_pybit_client(
    base_url: str, ws_url: str, api_key: str, api_secret: str, timeout_seconds: float = 10.0,
) -> HTTP:
    """Derives the pybit client mode from the VALIDATED host environment,
    instead of hardcoding demo=True regardless of what base_url actually
    says. Cross-checks base_url and ws_url resolve to the same environment
    before constructing anything.

    Correção operacional do poll loop v1.0: `timeout_seconds` is always
    passed explicitly to `HTTP()` -- pybit's own `_V5HTTPManager` already
    defaults `timeout=10`, but this app never relies on that implicit
    default; it is configured via `Settings.bybit_http_timeout_seconds`."""
    env = assert_consistent_bybit_environment(base_url, ws_url)
    kwargs = _PYBIT_ENV_KWARGS.get(env)
    if kwargs is None:
        raise ProductionEndpointBlockedError(
            f"Ambiente Bybit '{env}' não possui configuração de cliente pybit suportada "
            "nesta fase. Inicialização recusada."
        )
    return HTTP(api_key=api_key, api_secret=api_secret, timeout=_pybit_timeout(timeout_seconds), **kwargs)


def build_public_pybit_client(base_url: str, ws_url: str, timeout_seconds: float = 10.0) -> HTTP:
    """Fase 2, item 7.1 (PAPER_LIVE): the SAME host validation as
    `build_pybit_client`, but constructed with NO credentials at all --
    `pybit`'s HTTP() client works for public endpoints (kline, server time)
    without api_key/api_secret; it simply can't sign private requests
    (order create/cancel, position list), which PAPER_LIVE never calls
    anyway (see app/api/main.py -- the PAPER_LIVE branch only ever pairs
    this with PaperLocalExecutionEngine, never BybitDemoExecutionEngine).

    Correção operacional do poll loop v1.0: same explicit `timeout_seconds`
    as `build_pybit_client`."""
    env = assert_consistent_bybit_environment(base_url, ws_url)
    kwargs = _PYBIT_ENV_KWARGS.get(env)
    if kwargs is None:
        raise ProductionEndpointBlockedError(
            f"Ambiente Bybit '{env}' não possui configuração de cliente pybit suportada "
            "nesta fase. Inicialização recusada."
        )
    return HTTP(timeout=_pybit_timeout(timeout_seconds), **kwargs)


class PybitTransport:
    """Routes our internal (url, params)/(url, payload) calls to the matching
    pybit method, by pattern-matching on the Bybit V5 path suffix we already
    use elsewhere (".../v5/market/kline", ".../v5/order/create", ...). pybit
    exceptions are translated into our own ExchangeTimeoutError/RateLimitError
    so callers never need to know pybit is involved.
    """

    def __init__(self, client: HTTP):
        self._client = client

    def _call(self, fn, **kwargs) -> dict:
        try:
            return fn(**kwargs)
        except Exception as exc:  # pybit raises its own exception types for HTTP errors
            message = str(exc).lower()
            if any(marker in message for marker in _RATE_LIMIT_MARKERS):
                raise RateLimitError(str(exc)) from exc
            if any(marker in message for marker in _TIMEOUT_MARKERS):
                raise ExchangeTimeoutError(str(exc)) from exc
            raise

    def http_get(self, url: str, params: dict) -> dict:
        if url.endswith("/v5/market/kline"):
            return self._call(self._client.get_kline, **params)
        if url.endswith("/v5/market/time"):
            return self._call(self._client.get_server_time)
        if url.endswith("/v5/order/realtime"):
            # get_order_history covers filled/cancelled/rejected orders too,
            # unlike get_open_orders which drops an order once it's no longer open.
            return self._call(self._client.get_order_history, **params)
        if url.endswith("/v5/position/list"):
            return self._call(self._client.get_positions, **params)
        if url.endswith("/v5/execution/list"):
            # Correção v1.1 #1/#2: individual fills (execId, execQty,
            # execPrice, execFee) -- what the persistent fill ledger
            # dedupes against, never the cumulative totals alone.
            return self._call(self._client.get_executions, **params)
        if url.endswith("/v5/account/transaction-log"):
            # Correção v1.1 #6: individual funding settlements (SETTLEMENT
            # transaction-log rows) -- what app.execution.funding dedupes
            # against, never a simulated/fabricated value.
            return self._call(self._client.get_transaction_log, **params)
        raise NotImplementedError(f"No pybit GET mapping for {url}")

    def http_post(self, url: str, payload: dict) -> dict:
        if url.endswith("/v5/order/create"):
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            return self._call(self._client.place_order, **clean_payload)
        if url.endswith("/v5/order/cancel"):
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            return self._call(self._client.cancel_order, **clean_payload)
        if url.endswith("/v5/position/trading-stop"):
            # Correção Stop/Take Pós-Preenchimento v1.1, Bloqueio 2:
            # sincroniza a proteção (stop/alvo) da posição já aberta --
            # nunca cria/envia uma nova ordem.
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            return self._call(self._client.set_trading_stop, **clean_payload)
        raise NotImplementedError(f"No pybit POST mapping for {url}")
