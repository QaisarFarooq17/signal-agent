"""Synthetic candles for offline tests (trending random walk with regime changes)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_m15(days: int = 70, start_price: float = 4200.0, seed: int = 7, vol: float = 0.0012,
             end: str | None = None, weekdays_only: bool = False, drift: float = 0.0004) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = days * 96
    end_ts = pd.Timestamp(end or "2026-09-25 14:00", tz="UTC").floor("15min")
    idx = pd.date_range(end=end_ts, periods=n, freq="15min", tz="UTC")
    drift = np.repeat(rng.normal(0, drift, n // 192 + 1), 192)[:n] if drift else np.zeros(n)  # regime every 2 days
    rets = drift + rng.normal(0, vol, n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.normal(0, vol * 0.8, n)) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.8, n)) * close
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 0.0}, index=idx)
    if weekdays_only:
        df = df[df.index.dayofweek < 5]
    return df


def make_m15_path(days: int = 70, start_price: float = 4200.0, seed: int = 7, vol: float = 0.0012,
                  end: str | None = None) -> pd.DataFrame:
    """Driftless random walk built from 1-minute steps, so highs/lows are real path extremes."""
    rng = np.random.default_rng(seed)
    n = days * 96
    end_ts = pd.Timestamp(end or "2026-09-25 14:00", tz="UTC").floor("15min")
    idx = pd.date_range(end=end_ts, periods=n, freq="15min", tz="UTC")
    steps = rng.normal(0, vol / np.sqrt(15), n * 15).reshape(n, 15)
    logp = np.log(start_price) + np.cumsum(steps.ravel()).reshape(n, 15)
    prev_close = np.concatenate([[np.log(start_price)], logp[:-1, -1]])
    path = np.exp(np.column_stack([prev_close, logp]))
    return pd.DataFrame({"open": path[:, 0], "high": path.max(axis=1), "low": path.min(axis=1),
                         "close": path[:, -1], "volume": 0.0}, index=idx)


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return out.dropna()


def frames(days: int = 70, path: bool = False, **kw):
    # One long series; H4 needs ~3x more history than M15 for the EMA200 warm-up.
    long = make_m15_path(days * 3, **kw) if path else make_m15(days * 3, **kw)
    m15 = long.iloc[-days * 96:]
    return m15, resample(long, "1h"), resample(long, "4h")
