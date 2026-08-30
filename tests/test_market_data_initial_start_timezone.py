"""Correction v1.6: `MARKET_DATA_INITIAL_START` accepted a naive (no
timezone) datetime, which later blew up comparing against timezone-aware
candle timestamps inside `BybitDemoMarketDataProvider._fetch_window()`:

    TypeError: can't compare offset-naive and offset-aware datetimes

Fixed at the earliest possible point -- `Settings` construction, via
`app/core/config.py::Settings._normalize_market_data_initial_start` -- so a
bad value fails loudly at process startup, never only during the first
polling tick. Policy: an explicit timezone/offset is honored and converted
to UTC; a value with NO timezone is accepted and interpreted AS UTC (never
left naive); a syntactically invalid value fails Settings construction
immediately.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.core.config import RunMode, Settings
from app.market_data.base import CandleFetchStatus
from app.market_data.bybit_provider import BybitDemoMarketDataProvider


def _row(open_time: datetime, close: str) -> list:
    return [str(int(open_time.timestamp() * 1000)), close, close, close, close, "10", "0"]


# --- Required test 1: value with 'Z' ----------------------------------------

def test_value_with_z_suffix_is_accepted_and_normalized_to_utc():
    settings = Settings(market_data_initial_start="2024-06-01T12:00:00Z")
    dt = settings.market_data_initial_start
    assert dt.tzinfo is not None
    assert dt == datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Required test 2: value with an explicit offset -------------------------

def test_value_with_negative_offset_is_converted_to_the_correct_utc_instant():
    settings = Settings(market_data_initial_start="2024-06-01T09:00:00-03:00")
    dt = settings.market_data_initial_start
    assert dt.tzinfo is not None
    # 09:00 at UTC-03:00 is 12:00 UTC -- the same real instant as the 'Z' test above.
    assert dt == datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Required test 3: value with no timezone --------------------------------

def test_value_with_no_timezone_is_interpreted_as_utc():
    """Policy chosen (documented in docs/OPERACAO_DEMO.md and
    app/core/config.py): a naive value is accepted and interpreted as UTC,
    never left naive -- it must never be possible for a naive datetime to
    reach the provider."""
    settings = Settings(market_data_initial_start="2024-06-01T12:00:00")
    dt = settings.market_data_initial_start
    assert dt.tzinfo is not None
    assert dt == datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Required test 4: syntactically invalid value ---------------------------

def test_syntactically_invalid_value_fails_settings_construction_immediately():
    """Must fail at Settings() construction (process startup), with a clear
    Portuguese message -- never only later, during the first poll."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(market_data_initial_start="isto-nao-e-uma-data")
    assert "MARKET_DATA_INITIAL_START" in str(excinfo.value)


# --- Required test 5: value in the future -----------------------------------

def test_future_value_behaves_safely_with_no_crash_and_no_open_candle_delivered():
    future_start = datetime(2999, 1, 1, tzinfo=timezone.utc)
    settings = Settings(market_data_initial_start=future_start)
    assert settings.market_data_initial_start == future_start

    def fake_get(url, params):
        if url.endswith("/v5/market/time"):
            return {"result": {"timeSecond": "0"}}
        return {"result": {"list": []}}  # nothing exists that far in the future

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: None,
        now_fn=lambda: datetime(2024, 6, 1, tzinfo=timezone.utc),
        initial_start=settings.market_data_initial_start,
    )

    # No crash comparing naive vs aware; nothing to deliver yet.
    result = provider.next_candle()
    assert result.status == CandleFetchStatus.NO_NEW_CANDLE
    assert result.candle is None


# --- Required test 6: integration through real Settings --------------------

def test_integration_settings_through_provider_next_candle_never_raises_typeerror():
    """The exact reproduction path from the audit: a real `Settings`
    instance with a naive-looking `MARKET_DATA_INITIAL_START` string, wired
    all the way through to `BybitDemoMarketDataProvider.next_candle()` --
    must never raise
    `TypeError: can't compare offset-naive and offset-aware datetimes`."""
    settings = Settings(market_data_initial_start="2024-06-01T12:00:00")
    configured_start = settings.market_data_initial_start
    assert configured_start.tzinfo is not None

    rows = [_row(configured_start + timedelta(minutes=i), str(100 + i)) for i in range(3)]
    fixed_now = configured_start + timedelta(minutes=3, seconds=30)

    def fake_get(url, params):
        if url.endswith("/v5/market/time"):
            return {"result": {"timeSecond": str(int(fixed_now.timestamp()))}}
        start = params.get("start")
        end = params.get("end")
        candidates = [r for r in rows if (start is None or int(r[0]) >= start) and (end is None or int(r[0]) <= end)]
        return {"result": {"list": sorted(candidates, key=lambda r: int(r[0]), reverse=True)}}

    provider = BybitDemoMarketDataProvider(
        settings.bybit_base_url, settings.symbol, "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        initial_start=settings.market_data_initial_start,
    )

    delivered = [provider.next_candle().candle.open_time for _ in range(3)]
    assert delivered == [configured_start + timedelta(minutes=i) for i in range(3)]


# --- Required test 7: 17-candle backlog regression still passes ------------

def test_backlog_of_17_candles_regression_still_passes_with_configured_initial_start():
    """Regression guard: the v1.5 pagination fix (bounded start+end windows)
    must keep working correctly when `initial_start` comes from the real,
    normalized `Settings` field, not just a hand-built test datetime."""
    settings = Settings(market_data_initial_start="2024-06-01T12:00:00Z")
    configured_start = settings.market_data_initial_start

    rows = [_row(configured_start + timedelta(minutes=i), str(100 + i)) for i in range(17)]
    fixed_now = configured_start + timedelta(minutes=17, seconds=30)

    def fake_get(url, params):
        if url.endswith("/v5/market/time"):
            return {"result": {"timeSecond": str(int(fixed_now.timestamp()))}}
        start = params.get("start")
        end = params.get("end")
        candidates = [r for r in rows if (start is None or int(r[0]) >= start) and (end is None or int(r[0]) <= end)]
        return {"result": {"list": sorted(candidates, key=lambda r: int(r[0]), reverse=True)}}

    provider = BybitDemoMarketDataProvider(
        settings.bybit_base_url, settings.symbol, "1",
        http_get=fake_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=5, max_pages_per_poll=10, initial_start=settings.market_data_initial_start,
    )

    delivered = [provider.next_candle().candle.open_time for _ in range(17)]
    assert delivered == [configured_start + timedelta(minutes=i) for i in range(17)]
    assert provider.next_candle().status == CandleFetchStatus.NO_NEW_CANDLE
