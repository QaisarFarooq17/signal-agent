"""Multi-timeframe confluence strategy.

Score out of 100 for each side (BUY / SELL):
  H4 bias        up to 25  (EMA50 vs EMA200, EMA50 slope, Supertrend)
  H1 trend       up to 30  (EMA20/50, MACD histogram, ADX + DI)
  M15 trigger    up to 30  (pullback-to-EMA resume OR range breakout, + candle pattern)
  News bias      -10..+10  (Claude-scored headlines, decaying)
  Room to level  -5..+5    (distance to the next support/resistance in R)
  Penalties      overextension (-15), stretched H1 RSI (-10)
A trigger is REQUIRED. Hard vetoes: calendar blackout, rollover window, dead or spiking
volatility, or an opposing key level closer than 1R.

The same `evaluate_at` function is used live and in the backtest, so backtest numbers
describe exactly the logic that sends signals.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from .config import Config, SymbolSpec
from .indicators import enrich, swing_events
from .variants import V1, Params

H1_COLS = ["close", "ema20", "ema50", "rsi", "atr", "macd_hist", "adx", "pdi", "mdi", "st_dir"]
H4_COLS = ["close", "ema50", "ema200", "ema50_slope", "st_dir", "rsi", "atr"]


@dataclass
class Context:
    spec: SymbolSpec
    m: pd.DataFrame                      # enriched M15 + merged h1_/h4_ columns + daily levels
    A: dict                              # column -> numpy array (fast access)
    ct_ns: np.ndarray                    # M15 bar close times (int ns)
    swing_ct: list = field(default_factory=list)    # H1 swing confirm close times (int ns), sorted
    swing_px: list = field(default_factory=list)
    swing_kind: list = field(default_factory=list)  # +1 swing high, -1 swing low


def _ns(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(idx).tz_convert("UTC").as_unit("ns")


def build_context(m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, spec: SymbolSpec) -> Context:
    m15, h1, h4 = m15.copy(), h1.copy(), h4.copy()
    m15.index, h1.index, h4.index = _ns(m15.index), _ns(h1.index), _ns(h4.index)
    m, h, f = enrich(m15), enrich(h1), enrich(h4)

    hh = h[H1_COLS].copy()
    hh["macd_hist_prev"] = h["macd_hist"].shift(1)
    hh = hh.add_prefix("h1_")
    hh["ct"] = h.index + pd.Timedelta(minutes=60)
    ff = f[H4_COLS].add_prefix("h4_")
    ff["ct"] = f.index + pd.Timedelta(minutes=240)

    mm = m.copy()
    mm["ct"] = m.index + pd.Timedelta(minutes=15)
    mm = mm.reset_index().rename(columns={"index": "time"})
    if "time" not in mm.columns:
        mm = mm.rename(columns={mm.columns[0]: "time"})
    # Attach the most recent CLOSED H1/H4 bar to every M15 bar (no look-ahead).
    mm = pd.merge_asof(mm.sort_values("ct"), hh.sort_values("ct"), on="ct", direction="backward")
    mm = pd.merge_asof(mm, ff.sort_values("ct"), on="ct", direction="backward")
    mm = mm.set_index("time")

    # Previous complete trading day (UTC, like Exness server time). Skip stub days (<40 M15 bars).
    day = mm.index.floor("D")
    daily = m.groupby(m.index.floor("D")).agg(high=("high", "max"), low=("low", "min"),
                                              close=("close", "last"), n=("close", "size"))
    valid = daily[daily["n"] >= 40]
    vdays = valid.index.as_unit("ns").asi8
    pos = np.searchsorted(vdays, day.as_unit("ns").asi8, side="left") - 1
    for col in ("high", "low", "close"):
        arr = valid[col].to_numpy()
        mm["pd_" + col] = np.where(pos >= 0, arr[np.clip(pos, 0, None)] if len(arr) else np.nan, np.nan)
    P = (mm["pd_high"] + mm["pd_low"] + mm["pd_close"]) / 3
    mm["piv_p"] = P
    mm["piv_r1"], mm["piv_s1"] = 2 * P - mm["pd_low"], 2 * P - mm["pd_high"]
    mm["piv_r2"], mm["piv_s2"] = P + (mm["pd_high"] - mm["pd_low"]), P - (mm["pd_high"] - mm["pd_low"])

    # Asian session range (00:00-07:00 UTC) of the same day, known only from 07:00 UTC on.
    hours = mm.index.hour
    asia = mm[hours < 7]
    g = asia.groupby(asia.index.floor("D")).agg(hi=("high", "max"), lo=("low", "min"), n=("high", "size"))
    g = g[g["n"] >= 20]
    after = hours >= 7
    mm["asia_hi"] = np.where(after, g["hi"].reindex(day).to_numpy(), np.nan)
    mm["asia_lo"] = np.where(after, g["lo"].reindex(day).to_numpy(), np.nan)

    A = {c: mm[c].to_numpy() for c in mm.columns if c != "ct"}
    A["hour"] = np.asarray(hours)
    ctx = Context(spec=spec, m=mm, A=A, ct_ns=pd.DatetimeIndex(mm["ct"]).as_unit("ns").asi8)

    highs, lows = swing_events(h, k=3)
    h_ct = (h.index + pd.Timedelta(minutes=60)).asi8
    ev = [(int(h_ct[i]), float(px), 1) for i, px in highs] + [(int(h_ct[i]), float(px), -1) for i, px in lows]
    ev.sort()
    ctx.swing_ct = [e[0] for e in ev]
    ctx.swing_px = [e[1] for e in ev]
    ctx.swing_kind = [e[2] for e in ev]
    return ctx


# ----------------------------------------------------------------------------- scoring

@dataclass
class SideEval:
    side: str                 # "BUY" / "SELL"
    trend_pts: int = 0
    score: int = 0
    setup: str | None = None
    reasons: list = field(default_factory=list)
    veto: str | None = None
    plan: dict | None = None  # entry/sl/tp when a setup exists


@dataclass
class Evaluation:
    symbol: str
    bar_time: str
    price: float
    atr: float
    buy: SideEval
    sell: SideEval
    signal: dict | None = None
    global_veto: str | None = None


def _ok(*vals) -> bool:
    return all(v is not None and not (isinstance(v, float) and math.isnan(v)) for v in vals)


def _trend_points(A, i, s):
    pts, why = 0, []
    up = s > 0
    c4, e50, e200, sl4, st4 = (A["h4_close"][i], A["h4_ema50"][i], A["h4_ema200"][i],
                               A["h4_ema50_slope"][i], A["h4_st_dir"][i])
    if _ok(c4, e50, e200):
        if s * (c4 - e50) > 0 and s * (e50 - e200) > 0:
            pts += 15
            why.append(f"H4 {'up' if up else 'down'}trend (price {'>' if up else '<'} EMA50 {'>' if up else '<'} EMA200)")
        elif s * (c4 - e200) > 0:
            pts += 7
            why.append(f"H4 {'above' if up else 'below'} EMA200")
    if _ok(sl4) and s * sl4 > 0:
        pts += 5
        why.append(f"H4 EMA50 {'rising' if up else 'falling'}")
    if _ok(st4) and int(st4) == s:
        pts += 5
        why.append(f"H4 Supertrend {'up' if up else 'down'}")

    e20h, e50h, ch = A["h1_ema20"][i], A["h1_ema50"][i], A["h1_close"][i]
    if _ok(e20h, e50h, ch) and s * (e20h - e50h) > 0 and s * (ch - e50h) > 0:
        pts += 12
        why.append(f"H1 {'up' if up else 'down'}trend (EMA20 {'>' if up else '<'} EMA50)")
    mh, mhp = A["h1_macd_hist"][i], A["h1_macd_hist_prev"][i]
    if _ok(mh) and s * mh > 0:
        pts += 6
        if _ok(mhp) and s * (mh - mhp) > 0:
            pts += 2
            why.append("H1 MACD momentum building")
        else:
            why.append(f"H1 MACD {'positive' if up else 'negative'}")
    adx, pdi, mdi = A["h1_adx"][i], A["h1_pdi"][i], A["h1_mdi"][i]
    if _ok(adx, pdi, mdi) and s * (pdi - mdi) > 0:
        if adx >= 25:
            pts += 10
            why.append(f"H1 ADX {adx:.0f} (strong trend)")
        elif adx >= 20:
            pts += 6
            why.append(f"H1 ADX {adx:.0f}")
    return pts, why


def _trigger(A, i, s, setups=("pullback", "breakout")):
    if i < 25:
        return 0, None, []
    c, o, h, l = A["close"], A["open"], A["high"], A["low"]
    e20, e50, r, at = A["ema20"], A["ema50"], A["rsi"], A["atr"]
    pts, setup, why = 0, None, []
    if "pullback" in setups:
        rng = range(i - 6, i + 1)
        if s > 0:
            touched = any(l[j] <= e20[j] + 0.25 * at[j] for j in rng)
            intact = all(c[j] >= e50[j] - 0.5 * at[j] for j in rng)
            resume = c[i] > e20[i] and c[i] > o[i] and 40 <= r[i] <= 68 and r[i] > r[i - 1]
        else:
            touched = any(h[j] >= e20[j] - 0.25 * at[j] for j in rng)
            intact = all(c[j] <= e50[j] + 0.5 * at[j] for j in rng)
            resume = c[i] < e20[i] and c[i] < o[i] and 32 <= r[i] <= 60 and r[i] < r[i - 1]
        if touched and intact and resume:
            pts, setup = 20, "pullback"
            why.append(f"M15 pullback into EMA20/50 zone, momentum resuming (RSI {r[i]:.0f})")
    if not setup and "breakout" in setups:
        hi20, lo20 = float(np.max(h[i - 20:i])), float(np.min(l[i - 20:i]))
        wide = (h[i] - l[i]) >= 1.2 * at[i]
        expanding = _ok(A["bbw"][i], A["bbw"][i - 5]) and A["bbw"][i] > A["bbw"][i - 5]
        if s > 0 and c[i] > hi20 and wide and expanding and r[i] < 75:
            pts, setup = 18, "breakout"
            why.append(f"M15 breakout above 5h range high {hi20:.2f} on expanding volatility")
        elif s < 0 and c[i] < lo20 and wide and expanding and r[i] > 25:
            pts, setup = 18, "breakout"
            why.append(f"M15 breakdown below 5h range low {lo20:.2f} on expanding volatility")
    if not setup and "rsi2" in setups:
        r2, e200 = A["rsi2"], A["ema200"]
        if s > 0 and r2[i - 1] < 10 <= r2[i] and c[i] > o[i] and c[i] > e200[i]:
            pts, setup = 20, "rsi2"
            why.append("RSI(2) bounced from oversold (<10) above the M15 EMA200")
        elif s < 0 and r2[i - 1] > 90 >= r2[i] and c[i] < o[i] and c[i] < e200[i]:
            pts, setup = 20, "rsi2"
            why.append("RSI(2) turned down from overbought (>90) below the M15 EMA200")
    if not setup and "london" in setups:
        ahi, alo, hr = A["asia_hi"][i], A["asia_lo"][i], A["hour"][i]
        if _ok(ahi, alo) and 7 <= hr < 11 and at[i] > 0 and 1.0 <= (ahi - alo) / at[i] <= 8.0:
            if s > 0 and c[i] > ahi and c[i - 1] <= ahi:
                pts, setup = 20, "london"
                why.append(f"London session broke above the Asian range high {ahi:.2f}")
            elif s < 0 and c[i] < alo and c[i - 1] >= alo:
                pts, setup = 20, "london"
                why.append(f"London session broke below the Asian range low {alo:.2f}")
    if setup:
        if s > 0 and (A["bull_engulf"][i] or A["bull_pin"][i]):
            pts += 7
            why.append("bullish " + ("engulfing" if A["bull_engulf"][i] else "pin bar") + " candle")
        if s < 0 and (A["bear_engulf"][i] or A["bear_pin"][i]):
            pts += 7
            why.append("bearish " + ("engulfing" if A["bear_engulf"][i] else "pin bar") + " candle")
        if s * (r[i] - 50) > 0:
            pts += 3
    return min(pts, 30), setup, why


def key_levels(ctx: Context, i: int, days: int = 5) -> list[tuple[str, float]]:
    A = ctx.A
    lv = []
    for name, col in (("prev-day high", "pd_high"), ("prev-day low", "pd_low"), ("pivot", "piv_p"),
                      ("R1", "piv_r1"), ("S1", "piv_s1"), ("R2", "piv_r2"), ("S2", "piv_s2")):
        v = A[col][i]
        if _ok(v):
            lv.append((name, float(v)))
    t = int(ctx.ct_ns[i])
    lo_t = t - days * 86_400 * 10**9
    a, b = bisect.bisect_left(ctx.swing_ct, lo_t), bisect.bisect_right(ctx.swing_ct, t)
    for k in range(a, b):
        lv.append(("H1 swing high" if ctx.swing_kind[k] > 0 else "H1 swing low", ctx.swing_px[k]))
    return lv


def _atr_percentile(A, i, look=200):
    lo = max(0, i - look)
    window = A["atr"][lo:i + 1]
    window = window[~np.isnan(window)]
    if len(window) < 50:
        return 0.5
    return float((window < A["atr"][i]).mean())


def _in_window(t: pd.Timestamp, start: str, end: str) -> bool:
    hm = t.hour * 60 + t.minute
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    return s <= hm < e if s <= e else (hm >= s or hm < e)


def position_size(spec: SymbolSpec, cfg: Config, sl_dist: float):
    target = cfg.account_balance * cfg.risk_pct / 100.0
    per_lot = sl_dist * spec.contract_size
    lots = math.floor(target / per_lot / spec.lot_step + 1e-9) * spec.lot_step if per_lot > 0 else 0
    lots = max(round(lots, 2), spec.min_lot)
    risk = lots * per_lot
    return lots, risk, (risk / cfg.account_balance * 100.0 if cfg.account_balance else 0.0)


def _side(ctx, i, s, cfg, news_bias, params: Params = V1) -> SideEval:
    A, spec = ctx.A, ctx.spec
    ev = SideEval(side="BUY" if s > 0 else "SELL")
    tpts, twhy = _trend_points(A, i, s)
    ev.trend_pts = tpts
    trig, setup, gwhy = _trigger(A, i, s, params.setups)
    ev.setup = setup
    pts = tpts + trig
    why = twhy + gwhy

    ext = s * (A["h1_close"][i] - A["h1_ema20"][i]) / A["h1_atr"][i] if _ok(A["h1_atr"][i]) and A["h1_atr"][i] > 0 else 0
    if ext > 2.5:
        pts -= 15
        why.append(f"⚠ H1 overextended ({ext:.1f} ATR from EMA20)")
    h1r = A["h1_rsi"][i]
    if _ok(h1r) and ((s > 0 and h1r > 75) or (s < 0 and h1r < 25)):
        pts -= 10
        why.append(f"⚠ H1 RSI {h1r:.0f} stretched")
    if news_bias:
        npts = int(round(max(-10, min(10, 10 * s * news_bias))))
        if npts:
            pts += npts
            why.append(f"news flow {'supports' if npts > 0 else 'is against'} this side ({news_bias:+.2f})")

    if setup:
        atr_i = float(A["atr"][i])
        entry = float(A["close"][i])
        if s > 0:
            struct = float(np.min(A["low"][i - 10:i + 1]))
            raw = entry - struct + 0.3 * atr_i + spec.spread
        else:
            struct = float(np.max(A["high"][i - 10:i + 1]))
            raw = struct - entry + 0.3 * atr_i + spec.spread
        sl_dist = min(max(raw, params.sl_min_atr * atr_i), params.sl_max_atr * atr_i)
        if raw > params.sl_max_atr * atr_i:
            why.append(f"stop capped at {params.sl_max_atr:g}×ATR (structure is far)")
        sl = entry - s * sl_dist
        tp1 = entry + s * params.tp1_r * sl_dist
        tp2 = entry + s * params.tp2_r * sl_dist
        opp = [(n, p) for n, p in key_levels(ctx, i) if s * (p - entry) > 0.2 * atr_i]
        nearest = min(opp, key=lambda x: abs(x[1] - entry)) if opp else None
        room = abs(nearest[1] - entry) / sl_dist if nearest else float("inf")
        if nearest is None or room >= 2.0:
            pts += 5
            if nearest:
                why.append(f"clear room: next level {nearest[0]} {nearest[1]:.{spec.decimals}f} ({room:.1f}R)")
            if nearest and room < params.tp2_r and params.partial < 1.0:
                tp2 = nearest[1] - s * 0.1 * atr_i
                why.append(f"TP2 placed just before {nearest[0]}")
        elif room >= 1.5:
            pts += 2
            why.append(f"{nearest[0]} {nearest[1]:.{spec.decimals}f} at {room:.1f}R")
        elif room >= 1.0:
            pts -= 5
            why.append(f"⚠ {nearest[0]} {nearest[1]:.{spec.decimals}f} only {room:.1f}R away")
        else:
            ev.veto = f"{nearest[0]} {nearest[1]:.{spec.decimals}f} blocks the move (only {room:.1f}R away)"
        ev.plan = {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "sl_dist": sl_dist, "atr": atr_i,
                   "room_r": None if room == float("inf") else round(room, 2),
                   "nearest": nearest}
    ev.score = int(max(0, min(100, pts)))
    ev.reasons = why
    return ev


def grade(score: int) -> str:
    return "A" if score >= 80 else "B"


def _rule_block(A, i, s, params: Params, close_t) -> str | None:
    """Hard filters of the chosen rule set. Returns a reason when the side is not allowed."""
    if params.sides == "buy" and s < 0:
        return "rule set trades BUY only"
    if params.sides == "sell" and s > 0:
        return "rule set trades SELL only"
    if params.require_h4:
        c4, e50, e200 = A["h4_close"][i], A["h4_ema50"][i], A["h4_ema200"][i]
        if not (_ok(c4, e50, e200) and s * (c4 - e50) > 0 and s * (e50 - e200) > 0):
            return "H4 trend not aligned"
    if params.require_h1:
        c1, e20, e50 = A["h1_close"][i], A["h1_ema20"][i], A["h1_ema50"][i]
        if not (_ok(c1, e20, e50) and s * (e20 - e50) > 0 and s * (c1 - e50) > 0):
            return "H1 trend not aligned"
    if params.adx_min and not (_ok(A["h1_adx"][i]) and A["h1_adx"][i] >= params.adx_min):
        return f"H1 ADX below {params.adx_min:g}"
    if params.sessions_utc and not any(_in_window(close_t, a, b) for a, b in params.sessions_utc):
        return "outside the rule set's trading hours"
    return None


def evaluate_at(ctx: Context, i: int, cfg: Config, news_bias: float = 0.0,
                blackout: str | None = None, params: Params = V1) -> Evaluation:
    A, spec = ctx.A, ctx.spec
    t = ctx.m.index[i]
    ev = Evaluation(symbol=spec.key, bar_time=t.isoformat(), price=float(A["close"][i]), atr=float(A["atr"][i]),
                    buy=_side(ctx, i, +1, cfg, news_bias, params), sell=_side(ctx, i, -1, cfg, news_bias, params))
    close_t = t + timedelta(minutes=15)
    veto = blackout
    for a, b in spec.blackouts_utc:
        if _in_window(close_t, a, b):
            veto = veto or f"daily rollover window {a}–{b} UTC (wide spreads)"
    pct = _atr_percentile(A, i)
    if pct < 0.10:
        veto = veto or "market too quiet (ATR in bottom 10%)"
    elif pct > 0.98:
        veto = veto or "volatility spike (ATR in top 2%) — let it settle"
    ev.global_veto = veto

    min_score = cfg.min_score if params.min_score is None else params.min_score
    candidates = []
    for se, s in ((ev.buy, 1), (ev.sell, -1)):
        if s < 0 and not cfg.allow_sells:
            continue
        if not (se.setup and se.plan and not se.veto and not veto and se.score >= min_score):
            continue
        block = _rule_block(A, i, s, params, close_t)
        if block:
            se.veto = se.veto or block
            continue
        candidates.append((se.score, s, se))
    if candidates:
        _, s, se = max(candidates, key=lambda x: x[0])
        p = se.plan
        lots, risk, risk_pct = position_size(spec, cfg, p["sl_dist"])
        ev.signal = {
            "symbol": spec.key, "side": se.side, "bar_time": ev.bar_time,
            "entry": round(p["entry"], spec.decimals), "sl": round(p["sl"], spec.decimals),
            "tp1": round(p["tp1"], spec.decimals), "tp2": round(p["tp2"], spec.decimals),
            "sl_dist": p["sl_dist"], "atr": p["atr"], "score": se.score, "grade": grade(se.score),
            "setup": se.setup, "reasons": se.reasons, "lots": lots, "risk_usd": round(risk, 2),
            "risk_pct": round(risk_pct, 2), "room_r": p["room_r"],
            "nearest": list(p["nearest"]) if p["nearest"] else None,
            "strategy": params.name, "partial": params.partial, "be": params.be_after_tp1,
        }
    return ev


# ----------------------------------------------------------------------------- summaries

def trend_label(A, i, prefix):
    c, e_fast, e_slow = A[f"{prefix}close"][i], A[f"{prefix}ema50" if prefix == "h4_" else f"{prefix}ema20"][i], \
        A[f"{prefix}ema200" if prefix == "h4_" else f"{prefix}ema50"][i]
    if not _ok(c, e_fast, e_slow):
        return "n/a"
    if c > e_fast > e_slow:
        return "UP"
    if c < e_fast < e_slow:
        return "DOWN"
    return "MIXED"


def summarize(ctx: Context, i: int, ev: Evaluation, live_price: float | None = None) -> dict:
    A, spec = ctx.A, ctx.spec
    price = live_price if live_price is not None else ev.price
    lv = key_levels(ctx, i)
    gap = 0.5 * ev.atr  # merge levels closer than half an M15 ATR

    def distinct(levels):
        out = []
        for n, p in levels:
            if not out or abs(p - out[-1][1]) > gap:
                out.append((n, p))
        return out[:3]
    above = distinct(sorted([x for x in lv if x[1] > price], key=lambda x: x[1]))
    below = distinct(sorted([x for x in lv if x[1] < price], key=lambda x: -x[1]))

    def side_view(se: SideEval):
        return {"score": se.score, "trend_pts": se.trend_pts, "setup": se.setup, "veto": se.veto}

    return {
        "symbol": spec.key, "display": spec.display, "bar_time": ev.bar_time,
        "price": round(price, spec.decimals), "atr_m15": round(ev.atr, spec.decimals),
        "atr_h1": round(float(A["h1_atr"][i]), spec.decimals) if _ok(A["h1_atr"][i]) else None,
        "trend_h4": trend_label(A, i, "h4_"), "trend_h1": trend_label(A, i, "h1_"),
        "trend_m15": trend_label(A, i, ""),
        "rsi_m15": round(float(A["rsi"][i]), 1), "rsi_h1": round(float(A["h1_rsi"][i]), 1) if _ok(A["h1_rsi"][i]) else None,
        "adx_h1": round(float(A["h1_adx"][i]), 1) if _ok(A["h1_adx"][i]) else None,
        "resistance": [(n, round(p, spec.decimals)) for n, p in above],
        "support": [(n, round(p, spec.decimals)) for n, p in below],
        "buy": side_view(ev.buy), "sell": side_view(ev.sell),
        "veto": ev.global_veto,
    }


def summary_text(s: dict) -> str:
    """Compact plain-text snapshot for Claude prompts."""
    res = ", ".join(f"{n} {p}" for n, p in s["resistance"]) or "none nearby"
    sup = ", ".join(f"{n} {p}" for n, p in s["support"]) or "none nearby"
    return (f"{s['display']}: price {s['price']} | trend H4 {s['trend_h4']}, H1 {s['trend_h1']}, M15 {s['trend_m15']} | "
            f"RSI M15 {s['rsi_m15']}, H1 {s['rsi_h1']} | ADX H1 {s['adx_h1']} | ATR M15 {s['atr_m15']}, H1 {s['atr_h1']} | "
            f"resistance: {res} | support: {sup} | "
            f"buy score {s['buy']['score']} (trend {s['buy']['trend_pts']}/55), sell score {s['sell']['score']} "
            f"(trend {s['sell']['trend_pts']}/55)")


def as_dict(obj):
    return asdict(obj)
