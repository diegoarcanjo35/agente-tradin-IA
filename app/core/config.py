"""Process-level configuration. Mode is chosen once at process start from the
environment; there is deliberately no runtime endpoint/mode toggle anywhere in
the API surface, per the non-negotiable "no Demo/Real switch" requirement.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ProductionEndpointBlockedError, TradingSystemError


class UnsafeBindHostError(TradingSystemError):
    """Raised when the API is configured to bind to a non-local address
    without explicitly opting in -- the control API (kill switch, etc.) has
    no authentication in this phase, so it must never be exposed by
    accident."""


LOCAL_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Correction v1.2 #6: Fase 1 supports ONLY Bybit's official Demo Trading
# environment ("demo"), not plain Testnet. Reason: the `pybit` client's
# HTTP(demo=True, testnet=True) combination resolves to a THIRD host
# (api-demo-testnet.bybit.com) distinct from both api-demo.bybit.com and
# api-testnet.bybit.com, and HTTP(testnet=True, demo=False) would silently
# connect to plain Testnet even if BYBIT_BASE_URL said something else --
# there was no way for the validated base_url to actually determine which
# environment the client used. Rather than build bespoke per-host client
# wiring to support both safely, this phase supports Demo only; a plain
# Testnet host is deliberately NOT in the allowlist below (see
# docs/OPERACAO_DEMO.md for the full rationale).
BYBIT_HOST_ENVIRONMENTS: dict[str, str] = {
    "api-demo.bybit.com": "demo",
    "stream-demo.bybit.com": "demo",
}
ALLOWED_BYBIT_HOSTS = frozenset(BYBIT_HOST_ENVIRONMENTS.keys())

# Hosts that are known-production and must never be reachable, even if someone
# tries to sneak them in via .env. Checked explicitly so the failure message is
# unambiguous rather than a generic "not in allowlist".
KNOWN_PRODUCTION_BYBIT_HOSTS = frozenset(
    {
        "api.bybit.com",
        "stream.bybit.com",
        "api.bytick.com",
    }
)


class RunMode(str, Enum):
    REPLAY = "REPLAY"
    PAPER_LOCAL = "PAPER_LOCAL"
    BYBIT_DEMO = "BYBIT_DEMO"


def _extract_host(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url if "://" in url else f"https://{url}").hostname or url
    return host.lower()


def assert_demo_host(url: str) -> None:
    """Fail safe: raise unless the host is on the Bybit Demo Trading allowlist."""
    host = _extract_host(url)
    if host in KNOWN_PRODUCTION_BYBIT_HOSTS:
        raise ProductionEndpointBlockedError(
            f"Host '{host}' é um host de PRODUÇÃO conhecido da Bybit. Inicialização recusada."
        )
    if host not in ALLOWED_BYBIT_HOSTS:
        raise ProductionEndpointBlockedError(
            f"Host '{host}' não está na lista permitida de hosts Bybit Demo Trading "
            f"({sorted(ALLOWED_BYBIT_HOSTS)}). Inicialização recusada."
        )


def get_host_environment(url: str) -> str:
    """Raises via assert_demo_host() if the host isn't allowed; otherwise
    returns its Bybit environment tag ("demo")."""
    assert_demo_host(url)
    return BYBIT_HOST_ENVIRONMENTS[_extract_host(url)]


def assert_consistent_bybit_environment(base_url: str, ws_url: str) -> str:
    """Both the REST base URL and the WebSocket URL must resolve to the SAME
    Bybit environment. Raises before any client/network object is built if
    they disagree (e.g. one demo, one pointing somewhere else)."""
    base_env = get_host_environment(base_url)
    ws_env = get_host_environment(ws_url)
    if base_env != ws_env:
        raise ProductionEndpointBlockedError(
            f"BYBIT_BASE_URL (ambiente '{base_env}') e BYBIT_WS_URL (ambiente '{ws_env}') "
            "não correspondem ao mesmo ambiente Bybit. Inicialização recusada."
        )
    return base_env


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    mode: RunMode = Field(default=RunMode.REPLAY)

    bybit_api_key: str = Field(default="")
    bybit_api_secret: str = Field(default="")
    bybit_base_url: str = Field(default="https://api-demo.bybit.com")
    bybit_ws_url: str = Field(default="wss://stream-demo.bybit.com")

    database_url: str = Field(default="sqlite:///./agente_trader.db")

    symbol: str = Field(default="BTCUSDT")

    # Correction v1.2 #2: conservative, configurable polling cadence for
    # BYBIT_DEMO -- for a 1-minute timeframe there is no reason to poll
    # dozens of times per second (see docs/OPERACAO_DEMO.md). REPLAY/
    # PAPER_LOCAL use a separate, much faster interval since they never
    # touch a real rate-limited API.
    bybit_poll_interval_seconds: float = Field(default=5.0)
    replay_poll_interval_seconds: float = Field(default=0.02)

    # Correction v1.5 #1: explicit first-boot policy for market data backlog
    # draining. When set, a provider with no persisted cursor yet anchors to
    # this timestamp instead of the default "most recent closed candle at
    # boot" baseline (see app/market_data/bybit_provider.py). Never implies
    # an unbounded "recover all history" attempt either way.
    market_data_initial_start: datetime | None = Field(default=None)

    # Risk defaults (conservative). See app/risk/config.py for the dataclass
    # these seed and full documentation of each limit.
    risk_max_position_usd: float = Field(default=50.0)
    risk_max_concurrent_positions: int = Field(default=1)
    risk_max_daily_loss_usd: float = Field(default=25.0)
    risk_max_total_exposure_usd: float = Field(default=50.0)
    risk_cooldown_after_losses: int = Field(default=3)
    risk_cooldown_minutes: int = Field(default=30)
    risk_max_data_staleness_seconds: int = Field(default=30)
    risk_max_api_failures: int = Field(default=5)
    risk_max_clock_drift_seconds: float = Field(default=5.0)

    ai_shadow_enabled_default: bool = Field(default=True)
    ai_provider_api_key: str = Field(default="")
    ai_timeout_seconds: float = Field(default=8.0)
    ai_max_response_chars: int = Field(default=4000)

    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="./logs")
    log_max_bytes: int = Field(default=5_000_000)
    log_backup_count: int = Field(default=5)

    # Correction v1.3 #3: `api_host`/`api_port` are only meaningful when the
    # app is started through the official launcher (app/run.py), which is
    # the only code path that actually passes them to uvicorn -- a raw
    # `uvicorn app.api.main:app --host ...` invocation bypasses Python
    # entirely and this app has no way to observe or veto that. As a second,
    # independent layer that holds regardless of how the process was
    # started, the control endpoints (kill switch) require CONTROL_API_TOKEN
    # for any request that isn't from localhost -- see
    # app/api/routes_control.py::require_control_access and
    # docs/SEGURANCA.md.
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)
    api_allow_external_bind: bool = Field(default=False)
    control_api_token: str = Field(default="")

    @field_validator("bybit_base_url", "bybit_ws_url")
    @classmethod
    def _validate_bybit_hosts_are_never_production(cls, v: str) -> str:
        # Validated unconditionally (not just in BYBIT_DEMO mode) so a bad
        # value can never be set even if mode is later flipped without restart.
        assert_demo_host(v)
        return v

    def require_bybit_credentials(self) -> None:
        if not self.bybit_api_key or not self.bybit_api_secret:
            raise ProductionEndpointBlockedError(
                "O modo BYBIT_DEMO exige BYBIT_API_KEY e BYBIT_API_SECRET "
                "configurados via variáveis de ambiente."
            )

    def assert_safe_bind_host(self) -> None:
        if self.api_host not in LOCAL_BIND_HOSTS and not self.api_allow_external_bind:
            raise UnsafeBindHostError(
                f"API_HOST={self.api_host!r} não é um endereço local e "
                "API_ALLOW_EXTERNAL_BIND não está habilitado. A API de controle "
                "(bloqueio de emergência, etc.) não possui autenticação nesta fase; "
                "inicialização recusada para evitar exposição acidental."
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.mode == RunMode.BYBIT_DEMO:
        assert_consistent_bybit_environment(settings.bybit_base_url, settings.bybit_ws_url)
        settings.require_bybit_credentials()
    settings.assert_safe_bind_host()
    return settings
