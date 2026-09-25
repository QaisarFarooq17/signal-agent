"""Named rule sets ("strategies") that the research mode tests against each other.

Each one is a small change to the same engine, so results are directly comparable.
`v1` is the original confluence strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

LONDON_NY = (("07:00", "17:00"),)   # UTC: London open to New York afternoon


@dataclass(frozen=True)
class Params:
    name: str = "v1"
    desc: str = "Original: confluence score ≥ 65, pullback or breakout"
    min_score: int | None = None          # None = use MIN_SCORE setting (default 65)
    sides: str = "both"                   # both | buy | sell
    setups: tuple = ("pullback", "breakout")   # pullback | breakout | rsi2 | london
    require_h4: bool = False              # H4 price > EMA50 > EMA200 (or mirror) required
    require_h1: bool = False              # H1 EMA20 > EMA50 with price above (or mirror) required
    sessions_utc: tuple = ()              # only trade when the M15 candle closes inside these windows
    adx_min: float = 0.0                  # minimum H1 ADX
    sl_min_atr: float = 1.0
    sl_max_atr: float = 2.5
    tp1_r: float = 1.5
    tp2_r: float = 3.0
    partial: float = 0.5                  # share closed at TP1 (1.0 = one target only)
    be_after_tp1: bool = True             # move stop to entry after TP1


V1 = Params()
_PB = Params(name="pb_aligned", desc="Pullback only, H4 + H1 trend must agree (no score)", min_score=0,
             setups=("pullback",), require_h4=True, require_h1=True)
_RSI2 = Params(name="rsi2_trend", desc="RSI(2) dip-buy / rip-sell with the H4 trend, single 1R target",
               min_score=0, setups=("rsi2",), require_h4=True, tp1_r=1.0, partial=1.0)
_LDN = Params(name="london_break", desc="Break of the Asian range (00–07 UTC) during 07–11 UTC", min_score=0,
              setups=("london",))

VARIANTS: dict[str, Params] = {p.name: p for p in [
    V1,
    replace(V1, name="v1_buy", desc="Original, BUY signals only", sides="buy"),
    replace(V1, name="v1_strict", desc="Original with score ≥ 75", min_score=75),
    replace(V1, name="v1_sessions", desc="Original, London + New York hours only", sessions_utc=LONDON_NY),
    replace(V1, name="v1_adx25", desc="Original, only when H1 ADX ≥ 25 (strong trend)", adx_min=25),
    replace(V1, name="v1_wide_sl", desc="Original with wider stops (1.5–3 ATR)", sl_min_atr=1.5, sl_max_atr=3.0),
    replace(V1, name="pullback_only", desc="Original scoring, pullback setups only", setups=("pullback",)),
    replace(V1, name="breakout_only", desc="Original scoring, breakout setups only (score ≥ 60)",
            setups=("breakout",), min_score=60),
    _PB,
    replace(_PB, name="pb_aligned_1R", desc="Aligned pullback, single target at 1R", tp1_r=1.0, partial=1.0),
    replace(_PB, name="pb_aligned_2R", desc="Aligned pullback, single target at 2R", tp1_r=2.0, partial=1.0),
    replace(_PB, name="pb_aligned_sess", desc="Aligned pullback, London + New York hours only",
            sessions_utc=LONDON_NY),
    replace(_PB, name="pb_aligned_buy", desc="Aligned pullback, BUY only", sides="buy"),
    _RSI2,
    replace(_RSI2, name="rsi2_trend_1.5", desc="RSI(2) with the H4 trend, half at 1.5R, rest at 3R",
            tp1_r=1.5, partial=0.5),
    _LDN,
    replace(_LDN, name="london_break_h1", desc="Asian-range breakout, H1 trend must agree", require_h1=True),
    replace(_LDN, name="london_break_2R", desc="Asian-range breakout, single target at 2R", tp1_r=2.0, partial=1.0),
]}


def get(name: str | None) -> Params:
    return VARIANTS.get(name or "v1", V1)
