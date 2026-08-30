"""Correction v1.3 #2: BybitDemoMarketDataProvider must fetch enough kline
rows to always be able to find the previous CLOSED candle even when the
newest row (per Bybit's real newest-first ordering) is still forming, and
must never skip a pending closed candle when several have piled up.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market_data.base import CandleFetchStatus
from app.market_data.bybit_provider import BybitDemoMarketDataProvider


def _row(open_time: datetime, close: str) -> list:
    return [str(int(open_time.timestamp() * 1000)), close, close, close, close, "10", "0"]


def test_newest_first_response_with_open_then_closed_selects_the_closed_one():
    """Exact scenario from the correction: response is newest-first --
    candle 12:01 (still open) followed by candle 12:00 (closed). The
    provider must select 12:00, never report NO_NEW_CANDLE forever."""
    fixed_now = datetime(2024, 6, 1, 12, 1, 30, tzinfo=timezone.utc)
    open_1201 = fixed_now.replace(second=0)  # started 90s ago... still within its own minute? Adjust below.
    open_1201 = datetime(2024, 6, 1, 12, 1, 0, tzinfo=timezone.utc)  # closes at 12:02 -- still open at 12:01:30
    open_1200 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)  # closes at 12:01 -- already closed

    # Bybit returns newest-first: [12:01 (open), 12:00 (closed)].
    rows = [_row(open_1201, "105"), _row(open_1200, "100")]

    def fake_get(url, params):
        return {"result": {"list": rows}}

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now, page_size=5,
    )

    result = provider.next_candle()
    assert result.status == CandleFetchStatus.CANDLE_AVAILABLE
    assert result.candle.open_time == open_1200
    assert result.candle.close == 100.0

    # The still-open 12:01 candle must not be returned yet on the next call.
    fixed_now_2 = datetime(2024, 6, 1, 12, 1, 45, tzinfo=timezone.utc)
    provider._now_fn = lambda: fixed_now_2
    result2 = provider.next_candle()
    assert result2.status == CandleFetchStatus.NO_NEW_CANDLE


def test_never_returns_only_a_forming_candle_across_repeated_polls():
    """Regression guard for the exact failure mode described in the
    correction: polling repeatedly while the newest candle keeps being the
    one still in formation must NOT mean the previous closed candle is lost
    forever -- as long as it's still within the fetch window."""
    open_1200 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    open_1201 = datetime(2024, 6, 1, 12, 1, 0, tzinfo=timezone.utc)

    call_times = iter([
        datetime(2024, 6, 1, 12, 0, 59, tzinfo=timezone.utc),  # 12:00 still forming
        datetime(2024, 6, 1, 12, 1, 1, tzinfo=timezone.utc),   # 12:00 just closed, 12:01 forming
    ])

    def fake_get(url, params):
        # Bybit always includes some trailing history alongside the latest row.
        return {"result": {"list": [_row(open_1201, "105"), _row(open_1200, "100")]}}

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: next(call_times), page_size=5,
    )

    first = provider.next_candle()
    assert first.status == CandleFetchStatus.NO_NEW_CANDLE  # 12:00 not closed yet at :59

    second = provider.next_candle()
    assert second.status == CandleFetchStatus.CANDLE_AVAILABLE
    assert second.candle.open_time == open_1200


def test_three_pending_closed_candles_are_delivered_once_each_in_chronological_order():
    base = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    opens = [base + timedelta(minutes=i) for i in range(3)]  # 12:00, 12:01, 12:02
    forming_open = base + timedelta(minutes=3)  # 12:03, still open

    fixed_now = base + timedelta(minutes=3, seconds=30)  # all of 12:00-12:02 are closed; 12:03 is not

    # Newest-first: forming candle, then the three closed ones descending.
    rows = [_row(forming_open, "999")] + [_row(t, str(100 + i)) for i, t in reversed(list(enumerate(opens)))]

    def fake_get(url, params):
        return {"result": {"list": rows}}

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now, page_size=5,
    )

    delivered = []
    for _ in range(3):
        result = provider.next_candle()
        assert result.status == CandleFetchStatus.CANDLE_AVAILABLE
        delivered.append(result.candle.open_time)

    assert delivered == opens  # chronological order, each exactly once, no gaps

    # A fourth call finds nothing new (12:03 is still forming).
    result4 = provider.next_candle()
    assert result4.status == CandleFetchStatus.NO_NEW_CANDLE


def test_minute_turnover_before_and_after_closure():
    """Reproduces the correction's exact turnover sequence: before closure
    (nothing new), after closure with a new current candle (the previous
    one becomes available), then that previous candle gets processed."""
    open_1200 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def fake_get(url, params):
        return {"result": {"list": [_row(open_1200, "100")]}}

    # Before closure: 12:00 started 10s ago, not closed yet.
    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1", http_get=fake_get,
        sleep=lambda s: None, now_fn=lambda: open_1200 + timedelta(seconds=10), page_size=5,
    )
    before = provider.next_candle()
    assert before.status == CandleFetchStatus.NO_NEW_CANDLE

    # After closure: now past 12:01, with a new (still forming) current candle
    # plus the now-closed 12:00 one both present in the response.
    open_1201 = open_1200 + timedelta(minutes=1)

    def fake_get_after(url, params):
        return {"result": {"list": [_row(open_1201, "105"), _row(open_1200, "100")]}}

    provider._http_get = fake_get_after
    provider._now_fn = lambda: open_1201 + timedelta(seconds=5)

    after = provider.next_candle()
    assert after.status == CandleFetchStatus.CANDLE_AVAILABLE
    assert after.candle.open_time == open_1200
    assert after.candle.close == 100.0
