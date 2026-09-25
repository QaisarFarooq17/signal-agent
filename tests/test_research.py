"""Rule sets, research selection, long-history paging and the news digest."""
import random
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from agent import news, research
from agent.config import Config
from agent.data import get_long_history
from agent.strategy import build_context, evaluate_at
from agent.tracker import advance, init_tracking
from agent.variants import VARIANTS
from tests.synthetic import frames
from tests.test_core import _truncate


@pytest.fixture(scope="module")
def data():
    return frames(50, seed=21)


@pytest.mark.parametrize("name", ["pb_aligned_1R", "rsi2_trend", "london_break", "v1_sessions", "v1_wide_sl"])
def test_rule_sets_have_no_lookahead(data, name):
    cfg = Config()
    spec = cfg.specs["XAUUSD"]
    p = VARIANTS[name]
    m15, h1, h4 = data
    full = build_context(m15, h1, h4, spec)
    rng = random.Random(7)
    for i in rng.sample(range(1200, len(m15) - 5), 20):
        cut = build_context(*_truncate(m15, h1, h4, i), spec)
        a = evaluate_at(full, i, cfg, params=p)
        b = evaluate_at(cut, len(cut.m) - 1, cfg, params=p)
        assert (a.signal is None) == (b.signal is None), (name, i)
        assert (a.buy.setup, a.sell.setup, a.buy.score, a.sell.score) == (b.buy.setup, b.sell.setup, b.buy.score, b.sell.score)
        if a.signal:
            assert a.signal["sl"] == pytest.approx(b.signal["sl"]) and a.signal["strategy"] == name


def test_new_setups_fire(data):
    cfg = Config()
    ctx = build_context(*data, cfg.specs["XAUUSD"])
    seen = {"rsi2": 0, "london": 0}
    for i in range(300, len(ctx.m)):
        for p in (VARIANTS["rsi2_trend"], VARIANTS["london_break"]):
            ev = evaluate_at(ctx, i, cfg, params=p)
            if ev.signal:
                seen[ev.signal["setup"]] += 1
    assert seen["rsi2"] > 0 and seen["london"] > 0
    # London breakouts only fire between 07:00 and 11:00 UTC
    for i in range(300, len(ctx.m)):
        ev = evaluate_at(ctx, i, cfg, params=VARIANTS["london_break"])
        if ev.signal:
            assert 7 <= ctx.m.index[i].hour < 11


def test_single_target_and_buy_only():
    sig = init_tracking({"symbol": "XAUUSD", "side": "BUY", "bar_time": "t0", "entry": 100.0, "sl": 90.0,
                         "tp1": 110.0, "tp2": 110.0, "sl_dist": 10.0, "atr": 5, "lots": 0.01, "risk_usd": 10,
                         "grade": "B", "partial": 1.0, "be": True})
    ev = advance(sig, [("t1", 111, 99, 109)], 96, spread=0.0)
    assert [e[0] for e in ev] == ["tp1"] and sig["exit_reason"] == "tp1" and sig["result_r"] == pytest.approx(1.0)
    cfg = Config()
    ctx = build_context(*frames(40, seed=3), cfg.specs["XAUUSD"])
    sides = {evaluate_at(ctx, i, cfg, params=VARIANTS["v1_buy"]).signal["side"]
             for i in range(300, len(ctx.m)) if evaluate_at(ctx, i, cfg, params=VARIANTS["v1_buy"]).signal}
    assert sides <= {"BUY"}


def _row(name, tn, ta, tpf, un, ua, upf):
    t = {"n": tn, "avg_r": ta, "profit_factor": tpf}
    u = {"n": un, "avg_r": ua, "profit_factor": upf}
    return {"name": name, "tune": t, "unseen": u, "tune_ok": research._passes(t, research.TUNE_RULES),
            "unseen_ok": research._passes(u, research.UNSEEN_RULES)}


def test_winner_selection_uses_tuning_rank_then_unseen_check():
    rows = [_row("lucky_small", 8, 1.5, 5.0, 12, 0.5, 2.0),     # too few tuning trades -> ignored
            _row("best_tune_fails", 60, 0.40, 2.0, 30, -0.1, 0.8),
            _row("second", 50, 0.30, 1.6, 25, 0.15, 1.3),
            _row("third", 40, 0.25, 1.5, 20, 0.30, 1.8)]
    assert research.select_winner(rows) == "second"          # first of the top-3 that also passes unseen
    rows[2]["unseen_ok"] = False
    assert research.select_winner(rows) == "third"
    rows[3]["unseen_ok"] = False
    assert research.select_winner(rows) is None


def test_long_history_pages_backwards(monkeypatch):
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    calls = []

    def fake_fetch(symbol, tf, n, key, session=None, start=None, end=None):
        calls.append((tf, start, end))
        if start < now - timedelta(days=200) and tf == "M15":
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                index=pd.DatetimeIndex([], tz="UTC"))       # provider has no older data
        step = {"M15": "15min", "H1": "1h", "H4": "4h"}[tf]
        idx = pd.date_range(start, end, freq=step, tz="UTC", inclusive="left")
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0}, index=idx)

    import agent.data as d
    monkeypatch.setattr(d, "fetch_twelvedata", fake_fetch)
    sleeps = []
    out = get_long_history(Config().specs["XAUUSD"], Config(twelvedata_key="k"), days=300, now=now,
                           sleep=sleeps.append)
    m15 = [c for c in calls if c[0] == "M15"]
    assert all((e - s) <= timedelta(days=40) for _, s, e in m15)          # every window < 5000 bars
    assert out["M15"].index[0] >= now - timedelta(days=201)               # stopped when data ran out
    assert out["M15"].index.is_unique and out["M15"].index.is_monotonic_increasing
    assert out["H4"].index[0] <= now - timedelta(days=300)                # H4 has extra warm-up
    assert len(sleeps) == len(calls) - 1                                   # throttled between calls


def test_digest_relevance():
    assert news.digest_relevant({"kind": "crypto", "title": "Bitcoin ETF inflows hit a record", "summary": ""})
    assert not news.digest_relevant({"kind": "crypto", "title": "New memecoin launches on Solana", "summary": ""})
    assert news.digest_relevant({"kind": "macro", "title": "anything already filtered", "summary": ""})
