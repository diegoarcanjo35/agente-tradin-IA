"""Correção v1.1 #5: optional, default-OFF external AI Shadow provider --
genuinely a different provider (not `SimulatedProvider` re-labeled), reached
through the same injectable `(url, payload) -> dict` transport shape used
throughout this app, so it is fully testable with a fake, zero real network.
Has NO access to Bybit credentials or the execution layer -- see
`app/ai_shadow/guard.py`, which structurally forbids both for every module
in this package, this one included.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Callable

HttpPost = Callable[[str, dict], dict]


def default_http_post(url: str, payload: dict) -> dict:
    """The real (stdlib-only, no new dependency) transport -- only ever
    used in production when `HttpAIProvider` is constructed without an
    explicit `http_post` override; every test supplies a fake instead."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=8.0) as response:
        return json.loads(response.read().decode("utf-8"))


class HttpAIProvider:
    """A genuinely different, generic HTTP-based provider -- posts the
    market context to a configured endpoint and returns the raw JSON text
    `AIShadowAgent.observe()` already knows how to parse (see
    `AIRecommendationOutput`). Only ever constructed when
    `ai_shadow_external_provider_enabled=True` AND `ai_provider_api_key` is
    non-empty (see `app/api/main.py::build_orchestrator`) --
    `SimulatedProvider` remains the production default in every other case,
    so a misconfiguration can never silently swap providers."""

    def __init__(
        self, endpoint_url: str, api_key: str, model_version: str = "external-v1",
        http_post: HttpPost = default_http_post,
    ):
        self._http_post = http_post
        self._endpoint_url = endpoint_url
        self._api_key = api_key
        self.model_version = model_version
        self.name = "http_external"

    def generate(self, symbol: str, market_context: dict) -> str:
        payload = {"symbol": symbol, "market_context": market_context, "api_key": self._api_key}
        response = self._http_post(self._endpoint_url, payload)
        if isinstance(response, dict) and "text" in response:
            return response["text"]
        return json.dumps(response)
