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

from app.core.config import assert_demo_host
from app.core.errors import ExchangeTimeoutError, RateLimitError

_RATE_LIMIT_MARKERS = ("rate limit", "too many visits", "10006", "10018", "10016")
_TIMEOUT_MARKERS = ("timed out", "timeout", "connection aborted", "read timed out")


def build_pybit_client(base_url: str, api_key: str, api_secret: str) -> HTTP:
    """Validates the configured host is demo/testnet before constructing the
    client. pybit's own `demo=True` flag routes requests to Bybit's Demo
    Trading endpoints; we still re-validate `base_url` here so a misconfigured
    non-demo host is caught before any client object exists."""
    assert_demo_host(base_url)
    return HTTP(demo=True, api_key=api_key, api_secret=api_secret)


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
        raise NotImplementedError(f"No pybit GET mapping for {url}")

    def http_post(self, url: str, payload: dict) -> dict:
        if url.endswith("/v5/order/create"):
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            return self._call(self._client.place_order, **clean_payload)
        raise NotImplementedError(f"No pybit POST mapping for {url}")
