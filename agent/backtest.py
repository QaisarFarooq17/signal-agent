"""Walk-forward backtest of the exact live logic on historical candles.

Limitations (stated in every report): no news/calendar filter (history not available
for free), spread is a fixed estimate, slippage is ignored.
"""
from __future__ import annotations

import csv
import os

from .config import Config, SymbolSpec
from .strategy import Context, evaluate_at
from .tracker import advance, init_tracking, stats


def first_ready_index(ctx: Context) -> int:
    A = ctx.A
    import numpy as np
    ok = ~np.isnan(A["h4_ema200"]) & ~np.isnan(A["h1_ema50"]) & ~np.isnan(A["pd_high"])
    idx = np.argmax(ok) if ok.any() else len(ok)
    return max(int(idx), 250)


def run(ctx: Context, cfg: Config) -> tuple[list[dict], dict]:
    spec: SymbolSpec = ctx.spec
    A, times = ctx.A, [t.isoformat() for t in ctx.m.index]
    expiry = int(cfg.signal_expiry_hours * 4)
    cooldown_bars = max(1, cfg.cooldown_minutes // 15)
    trades, open_sig, last_close = [], None, -10**9
    n = len(times)
    for i in range(first_ready_index(ctx), n):
        if open_sig is not None:
            advance(open_sig, [(times[i], A["high"][i], A["low"][i], A["close"][i])], expiry, spec.spread)
            if open_sig["status"] == "closed":
                trades.append(open_sig)
                open_sig, last_close = None, i
            continue
        if i - last_close < cooldown_bars:
            continue
        ev = evaluate_at(ctx, i, cfg)
        if ev.signal:
            open_sig = init_tracking(dict(ev.signal))
    result = {"all": stats(trades)}
    for g in ("A", "B"):
        result[g] = stats([t for t in trades if t["grade"] == g])
    for side in ("BUY", "SELL"):
        result[side] = stats([t for t in trades if t["side"] == side])
    start = times[first_ready_index(ctx)] if n else None
    result["period"] = {"start": start, "end": times[-1] if n else None}
    return trades, result


def save_csv(trades: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = ["symbol", "side", "grade", "score", "setup", "bar_time", "entry", "sl", "tp1", "tp2",
            "tp1_hit", "exit_reason", "exit_price", "closed_at", "result_r", "lots", "risk_usd"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)
