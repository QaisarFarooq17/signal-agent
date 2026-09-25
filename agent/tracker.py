"""Follows every signal to its outcome and keeps an honest scoreboard.

Management rule used everywhere (live and backtest):
  * close half at TP1 and move the stop to entry (break-even)
  * close the rest at TP2, at break-even, or at market when the signal expires
  * if a single candle touches both SL and TP, assume the SL was hit first (conservative)
  * the spread is charged on every trade
"""
from __future__ import annotations

import math


def r_of(sig: dict, price: float) -> float:
    s = 1 if sig["side"] == "BUY" else -1
    return s * (price - sig["entry"]) / sig["sl_dist"]


def _close(sig, price, when, reason, spread):
    sig["status"] = "closed"
    sig["exit_price"] = price
    sig["closed_at"] = when
    sig["exit_reason"] = reason
    cost = spread / sig["sl_dist"] if sig["sl_dist"] else 0.0
    sig["result_r"] = round(sig["realized_r"] - cost, 3)


def init_tracking(sig: dict) -> dict:
    sig.setdefault("status", "open")
    sig.setdefault("tp1_hit", False)
    sig.setdefault("realized_r", 0.0)
    sig.setdefault("sl_current", sig["sl"])
    sig.setdefault("bars", 0)
    sig.setdefault("last_checked", sig["bar_time"])
    return sig


def advance(sig: dict, bars, expiry_bars: int, spread: float) -> list[tuple[str, float, str]]:
    """Walk closed bars (iterable of (time_iso, high, low, close)) after the signal. Returns events."""
    init_tracking(sig)
    events = []
    s = 1 if sig["side"] == "BUY" else -1
    for t, h, l, c in bars:
        if sig["status"] != "open":
            break
        sig["bars"] += 1
        sig["last_checked"] = t
        sl = sig["sl_current"]
        hit_sl = (l <= sl) if s > 0 else (h >= sl)
        hit_tp1 = (h >= sig["tp1"]) if s > 0 else (l <= sig["tp1"])
        hit_tp2 = (h >= sig["tp2"]) if s > 0 else (l <= sig["tp2"])
        if not sig["tp1_hit"]:
            if hit_sl:
                sig["realized_r"] += r_of(sig, sl)
                _close(sig, sl, t, "sl", spread)
                events.append(("sl", sl, t))
                break
            if hit_tp1:
                sig["tp1_hit"] = True
                sig["realized_r"] += 0.5 * r_of(sig, sig["tp1"])
                sig["sl_current"] = sig["entry"]
                events.append(("tp1", sig["tp1"], t))
                if hit_tp2:
                    sig["realized_r"] += 0.5 * r_of(sig, sig["tp2"])
                    _close(sig, sig["tp2"], t, "tp2", spread)
                    events.append(("tp2", sig["tp2"], t))
                    break
                continue
        else:
            if hit_sl:
                _close(sig, sig["entry"], t, "be", spread)
                events.append(("be", sig["entry"], t))
                break
            if hit_tp2:
                sig["realized_r"] += 0.5 * r_of(sig, sig["tp2"])
                _close(sig, sig["tp2"], t, "tp2", spread)
                events.append(("tp2", sig["tp2"], t))
                break
        if sig["bars"] >= expiry_bars:
            remaining = 0.5 if sig["tp1_hit"] else 1.0
            sig["realized_r"] += remaining * r_of(sig, c)
            _close(sig, c, t, "expired", spread)
            events.append(("expired", c, t))
            break
    return events


def stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("status") == "closed" and t.get("result_r") is not None]
    n = len(closed)
    if not n:
        return {"n": 0}
    rs = [t["result_r"] for t in closed]
    wins = [r for r in rs if r > 0.05]
    losses = [r for r in rs if r < -0.05]
    gross_w, gross_l = sum(wins), -sum(losses)
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    usd = sum(t["result_r"] * t.get("risk_usd", 0) for t in closed)
    return {
        "n": n,
        "wins": len(wins), "losses": len(losses), "flat": n - len(wins) - len(losses),
        "win_rate": round(len(wins) / n, 3),
        "tp1_rate": round(sum(1 for t in closed if t.get("tp1_hit")) / n, 3),
        "total_r": round(sum(rs), 2), "avg_r": round(sum(rs) / n, 3),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else (math.inf if gross_w > 0 else 0.0),
        "max_dd_r": round(dd, 2), "usd": round(usd, 2),
    }
