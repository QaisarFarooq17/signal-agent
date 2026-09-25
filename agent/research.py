"""Strategy research: test every rule set on ~10 months of history, honestly.

Method (per symbol):
  1. Run every rule set over the whole history with the live engine (no look-ahead).
  2. Split trades by entry time: the first 65% of the period is the TUNING period, the last 35%
     is the UNSEEN period.
  3. Rank rule sets only by their tuning-period results (a t-stat-like score: avg R x sqrt(n)).
  4. Walk down the top 3. The first one that ALSO holds up on the unseen period wins.
     If none does, there is no winner and signals stay in PAPER mode.
Choosing on one period and confirming on another keeps us from simply picking the luckiest
of 18 rule sets. A pass is evidence, not proof.
"""
from __future__ import annotations

import csv
import math
import os

from . import backtest as bt
from .config import Config
from .strategy import Context
from .tracker import stats
from .variants import VARIANTS

SPLIT = 0.65
TUNE_RULES = {"n": 20, "avg_r": 0.10, "pf": 1.15}
UNSEEN_RULES = {"n": 10, "avg_r": 0.05, "pf": 1.10}


def _passes(st: dict, rules: dict) -> bool:
    return bool(st.get("n", 0) >= rules["n"] and st.get("avg_r", -9) >= rules["avg_r"]
                and st.get("profit_factor", 0) >= rules["pf"])


def rank_score(st: dict) -> float:
    n = st.get("n", 0)
    return st["avg_r"] * math.sqrt(n) if n else -math.inf


def select_winner(rows: list[dict], top: int = 3) -> str | None:
    tuned = [r for r in rows if r["tune_ok"]]
    tuned.sort(key=lambda r: rank_score(r["tune"]), reverse=True)
    for r in tuned[:top]:
        if r["unseen_ok"]:
            return r["name"]
    return None


def run(ctx: Context, cfg: Config, variants=None) -> dict:
    variants = variants or list(VARIANTS.values())
    times = ctx.m.index
    start_i = bt.first_ready_index(ctx)
    if start_i >= len(times) - 100:
        raise ValueError("not enough history for research")
    split_time = times[start_i + int((len(times) - start_i) * SPLIT)].isoformat()
    rows, all_trades = [], {}
    for p in variants:
        trades, _ = bt.run(ctx, cfg, p)
        all_trades[p.name] = trades
        tune = stats([t for t in trades if t["bar_time"] < split_time])
        unseen = stats([t for t in trades if t["bar_time"] >= split_time])
        rows.append({"name": p.name, "desc": p.desc, "all": stats(trades), "tune": tune, "unseen": unseen,
                     "tune_ok": _passes(tune, TUNE_RULES), "unseen_ok": _passes(unseen, UNSEEN_RULES)})
    for r in rows:
        r["passed"] = r["tune_ok"] and r["unseen_ok"]
    return {"rows": rows, "winner": select_winner(rows), "split": split_time,
            "start": times[start_i].isoformat(), "end": times[-1].isoformat(), "bars": len(times) - start_i,
            "trades": all_trades}


def save_csv(result: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rule_set", "description", "passed", "tune_n", "tune_win", "tune_avg_r", "tune_pf",
                    "unseen_n", "unseen_win", "unseen_avg_r", "unseen_pf", "all_n", "all_avg_r", "all_total_r",
                    "all_max_dd_r"])
        for r in result["rows"]:
            t, u, a = r["tune"], r["unseen"], r["all"]
            w.writerow([r["name"], r["desc"], r["passed"], t.get("n", 0), t.get("win_rate"), t.get("avg_r"),
                        t.get("profit_factor"), u.get("n", 0), u.get("win_rate"), u.get("avg_r"),
                        u.get("profit_factor"), a.get("n", 0), a.get("avg_r"), a.get("total_r"), a.get("max_dd_r")])


def summary_for_state(result: dict) -> dict:
    """Compact version kept in the agent's memory (used to label live signals)."""
    return {
        "winner": result["winner"], "split": result["split"], "start": result["start"], "end": result["end"],
        "variants": {r["name"]: {"passed": r["passed"], "tune": r["tune"], "unseen": r["unseen"], "all": r["all"]}
                     for r in result["rows"]},
    }
