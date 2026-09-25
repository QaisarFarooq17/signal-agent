"""Telegram message formatting (HTML)."""
from __future__ import annotations

import html
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config, SymbolSpec

ARROWS = {2: "▲▲", 1: "▲", 0: "•", -1: "▼", -2: "▼▼"}


def esc(x) -> str:
    return html.escape(str(x), quote=False)


def when(t: datetime | str, cfg: Config, both: bool = True) -> str:
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    loc = t.astimezone(ZoneInfo(cfg.display_tz))
    city = cfg.display_tz.split("/")[-1].replace("_", " ")
    s = f"{loc:%H:%M} {city}"
    return f"{s} ({t.astimezone(timezone.utc):%H:%M} MT5)" if both else s


def px(spec: SymbolSpec, v: float) -> str:
    return f"{v:,.{spec.decimals}f}".replace(",", "")


def signed(spec: SymbolSpec, v: float) -> str:
    return ("+" if v >= 0 else "−") + f"{abs(v):.{spec.decimals}f}"


def fmt_stats(st: dict | None) -> str:
    if not st or not st.get("n"):
        return "no trades"
    pf = st.get("profit_factor", 0)
    pf_s = "∞" if pf == math.inf or pf >= 999 else f"{pf:.2f}"
    return f"n={st['n']} · {st['win_rate'] * 100:.0f}% win · {st['avg_r']:+.2f}R/trade · PF {pf_s}"


def signal_message(sig: dict, spec: SymbolSpec, cfg: Config, news_b: float | None = None,
                   review: dict | None = None, calib: dict | None = None, validation: dict | None = None) -> str:
    buy = sig["side"] == "BUY"
    s = 1 if buy else -1
    close_t = datetime.fromisoformat(sig["bar_time"]) + timedelta(minutes=15)
    chase = sig["entry"] + s * 0.5 * sig["atr"]
    r1 = abs(sig["tp1"] - sig["entry"]) / sig["sl_dist"]
    r2 = abs(sig["tp2"] - sig["entry"]) / sig["sl_dist"]
    single = float(sig.get("partial", 0.5)) >= 0.999
    setup = {"pullback": "Pullback in trend", "breakout": "Range breakout", "rsi2": "RSI(2) dip in trend",
             "london": "London breakout of the Asian range"}.get(sig["setup"], sig["setup"])
    validated = bool(validation and validation.get("validated"))
    lines = []
    if not validated:
        lines.append("📝 <b>PAPER SIGNAL</b> — for testing only, this strategy has not passed validation yet")
    lines += [
        f"{'🟢' if buy else '🔴'} <b>{sig['side']} {esc(spec.display)}</b> · score {sig['score']}/100",
        f"<i>{setup} · M15 close {when(close_t, cfg)}</i>",
        "",
        f"Entry <b>{px(spec, sig['entry'])}</b> (still valid {'up to' if buy else 'down to'} {px(spec, chase)})",
        f"SL <b>{px(spec, sig['sl'])}</b> ({signed(spec, sig['sl'] - sig['entry'])})",
    ]
    if single:
        lines.append(f"TP <b>{px(spec, sig['tp1'])}</b> ({signed(spec, sig['tp1'] - sig['entry'])} · {r1:.1f}R) → close all")
    else:
        part = float(sig.get("partial", 0.5))
        share = "½" if abs(part - 0.5) < 1e-9 else f"{part * 100:.0f}%"
        be = ", move SL to entry" if sig.get("be", True) else ""
        lines += [f"TP1 <b>{px(spec, sig['tp1'])}</b> ({signed(spec, sig['tp1'] - sig['entry'])} · {r1:.1f}R) → close {share}{be}",
                  f"TP2 <b>{px(spec, sig['tp2'])}</b> ({signed(spec, sig['tp2'] - sig['entry'])} · {r2:.1f}R)"]
    lines.append(f"Lot <b>{sig['lots']:.2f}</b> → risk ${sig['risk_usd']:.2f} ({sig['risk_pct']:.1f}% of ${cfg.account_balance:,.0f})")
    if sig["risk_pct"] > cfg.max_risk_pct_warn:
        lines.append(f"⚠️ Even the minimum lot risks {sig['risk_pct']:.1f}% here — consider skipping.")
    elif sig["risk_pct"] > cfg.risk_pct * 1.25:
        lines.append(f"<i>Min lot is above your {cfg.risk_pct:g}% target on this stop.</i>")
    lines += ["", "<b>Why</b>"] + [f"• {esc(r)}" for r in sig["reasons"]]
    extra = []
    if validation:
        u = validation.get("unseen")
        tag = "✅ passed the unseen-data test" if validated else "not validated"
        extra.append(f"🔬 Strategy <b>{esc(validation['name'])}</b>: {tag}" + (f" ({fmt_stats(u)})" if u and u.get("n") else ""))
    if review:
        icon = {"confirm": "✅", "caution": "⚠️", "reject": "⛔"}[review["verdict"]]
        extra.append(f"🤖 Claude check: {icon} {review['verdict'].upper()} — {esc(review['reason'])}")
    if calib and calib.get("n", 0) >= 5 and not (validation and validation.get("unseen")):
        extra.append(f"🧪 Backtest: TP1 hit {calib['tp1_rate'] * 100:.0f}% · avg {calib['avg_r']:+.2f}R · n={calib['n']}")
    if extra:
        lines += [""] + extra
    lines += ["", f"<i>Signal #{sig.get('id', '?')} · skip it if price is already past the 'valid' level · "
                  f"not financial advice</i>"]
    return "\n".join(lines)


def short_caption(sig: dict, spec: SymbolSpec, paper: bool = False) -> str:
    return (("📝 PAPER · " if paper else "") +
            f"{'🟢' if sig['side'] == 'BUY' else '🔴'} <b>{sig['side']} {esc(spec.display)}</b> #{sig.get('id', '?')} · "
            f"entry {px(spec, sig['entry'])} · SL {px(spec, sig['sl'])} · TP {px(spec, sig['tp1'])}")


def track_message(sig: dict, spec: SymbolSpec, event: str, price: float) -> str:
    tag = f"#{sig.get('id', '?')} {sig['side']} {esc(spec.display)}"
    usd = (sig.get("result_r") or 0) * sig.get("risk_usd", 0)
    total = f"total {sig.get('result_r', 0):+.2f}R (≈ {'+' if usd >= 0 else '−'}${abs(usd):.2f} at {sig['lots']:.2f} lot)"
    if event == "tp1" and float(sig.get("partial", 0.5)) >= 0.999:
        return f"✅ <b>{tag} hit its target</b> {px(spec, price)} — closed, {total}"
    if event == "tp1":
        return (f"✅ <b>{tag} hit TP1</b> {px(spec, price)}\n"
                f"Close half, move SL to entry {px(spec, sig['entry'])}. Runner targets TP2 {px(spec, sig['tp2'])}.")
    if event == "tp2":
        return f"🏁 <b>{tag} hit TP2</b> {px(spec, price)} — closed, {total}"
    if event == "sl":
        return f"❌ <b>{tag} stopped out</b> at {px(spec, price)} — {total}"
    if event == "be":
        return f"➖ <b>{tag} back to entry</b> — runner closed at break-even, {total}"
    if event == "expired":
        return f"⌛ <b>{tag} expired</b> (no TP/SL in {sig.get('bars', 0) // 4}h) at {px(spec, price)} — {total}"
    return f"{tag}: {event} {px(spec, price)}"


def news_alert(items: list[dict], cfg: Config) -> str:
    lines = ["📰 <b>Market-moving news</b>"]
    for it in items:
        sc = it["score"]
        lines.append(f"• <b>{esc(sc.get('summary') or it['title'])}</b>")
        lines.append(f"  Gold {ARROWS.get(sc['xau'], '•')} · BTC {ARROWS.get(sc['btc'], '•')} · impact {sc['impact']}/5 · "
                     f"{esc(it['source'])} {when(it['published'], cfg, both=False)}")
        if it.get("link"):
            lines.append(f"  <a href=\"{esc(it['link'])}\">source</a>")
    return "\n".join(lines)


def event_alert(ev: dict, cfg: Config, minutes: int) -> str:
    t = datetime.fromisoformat(ev["time"])
    a = when(t - timedelta(minutes=cfg.news_blackout_minutes), cfg, both=False).split()[0]
    b = when(t + timedelta(minutes=cfg.news_blackout_minutes), cfg, both=False).split()[0]
    fc = f"Forecast {esc(ev['forecast'])} · " if ev.get("forecast") else ""
    pv = f"Previous {esc(ev['previous'])}" if ev.get("previous") else ""
    return (f"⏰ <b>In ~{minutes} min: {esc(ev['country'])} {esc(ev['title'])}</b>\n"
            f"at {when(t, cfg).replace(' (', ' · ').rstrip(')')}\n"
            f"{fc}{pv}\n"
            f"Expect a volatility spike in gold and BTC. New signals paused {a}–{b}. "
            f"Consider tightening stops or banking partial profit on open trades.")


def status_message(summaries: list[dict], biases: dict, cfg: Config, now: datetime, open_sigs: list[dict],
                   events: list[dict]) -> str:
    lines = [f"📊 <b>Market status</b> · {when(now, cfg)}"]
    for s in summaries:
        res = " · ".join(f"{esc(n)} {p}" for n, p in s["resistance"][:2]) or "—"
        sup = " · ".join(f"{esc(n)} {p}" for n, p in s["support"][:2]) or "—"
        b, se = s["buy"], s["sell"]

        def side(v, name):
            tag = f"{v['setup']} setup" if v["setup"] else "no trigger yet"
            if v.get("veto"):
                tag += f", blocked: {esc(v['veto'])}"
            return f"{name} {v['score']}/100 ({tag})"
        strat = ""
        if s.get("strategy"):
            strat = f"Strategy: {esc(s['strategy'])} ({'✅ validated' if s.get('validated') else '📝 paper'})"
        lines += ["", f"<b>{esc(s['display'])} {s['price']}</b>"] + ([strat] if strat else []) + [
                  f"Trend: H4 {s['trend_h4']} · H1 {s['trend_h1']} · M15 {s['trend_m15']}",
                  f"RSI M15 {s['rsi_m15']:.0f} · H1 {s['rsi_h1'] or 0:.0f} · ADX H1 {s['adx_h1'] or 0:.0f} · ATR M15 {s['atr_m15']}",
                  f"Resistance: {res}", f"Support: {sup}",
                  side(b, "Buy") + " · " + side(se, "Sell"),
                  f"News bias: {biases.get(s['symbol'], 0):+.2f}"]
        if s.get("veto"):
            lines.append(f"⏸ Paused: {esc(s['veto'])}")
    if open_sigs:
        lines += ["", "<b>Open signals</b>"] + [
            f"#{o['id']} {o['side']} {o['symbol']} @ {o['entry']} · SL {o['sl_current']} · "
            f"{'TP1 hit' if o.get('tp1_hit') else 'running'}" for o in open_sigs]
    if events:
        lines += ["", "<b>Next events</b>"] + [
            f"{when(e['time'], cfg, both=False)} {esc(e['country'])} {esc(e['title'])} ({e['impact']})" for e in events[:5]]
    lines.append("\n<i>Buy/Sell score = trend + trigger confluence (0–100). 📝 paper = strategy not validated yet.</i>")
    return "\n".join(lines)


def _pf(v) -> str:
    return "∞" if v == math.inf else f"{v:.2f}"


def stats_block(st: dict) -> list[str]:
    if not st or not st.get("n"):
        return ["No closed signals yet."]
    return [f"{st['n']} signals · {st['wins']} wins · {st['losses']} losses · {st['flat']} flat",
            f"Win rate {st['win_rate'] * 100:.0f}% · TP1 hit {st['tp1_rate'] * 100:.0f}%",
            f"Total {st['total_r']:+.2f}R · avg {st['avg_r']:+.2f}R · PF {_pf(st['profit_factor'])} · max DD {st['max_dd_r']:.1f}R",
            f"≈ {'+' if st['usd'] >= 0 else '−'}${abs(st['usd']):.2f} at the suggested lots"]


def scoreboard(title: str, st: dict, open_sigs: list[dict]) -> str:
    lines = [f"📊 <b>{esc(title)}</b>"] + stats_block(st)
    if open_sigs:
        lines += ["", "Open: " + ", ".join(f"#{o['id']} {o['side']} {o['symbol']}" for o in open_sigs)]
    return "\n".join(lines)


def backtest_message(spec: SymbolSpec, res: dict, cfg: Config) -> str:
    p = res.get("period", {})
    start, end = (p.get("start") or "")[:10], (p.get("end") or "")[:10]
    lines = [f"🧪 <b>Backtest — {esc(spec.display)}</b> ({start} → {end}, M15 entries)"] + stats_block(res["all"])
    for k in ("A", "B", "BUY", "SELL"):
        st = res.get(k, {})
        if st.get("n"):
            lines.append(f"{'Grade ' + k if k in 'AB' else k}: n={st['n']} · win {st['win_rate'] * 100:.0f}% · avg {st['avg_r']:+.2f}R")
    if res["all"].get("n", 0) < 30:
        lines.append("⚠️ Small sample — treat these numbers with caution.")
    lines.append(f"<i>Same rules as live. No news filter (no free history), fixed spread {spec.spread:g}, "
                 f"no slippage. Past results do not guarantee future results.</i>")
    return "\n".join(lines)


def position_check(spec: SymbolSpec, cfg: Config, side: str, entry: float, lots: float, price: float, summ: dict,
                   with_pts: int, against_pts: int, stop: float, events: list[dict]) -> str:
    s = 1 if side == "BUY" else -1
    pnl = s * (price - entry) * spec.contract_size * lots
    stop_pnl = s * (stop - entry) * spec.contract_size * lots
    if with_pts >= 35 and with_pts > against_pts:
        verdict = "trend is WITH you"
    elif against_pts >= 35 and against_pts > with_pts:
        verdict = "trend is AGAINST you"
    else:
        verdict = "no clear trend either way"
    res = " · ".join(f"{esc(n)} {p}" for n, p in summ["resistance"][:2]) or "—"
    sup = " · ".join(f"{esc(n)} {p}" for n, p in summ["support"][:2]) or "—"
    lines = [
        f"🔍 <b>Position check — {side} {esc(spec.display)} {lots:.2f} @ {px(spec, entry)}</b>",
        f"Now {px(spec, price)} → {'+' if pnl >= 0 else '−'}${abs(pnl):.2f}",
        f"Trend: H4 {summ['trend_h4']} · H1 {summ['trend_h1']} · M15 {summ['trend_m15']} → <b>{verdict}</b> "
        f"(trend score {with_pts}/55 for your side vs {against_pts}/55 against)",
        f"Resistance: {res}",
        f"Support: {sup}",
        f"Structure-based stop for this {side.lower()}: <b>{px(spec, stop)}</b> → "
        f"{'+' if stop_pnl >= 0 else '−'}${abs(stop_pnl):.2f} if hit",
    ]
    if pnl < 0 and summ.get("atr_m15"):
        lines.append(f"Back to break-even needs {abs(price - entry):.{spec.decimals}f} "
                     f"(≈ {abs(price - entry) / summ['atr_m15']:.1f} × M15 ATR)")
    if events:
        e = events[0]
        lines.append(f"⏰ Next high-impact event: {esc(e['country'])} {esc(e['title'])} at {when(e['time'], cfg, both=False)}")
    lines.append("<i>Information, not an instruction. Decide your exit level now, before the market decides it.</i>")
    return "\n".join(lines)


def _bias_word(s: dict) -> str:
    h4, h1 = s["trend_h4"], s["trend_h1"]
    if h4 == h1 == "UP":
        return "bullish"
    if h4 == h1 == "DOWN":
        return "bearish"
    if "UP" in (h4, h1) and "DOWN" not in (h4, h1):
        return "leaning bullish"
    if "DOWN" in (h4, h1) and "UP" not in (h4, h1):
        return "leaning bearish"
    return "mixed / no clear trend"


def free_brief(title: str, summaries: list[dict], events: list[dict], headlines: list[dict], biases: dict,
               cfg: Config, ai_failed: bool = False) -> str:
    """Brief built only from candles, the calendar and RSS headlines (no AI needed)."""
    lines = [f"<b>{esc(title)}</b>", "<i>Technical + news brief (rule-based)</i>"]
    for s in summaries:
        bias = _bias_word(s)
        sup = s["support"][:2]
        res = s["resistance"][:2]
        lines += ["", f"<b>{esc(s['display'])} {s['price']}</b> — bias <b>{bias}</b>",
                  f"Trend H4 {s['trend_h4']} · H1 {s['trend_h1']} · M15 {s['trend_m15']} · RSI H1 {s['rsi_h1'] or 0:.0f} · "
                  f"news {biases.get(s['symbol'], 0):+.2f}",
                  "Resistance: " + (" · ".join(f"{esc(n)} {p}" for n, p in res) or "—"),
                  "Support: " + (" · ".join(f"{esc(n)} {p}" for n, p in sup) or "—")]
        if bias.endswith("bullish") and sup:
            plan = f"Look for buy setups on dips toward {sup[0][1]}" + (f"; below {sup[1][1]} stand aside." if len(sup) > 1 else ".")
        elif bias.endswith("bearish") and res:
            plan = f"Look for sell setups on rallies toward {res[0][1]}" + (f"; above {res[1][1]} stand aside." if len(res) > 1 else ".")
        else:
            plan = "No clear trend: wait for a signal or trade the range edges only."
        lines.append(f"Plan: {plan}")
    lines += ["", "<b>Event risk (next 24h)</b>"]
    lines += [f"• {when(e['time'], cfg, both=False)} {esc(e['country'])} {esc(e['title'])} ({e['impact']})"
              for e in events[:6]] or ["• no medium/high-impact USD events"]
    lines += ["", "<b>Top headlines (last 12h)</b>"]
    if headlines:
        for it in headlines:
            sc = it.get("score", {})
            link = f" <a href=\"{esc(it['link'])}\">link</a>" if it.get("link") else ""
            lines.append(f"• {esc(it['title'][:140])} — {esc(it['source'])} {when(it['published'], cfg, both=False)} "
                         f"(gold {ARROWS.get(sc.get('xau', 0), '•')} BTC {ARROWS.get(sc.get('btc', 0), '•')}){link}")
    else:
        lines.append("• none relevant")
    note = "AI brief failed this time, so this is the rule-based version." if ai_failed else \
        "Free mode: no AI key set. Add one later for written analysis with live web search."
    lines += ["", f"<i>{note} Bias, not certainty · not financial advice.</i>"]
    return "\n".join(lines)


def research_message(spec: SymbolSpec, res: dict, active: str, overridden: bool, switched_from: str | None) -> str:
    from .research import rank_score  # local import to avoid a cycle
    rows = res["rows"]
    from .research import TUNE_RULES
    # Rule sets with enough tuning trades first, then by tuning score.
    ranked = sorted(rows, key=lambda r: (r["tune"].get("n", 0) >= TUNE_RULES["n"], rank_score(r["tune"])),
                    reverse=True)
    start, split, end = res["start"][:10], res["split"][:10], res["end"][:10]
    lines = [f"🔬 <b>Strategy research — {esc(spec.display)}</b>",
             f"{start} → {end} · {len(rows)} rule sets tested",
             f"Tuned on {start} → {split}, then checked on <b>unseen</b> data {split} → {end}.",
             "", "<b>Top 5 on the tuning period</b>"]
    for k, r in enumerate(ranked[:5], 1):
        lines.append(f"{k}. <b>{esc(r['name'])}</b> {'✅' if r['passed'] else '❌'} — {esc(r['desc'])}")
        lines.append(f"   tuning: {fmt_stats(r['tune'])}")
        lines.append(f"   unseen: {fmt_stats(r['unseen'])}")
    v1 = next((r for r in rows if r["name"] == "v1"), None)
    if v1 and v1 not in ranked[:5]:
        lines.append(f"Original v1 — tuning: {fmt_stats(v1['tune'])} | unseen: {fmt_stats(v1['unseen'])}")
    lines.append("")
    w = res["winner"]
    if w:
        desc = next(r["desc"] for r in rows if r["name"] == w)
        lines.append(f"✅ <b>Winner: {esc(w)}</b> — {esc(desc)}. It made money on the tuning AND the unseen period.")
    else:
        lines.append("❌ <b>No rule set held up on the unseen period.</b>")
    if overridden:
        lines.append(f"Live strategy stays <b>{esc(active)}</b> (set by you in the XAU_/BTC_STRATEGY variable).")
    elif w:
        lines.append(f"➡️ Live signals now use <b>{esc(active)}</b>" +
                     (f" (was {esc(switched_from)})." if switched_from and switched_from != active else "."))
    else:
        lines.append(f"➡️ Signals continue as 📝 <b>PAPER</b> signals (strategy {esc(active)}), for testing only.")
    lines.append("<i>A pass is evidence, not proof: paper-trade ~2 weeks before real money. "
                 "Full table: GitHub → Actions → this run → Artifacts.</i>")
    return "\n".join(lines)


def news_digest(items: list[dict], cfg: Config, hours: int, now: datetime) -> str:
    lines = [f"📰 <b>News digest</b> · last {hours}h · {when(now, cfg, both=False)}"]
    for it in items:
        sc = it.get("score", {})
        if it["kind"] == "crypto":
            tag = f"BTC {ARROWS.get(sc.get('btc', 0), '•')}"
        else:
            tag = f"Gold {ARROWS.get(sc.get('xau', 0), '•')} BTC {ARROWS.get(sc.get('btc', 0), '•')}"
        link = f" · <a href=\"{esc(it['link'])}\">link</a>" if it.get("link") else ""
        lines.append(f"• <b>{tag}</b> {esc(it['title'][:160])} — <i>{esc(it['source'])} "
                     f"{when(it['published'], cfg, both=False)}</i>{link}")
    lines.append("<i>▲ bullish · ▼ bearish · • neutral/unclear (keyword rules, not advice)</i>")
    return "\n".join(lines)
