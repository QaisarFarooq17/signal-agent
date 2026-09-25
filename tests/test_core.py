import math
import random

import pandas as pd
import pytest

from agent.config import Config
from agent.indicators import atr, enrich, rsi, supertrend
from agent.strategy import build_context, evaluate_at, position_size
from agent.tracker import advance, init_tracking, stats
from tests.synthetic import frames


@pytest.fixture(scope="module")
def data():
    return frames(45, seed=11)


def test_indicators_sane(data):
    m15, _, _ = data
    r = rsi(m15["close"]).dropna()
    assert len(r) > 100 and r.between(0, 100).all()
    assert (atr(m15) > 0).all()
    _, d = supertrend(m15)
    assert set(d.unique()) <= {1, -1}
    e = enrich(m15)
    for col in ("ema20", "ema50", "ema200", "macd_hist", "adx", "bbw", "bull_engulf"):
        assert col in e


def _truncate(m15, h1, h4, i):
    """Data exactly as it would have looked live when M15 bar i had just closed."""
    t_close = m15.index[i] + pd.Timedelta(minutes=15)
    return (m15.iloc[: i + 1],
            h1[h1.index + pd.Timedelta(minutes=60) <= t_close],
            h4[h4.index + pd.Timedelta(minutes=240) <= t_close])


def test_no_lookahead(data):
    """Evaluating bar i with the full history must equal evaluating it with data cut at bar i."""
    cfg = Config()
    spec = cfg.specs["XAUUSD"]
    m15, h1, h4 = data
    full = build_context(m15, h1, h4, spec)
    rng = random.Random(3)
    idxs = rng.sample(range(900, len(m15) - 5), 25)
    for i in idxs:
        cut = build_context(*_truncate(m15, h1, h4, i), spec)
        a = evaluate_at(full, i, cfg)
        b = evaluate_at(cut, len(cut.m) - 1, cfg)
        assert (a.buy.score, a.sell.score, a.buy.setup, a.sell.setup) == (b.buy.score, b.sell.score, b.buy.setup, b.sell.setup), i
        assert (a.signal is None) == (b.signal is None)
        if a.signal:
            for k in ("entry", "sl", "tp1", "tp2", "lots"):
                assert a.signal[k] == pytest.approx(b.signal[k]), (i, k)


def test_random_walk_has_no_fake_edge():
    """On pure noise a correct (look-ahead-free) backtest should not show a big edge."""
    from agent import backtest
    cfg = Config()
    spec = cfg.specs["XAUUSD"]
    tot, n = 0.0, 0
    for seed in (1, 2, 3, 4):
        m15, h1, h4 = frames(60, seed=seed, path=True)  # pure random walk: no real edge exists
        trades, res = backtest.run(build_context(m15, h1, h4, spec), cfg)
        if res["all"].get("n"):
            tot += res["all"]["total_r"]
            n += res["all"]["n"]
    assert n > 10
    assert abs(tot / n) < 0.3  # avg R per trade stays near zero


def test_position_size():
    cfg = Config()
    xau, btc = cfg.specs["XAUUSD"], cfg.specs["BTCUSD"]
    lots, risk, pct = position_size(xau, cfg, 9.3)        # $600 account, 1% = $6 target
    assert lots == 0.01 and risk == pytest.approx(9.3) and pct == pytest.approx(1.55)
    lots, risk, pct = position_size(btc, cfg, 400)
    assert lots == 0.01 and risk == pytest.approx(4.0)
    big = Config(account_balance=10_000, risk_pct=1.0)
    lots, risk, _ = position_size(xau, big, 10.0)          # $100 / ($10 * 100oz) = 0.10 lot
    assert lots == pytest.approx(0.10) and risk == pytest.approx(100.0)


def _sig(side="BUY"):
    s = 1 if side == "BUY" else -1
    return init_tracking({"symbol": "XAUUSD", "side": side, "bar_time": "2026-09-25T10:00:00+00:00",
                          "entry": 4000.0, "sl": 4000.0 - s * 10, "tp1": 4000.0 + s * 15, "tp2": 4000.0 + s * 30,
                          "sl_dist": 10.0, "atr": 5.0, "lots": 0.01, "risk_usd": 10.0, "grade": "A"})


def test_tracker_paths():
    # TP1 then back to entry: +0.75R minus spread cost
    s = _sig()
    ev = advance(s, [("t1", 4016, 4001, 4010), ("t2", 4012, 3999, 4000)], 96, spread=0.0)
    assert [e[0] for e in ev] == ["tp1", "be"] and s["result_r"] == pytest.approx(0.75)
    # TP1 then TP2: 0.5*1.5 + 0.5*3 = 2.25R
    s = _sig()
    advance(s, [("t1", 4016, 4001, 4010), ("t2", 4031, 4005, 4030)], 96, spread=0.0)
    assert s["exit_reason"] == "tp2" and s["result_r"] == pytest.approx(2.25)
    # candle touching SL and TP1 -> conservative SL
    s = _sig()
    advance(s, [("t1", 4016, 3989, 4000)], 96, spread=0.0)
    assert s["exit_reason"] == "sl" and s["result_r"] == pytest.approx(-1.0)
    # SELL mirrored + spread cost 0.5 on a 10 stop = 0.05R
    s = _sig("SELL")
    advance(s, [("t1", 4001, 3984, 3990), ("t2", 3995, 3969, 3970)], 96, spread=0.5)
    assert s["exit_reason"] == "tp2" and s["result_r"] == pytest.approx(2.2)
    # expiry at market
    s = _sig()
    advance(s, [(f"t{k}", 4005, 3995, 4005) for k in range(4)], 4, spread=0.0)
    assert s["exit_reason"] == "expired" and s["result_r"] == pytest.approx(0.5)


def test_stats():
    trades = [{"status": "closed", "result_r": r, "tp1_hit": r > 0, "risk_usd": 10} for r in (2.25, -1, 0.75, -1, 0)]
    st = stats(trades)
    assert st["n"] == 5 and st["wins"] == 2 and st["losses"] == 2 and st["flat"] == 1
    assert st["total_r"] == pytest.approx(1.0) and st["profit_factor"] == pytest.approx(1.5)
    assert st["usd"] == pytest.approx(10.0)
    assert not math.isnan(st["max_dd_r"])
