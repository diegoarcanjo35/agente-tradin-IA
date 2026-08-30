from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    """Conservative defaults. All values configurable via Settings, but the
    defaults themselves are intentionally tight for a Fase 1 demo system."""

    max_position_usd: float = 50.0
    max_concurrent_positions: int = 1
    max_daily_loss_usd: float = 25.0
    max_total_exposure_usd: float = 50.0
    cooldown_after_losses: int = 3
    cooldown_minutes: int = 30
    max_data_staleness_seconds: int = 30
    max_api_failures: int = 5
    max_clock_drift_seconds: float = 5.0
    require_stop_loss: bool = True
