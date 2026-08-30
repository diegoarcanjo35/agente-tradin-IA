from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class AIRecommendationOutput(BaseModel):
    symbol: str
    recommendation: str  # BUY | SELL | HOLD
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_summary: str = Field(max_length=500)
    risk_flags: list[str] = Field(default_factory=list)
    timestamp: str  # ISO-8601

    @field_validator("recommendation")
    @classmethod
    def _validate_recommendation(cls, v: str) -> str:
        if v not in ("BUY", "SELL", "HOLD"):
            raise ValueError(f"recommendation must be BUY|SELL|HOLD, got {v!r}")
        return v

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, v: str) -> str:
        # Raises ValueError (caught by pydantic) if not a valid ISO-8601 string.
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v
