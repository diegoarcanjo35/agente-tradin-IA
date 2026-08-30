"""One-off deterministic generator for fixtures/replay_btcusdt.json.

Not part of the app runtime -- run manually if the fixture ever needs to be
regenerated. Uses a fixed seed and a simple deterministic formula (no
app.core.clock.utcnow(), no randomness) so output is 100% reproducible.
"""
import json
import math
from datetime import datetime, timedelta, timezone

START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
N = 180
BASE_PRICE = 40000.0

candles = []
price = BASE_PRICE
for i in range(N):
    # Deterministic wave: slow uptrend + sine oscillation + a later downtrend,
    # so a moving-average-cross strategy produces both BUY and SELL signals.
    trend = 15.0 * i if i < 90 else 15.0 * 90 - 20.0 * (i - 90)
    wave = 60.0 * math.sin(i / 6.0)
    close = BASE_PRICE + trend + wave
    open_ = price
    high = max(open_, close) + 10.0
    low = min(open_, close) - 10.0
    volume = 100.0 + 5.0 * (i % 10)
    open_time = START + timedelta(minutes=i)
    candles.append(
        {
            "open_time": open_time.isoformat(),
            "open": round(open_, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": round(volume, 2),
        }
    )
    price = close

with open("replay_btcusdt.json", "w", encoding="utf-8") as f:
    json.dump(candles, f, indent=2)

print(f"Wrote {len(candles)} candles to replay_btcusdt.json")
