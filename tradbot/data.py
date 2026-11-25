"""Downloads and caches Binance candles (public data, no API key needed)."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from .config import DATA_DIR
from .log import get_logger

log = get_logger("data")

# Tried in order. data-api.binance.vision is Binance's official public
# market-data mirror; it works in places where api.binance.com is blocked.
HOSTS = [
    "https://api.binance.com",
    "https://data-api.binance.vision",
    "https://api1.binance.com",
    "https://api2.binance.com",
]
TF_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
         "4h": 14_400_000, "1d": 86_400_000}
CANDLE_DIR = DATA_DIR / "candles"
COLUMNS = ["time", "open", "high", "low", "close", "volume"]

_session = requests.Session()
_session.headers["User-Agent"] = "tradbot/1.0"
_good_host: str | None = None


class DataError(RuntimeError):
    pass


def _get(path: str, params: dict, retries: int = 4):
    """GET with host fallback and retries. Returns parsed JSON."""
    global _good_host
    hosts = ([_good_host] if _good_host else []) + [h for h in HOSTS if h != _good_host]
    last_err = None
    for attempt in range(retries):
        for host in hosts:
            try:
                r = _session.get(host + path, params=params, timeout=15)
                if r.status_code == 200:
                    _good_host = host
                    return r.json()
                if r.status_code in (418, 429):   # rate limited - wait
                    wait = int(r.headers.get("Retry-After", "10"))
                    log.warning("Binance rate limit hit, waiting %ss", wait)
                    time.sleep(min(wait, 60))
                    continue
                last_err = f"{host} answered HTTP {r.status_code}"
                if r.status_code == 451:
                    last_err += " (blocked in this location)"
            except requests.RequestException as e:
                last_err = f"{host}: {type(e).__name__}"
        time.sleep(2 * (attempt + 1))
    raise DataError(f"Could not reach Binance. Last error: {last_err}. "
                    "Check your internet connection.")


def to_binance_symbol(symbol: str) -> str:
    return symbol.replace("/", "").upper()


def fetch_klines(symbol: str, timeframe: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
    """Downloads candles from start_ms until now (or end_ms), 1000 at a time."""
    if timeframe not in TF_MS:
        raise ValueError(f"Unsupported timeframe {timeframe}. Use one of {list(TF_MS)}")
    rows = []
    cursor = start_ms
    end_ms = end_ms or int(time.time() * 1000)
    while cursor < end_ms:
        batch = _get("/api/v3/klines", {
            "symbol": to_binance_symbol(symbol), "interval": timeframe,
            "startTime": cursor, "limit": 1000,
        })
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + TF_MS[timeframe]
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 1000:
            break
        time.sleep(0.15)   # be polite to the API
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame([r[:6] for r in rows], columns=COLUMNS)
    df["time"] = df["time"].astype("int64")
    for c in COLUMNS[1:]:
        df[c] = df[c].astype(float)
    return df


def drop_unclosed(df: pd.DataFrame, timeframe: str, now_ms: int | None = None) -> pd.DataFrame:
    """Removes the still-forming candle so decisions only use CLOSED candles."""
    now_ms = now_ms or int(time.time() * 1000)
    return df[df["time"] + TF_MS[timeframe] <= now_ms].reset_index(drop=True)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    bad = (df[["open", "high", "low", "close"]] <= 0).any(axis=1) | (df["high"] < df["low"])
    if bad.any():
        log.warning("Removed %d broken candles", int(bad.sum()))
        df = df[~bad].reset_index(drop=True)
    return df


def cache_path(symbol: str, timeframe: str) -> Path:
    return CANDLE_DIR / f"{to_binance_symbol(symbol)}_{timeframe}.csv"


def load_cached(symbol: str, timeframe: str) -> pd.DataFrame:
    p = cache_path(symbol, timeframe)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p)


def update_history(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Downloads missing candles (only what's new) and saves to the cache."""
    CANDLE_DIR.mkdir(parents=True, exist_ok=True)
    df = load_cached(symbol, timeframe)
    now_ms = int(time.time() * 1000)
    want_start = now_ms - days * 86_400_000
    if df.empty or df["time"].iloc[0] > want_start + TF_MS[timeframe] * 24:
        log.info("Downloading %d days of %s %s candles (first time can take a minute)...",
                 days, symbol, timeframe)
        df = fetch_klines(symbol, timeframe, want_start)
    else:
        start = int(df["time"].iloc[-1]) + TF_MS[timeframe]
        if start < now_ms - TF_MS[timeframe]:
            new = fetch_klines(symbol, timeframe, start)
            df = pd.concat([df, new], ignore_index=True)
    df = clean(drop_unclosed(df, timeframe, now_ms))
    df = df[df["time"] >= want_start].reset_index(drop=True)
    df.to_csv(cache_path(symbol, timeframe), index=False)
    return df


def fetch_recent(symbol: str, timeframe: str, bars: int = 400,
                 include_open: bool = False) -> pd.DataFrame:
    """Latest candles for the live loop. Decisions use CLOSED candles only;
    include_open=True adds the still-forming candle (live price / preview)."""
    start = int(time.time() * 1000) - (bars + 2) * TF_MS[timeframe]
    df = fetch_klines(symbol, timeframe, start)
    return clean(df if include_open else drop_unclosed(df, timeframe))


def refresh_recent(cache: pd.DataFrame | None, symbol: str, timeframe: str,
                   bars: int = 300) -> pd.DataFrame:
    """Keeps a window of the latest candles (including the one still forming) up to date
    with tiny downloads: the first call gets `bars` candles, later calls only the newest
    1-2 candles. Saves ~99% of internet data compared with downloading everything again."""
    step = TF_MS[timeframe]
    now = int(time.time() * 1000)
    if cache is None or cache.empty or now - int(cache["time"].iloc[-1]) > bars * step // 2:
        df = fetch_klines(symbol, timeframe, now - (bars + 2) * step)
    else:
        # from the last cached candle on: it may have been unfinished, so get it again
        new = fetch_klines(symbol, timeframe, int(cache["time"].iloc[-1]))
        df = pd.concat([cache, new], ignore_index=True).drop_duplicates("time", keep="last")
    return clean(df).tail(bars).reset_index(drop=True)


def fetch_price(symbol: str) -> float:
    data = _get("/api/v3/ticker/price", {"symbol": to_binance_symbol(symbol)})
    return float(data["price"])


# ------------------------------------------------------------ demo data
def synthetic_candles(bars: int = 8000, seed: int = 0, start_price: float = 30000.0,
                      timeframe: str = "1h") -> pd.DataFrame:
    """Fake but realistic-looking prices for offline tests and demos.
    Includes trending and sideways periods so strategies can be tested."""
    rng = np.random.default_rng(seed)
    regime_len = 600
    drift = np.repeat(rng.normal(0, 0.0006, bars // regime_len + 1), regime_len)[:bars]
    vol = np.repeat(rng.uniform(0.004, 0.012, bars // regime_len + 1), regime_len)[:bars]
    # small autocorrelation (momentum) so there is something to learn
    noise = rng.normal(0, 1, bars) * vol
    ret = np.zeros(bars)
    for i in range(1, bars):
        ret[i] = drift[i] + 0.08 * ret[i - 1] + noise[i]
    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(rng.normal(0, 1, bars)) * vol * close * 0.6
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.lognormal(3, 0.5, bars) * (1 + 20 * np.abs(ret))
    t0 = 1_600_000_000_000 - (1_600_000_000_000 % TF_MS[timeframe])
    times = t0 + np.arange(bars, dtype="int64") * TF_MS[timeframe]
    return pd.DataFrame({"time": times, "open": open_, "high": high, "low": low,
                         "close": close, "volume": volume})
