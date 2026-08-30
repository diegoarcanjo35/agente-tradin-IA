import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

DB = "agente_trader_paper_live.db"
SYMBOL = "BTCUSDT"
TOLERANCE_SECONDS = 120
MAX_WAIT_SECONDS = 60 * 60
POLL_EVERY = 30


def bybit_server_time():
    with urllib.request.urlopen("https://api-demo.bybit.com/v5/market/time", timeout=10) as r:
        data = json.load(r)
    return int(data["result"]["timeSecond"])


def last_closed_kline_open():
    url = (
        "https://api-demo.bybit.com/v5/market/kline"
        f"?category=linear&symbol={SYMBOL}&interval=1&limit=2"
    )
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.load(r)
    rows = data["result"]["list"]
    return int(rows[1][0]) / 1000, int(rows[0][0]) / 1000


def max_persisted_open():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*), MAX(open_time) FROM candles WHERE symbol=?", (SYMBOL,))
    count, max_open = cur.fetchone()
    con.close()
    if max_open is None:
        return count, None
    dt = datetime.strptime(max_open, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    return count, dt.timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


start = time.time()
while True:
    server_time = bybit_server_time()
    last_closed_open, forming_open = last_closed_kline_open()
    last_closed_close = forming_open
    count, persisted_open = max_persisted_open()

    age = server_time - persisted_open if persisted_open is not None else None
    gap_vs_last_closed = last_closed_close - persisted_open if persisted_open is not None else None

    elapsed = time.time() - start
    print(json.dumps({
        "elapsed_s": round(elapsed, 1),
        "server_time_utc": iso(server_time),
        "last_closed_candle_close_utc": iso(last_closed_close),
        "persisted_max_open_utc": iso(persisted_open) if persisted_open else None,
        "persisted_count": count,
        "gap_vs_last_closed_seconds": gap_vs_last_closed,
    }))

    if gap_vs_last_closed is not None and gap_vs_last_closed <= TOLERANCE_SECONDS:
        print("RESULT=OK")
        break
    if elapsed >= MAX_WAIT_SECONDS:
        print("RESULT=TIMEOUT")
        break
    time.sleep(POLL_EVERY)
