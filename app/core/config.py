"""Process-level configuration. Mode is chosen once at process start from the
environment; there is deliberately no runtime endpoint/mode toggle anywhere in
the API surface, per the non-negotiable "no Demo/Real switch" requirement.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ProductionEndpointBlockedError

# Only hosts on this allowlist may ever be used as the Bybit base URL. Anything
# else (including bare "api.bybit.com", the production host) is rejected at
# config load time, before any HTTP client is constructed.
ALLOWED_BYBIT_HOSTS = frozenset(
    {
        "api-demo.bybit.com",
        "stream-demo.bybit.com",
        "api-testnet.bybit.com",
        "stream-testnet.bybit.com",
    }
)

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


def assert_demo_host(url: str) -> None:
    """Fail safe: raise unless the host is on the demo/testnet allowlist."""
    from urllib.parse import urlparse

    host = urlparse(url if "://" in url else f"https://{url}").hostname or url
    host = host.lower()
    if host in KNOWN_PRODUCTION_BYBIT_HOSTS:
        raise ProductionEndpointBlockedError(
            f"Host '{host}' is a known Bybit PRODUCTION host. Refusing to start."
        )
    if host not in ALLOWED_BYBIT_HOSTS:
        raise ProductionEndpointBlockedError(
            f"Host '{host}' is not on the Bybit demo/testnet allowlist "
            f"({sorted(ALLOWED_BYBIT_HOSTS)}). Refusing to start."
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    mode: RunMode = Field(default=RunMode.REPLAY)

    bybit_api_key: str = Field(default="")
    bybit_api_secret: str = Field(default="")
    bybit_base_url: str = Field(default="https://api-demo.bybit.com")
    bybit_ws_url: str = Field(default="wss://stream-demo.bybit.com")

    database_url: str = Field(default="sqlite:///./agente_trader.db")

    symbol: str = Field(default="BTCUSDT")

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
                "BYBIT_DEMO mode requires BYBIT_API_KEY and BYBIT_API_SECRET "
                "to be set via environment variables."
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.mode == RunMode.BYBIT_DEMO:
        assert_demo_host(settings.bybit_base_url)
        assert_demo_host(settings.bybit_ws_url)
        settings.require_bybit_credentials()
    return settings
