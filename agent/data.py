"""Candle data: Twelve Data (gold, BTC fallback) and Kraken (BTC, free, no key).

All frames are indexed by bar OPEN time (UTC) with columns open/high/low/close/volume.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from .config import Config, SymbolSpec

log = logging.getLogger(__name__)

TF_MINUTES = {"M15": 15, "H1": 60, "H4": 240}
TD_INTERVAL = {"M15": "15min", "H1": "1h", "H4": "4h"}
UA = {"User-Agent": "xau-btc-signal-agent/1.0 (+github actions)"}


class DataError(RuntimeError):
    pass


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    return s


def fetch_twelvedata(symbol: str, tf: str, n: int, api_key: str | None,
                     session: requests.Session | None = None) -> pd.DataFrame:
    if not api_key:
        raise DataError("TWELVEDATA_API_KEY is not set")
    session = session or _session()
    params = {"symbol": symbol, "interval": TD_INTERVAL[tf], "outputsize": min(int(n), 5000),
              "timezone": "UTC", "order": "asc", "apikey": api_key}
    for attempt in range(2):
        r = session.get("https://api.twelvedata.com/time_series", params=params, timeout=30)
        try:
            j = r.json()
        except ValueError as e:
            raise DataError(f"Twelve Data returned non-JSON (HTTP {r.status_code})") from e
        if j.get("status") == "error":
            code = j.get("code")
            if code == 429 and attempt == 0:  # per-minute credit limit: wait and retry once
                log.warning("Twelve Data rate limit hit, sleeping 62s")
                time.sleep(62)
                continue
            raise DataError(f"Twelve Data error {code}: {j.get('message')}")
        break
    values = j.get("values") or []
    if not values:
        raise DataError(f"Twelve Data returned no candles for {symbol} {tf}")
    df = pd.DataFrame(values)
    df["time"] = pd.to_datetime(df["datetime"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["volume"] = df["volume"].astype(float) if "volume" in df else 0.0
    df = df.set_index("time")[["open", "high", "low", "close", "volume"]].sort_index()
    return df[~df.index.duplicated(keep="last")]


def fetch_kraken(pair: str, tf: str, session: requests.Session | None = None) -> pd.DataFrame:
    session = session or _session()
    r = session.get("https://api.kraken.com/0/public/OHLC",
                    params={"pair": pair, "interval": TF_MINUTES[tf]}, timeout=30)
    try:
        j = r.json()
    except ValueError as e:
        raise DataError(f"Kraken returned non-JSON (HTTP {r.status_code})") from e
    if j.get("error"):
        raise DataError(f"Kraken error: {j['error']}")
    result = j.get("result") or {}
    rows = next((v for k, v in result.items() if k != "last"), None)
    if not rows:
        raise DataError(f"Kraken returned no candles for {pair} {tf}")
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "vwap", "volume", "count"])
    df["time"] = pd.to_datetime(df["t"].astype(int), unit="s", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df.set_index("time")[["open", "high", "low", "close", "volume"]].sort_index()


def split_closed(df: pd.DataFrame, tf: str, now: datetime) -> tuple[pd.DataFrame, float | None]:
    """Return (closed bars only, latest traded price incl. the forming bar)."""
    if df.empty:
        return df, None
    last_price = float(df["close"].iloc[-1])
    closed = df[df.index + timedelta(minutes=TF_MINUTES[tf]) <= now]
    return closed, last_price


def apply_offset(df: pd.DataFrame, offset: float) -> pd.DataFrame:
    if not offset:
        return df
    df = df.copy()
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] + offset
    return df


# Bars fetched per timeframe for live analysis (long H4 history so EMA200 is fully warmed up).
LIVE_BARS = {"M15": 400, "H1": 500, "H4": 1000}  # 1 API credit each regardless of size


def get_live_candles(spec: SymbolSpec, cfg: Config, now: datetime | None = None,
                     session: requests.Session | None = None) -> dict:
    """Fetch M15/H1/H4 for a symbol. Returns {'M15': df, 'H1': df, 'H4': df, 'price': float, 'source': str}."""
    now = now or datetime.now(timezone.utc)
    session = session or _session()
    out: dict = {}
    sources = []
    for tf in ("M15", "H1", "H4"):
        df = None
        errors = []
        if spec.kraken_pair:
            try:
                df = fetch_kraken(spec.kraken_pair, tf, session)
                sources.append("Kraken")
            except Exception as e:  # noqa: BLE001 - fall through to next provider
                errors.append(str(e))
        if df is None:
            try:
                df = fetch_twelvedata(spec.td_symbol, tf, LIVE_BARS[tf], cfg.twelvedata_key, session)
                sources.append("TwelveData")
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
        if df is None:
            raise DataError(f"{spec.key} {tf}: " + " | ".join(errors))
        df = apply_offset(df, spec.price_offset)
        closed, last = split_closed(df, tf, now)
        out[tf] = closed
        if tf == "M15":
            out["price"] = last
    out["source"] = "/".join(sorted(set(sources)))
    return out


def get_history(spec: SymbolSpec, cfg: Config, session: requests.Session | None = None) -> dict:
    """Longest free history for backtests (Twelve Data, max 5000 bars per timeframe)."""
    session = session or _session()
    now = datetime.now(timezone.utc)
    out = {}
    for tf, n in (("M15", 5000), ("H1", 5000), ("H4", 2000)):
        df = fetch_twelvedata(spec.td_symbol, tf, n, cfg.twelvedata_key, session)
        df = apply_offset(df, spec.price_offset)
        out[tf], _ = split_closed(df, tf, now)
    return out
