from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: str  # BUY | SELL | HOLD
    justification: str
    created_at: datetime
    observed_price: float
    atr: float
    stop_loss: float | None
    take_profit: float | None
    params: dict = field(default_factory=dict)
