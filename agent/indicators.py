"""Technical indicators. Every value at bar i uses only bars <= i (no look-ahead)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = wilder(d.clip(lower=0), n)
    dn = wilder((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    val = 100 - 100 / (1 + rs)
    val = val.where(dn != 0, 100.0)                  # only gains -> 100
    return val.where(~((up == 0) & (dn == 0)), 50.0)  # flat -> 50


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.fillna(df["high"] - df["low"])


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    line = ema(close, fast) - ema(close, slow)
    signal = ema(line, sig)
    return line, signal, line - signal


def adx(df: pd.DataFrame, n: int = 14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = wilder(true_range(df), n)
    pdi = 100 * wilder(pdm, n) / tr.replace(0, np.nan)
    mdi = 100 * wilder(mdm, n) / tr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return wilder(dx.fillna(0), n), pdi.fillna(0), mdi.fillna(0)


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0):
    """Returns (line, direction) where direction is +1 (up) or -1 (down)."""
    a = atr(df, n).to_numpy()
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * a, hl2 - mult * a
    fub, flb = ub.copy(), lb.copy()
    direction = np.ones(len(df), dtype=int)
    line = np.zeros(len(df))
    for i in range(len(df)):
        if i == 0:
            line[i] = flb[i]
            continue
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if c[i] < flb[i] else 1
        else:
            direction[i] = 1 if c[i] > fub[i] else -1
        line[i] = flb[i] if direction[i] == 1 else fub[i]
    return pd.Series(line, index=df.index), pd.Series(direction, index=df.index)


def bb_width(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return (2 * k * sd) / mid


def candle_patterns(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    po, pc = o.shift(1), c.shift(1)
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper = h - pd.concat([o, c], axis=1).max(axis=1)
    lower = pd.concat([o, c], axis=1).min(axis=1) - l
    out = pd.DataFrame(index=df.index)
    out["bull_engulf"] = (c > o) & (pc < po) & (c >= po) & (o <= pc) & (body > (pc - po).abs())
    out["bear_engulf"] = (c < o) & (pc > po) & (c <= po) & (o >= pc) & (body > (pc - po).abs())
    out["bull_pin"] = (lower >= 2 * body) & (lower >= 0.55 * rng) & (upper <= 0.25 * rng)
    out["bear_pin"] = (upper >= 2 * body) & (upper >= 0.55 * rng) & (lower <= 0.25 * rng)
    return out.fillna(False).astype(bool)


def swing_events(df: pd.DataFrame, k: int = 3):
    """Fractal swing highs/lows, reported at the bar where they become CONFIRMED (k bars later).

    Returns two arrays of (confirm_position, price): highs, lows.
    """
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    highs, lows = [], []
    for i in range(2 * k, len(df)):
        c = i - k
        win_h = h[c - k:i + 1]
        win_l = l[c - k:i + 1]
        if h[c] == win_h.max() and (win_h == h[c]).sum() == 1:
            highs.append((i, h[c]))
        if l[c] == win_l.min() and (win_l == l[c]).sum() == 1:
            lows.append((i, l[c]))
    return highs, lows


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns used by the strategy."""
    d = df.copy()
    c = d["close"]
    d["ema20"], d["ema50"], d["ema200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    d["rsi"] = rsi(c, 14)
    d["rsi2"] = rsi(c, 2)
    d["atr"] = atr(d, 14)
    d["macd"], d["macd_sig"], d["macd_hist"] = macd(c)
    d["adx"], d["pdi"], d["mdi"] = adx(d, 14)
    d["st_line"], d["st_dir"] = supertrend(d, 10, 3.0)
    d["bbw"] = bb_width(c)
    d["ema50_slope"] = d["ema50"] - d["ema50"].shift(5)
    d = d.join(candle_patterns(d))
    return d
