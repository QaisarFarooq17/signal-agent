"""Entry point.

    python -m agent.main tick           # every 15 min: news, calendar, tracking, signals, /commands
    python -m agent.main brief-london   # tick + Claude web-search brief before London
    python -m agent.main brief-ny       # tick + Claude web-search brief before New York
    python -m agent.main weekly         # tick + weekly scoreboard + backtest refresh
    python -m agent.main backtest       # backtest only (posts results)
    python -m agent.main status         # tick + post a market status snapshot
    python -m agent.main test           # check every connection and post a report
Add --dry-run to print messages instead of sending them.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import backtest as bt
from . import messages as msg
from . import news
from . import state as state_mod
from .charts import signal_chart
from .config import Config
from .data import fetch_kraken, fetch_twelvedata, get_history, get_live_candles
from .llm import LLM, sanitize_html
from .strategy import build_context, evaluate_at, key_levels, summarize, summary_text
from .telegram import Telegram, strip_tags
from .tracker import advance, init_tracking, stats

log = logging.getLogger("agent")
MODES = ("tick", "brief-london", "brief-ny", "weekly", "backtest", "status", "test")

HELP = ("🤖 <b>Signal agent commands</b>\n"
        "/status – live trend, scores and key levels for XAU and BTC\n"
        "/score – running scoreboard of all signals\n"
        "/brief – fresh Claude news brief (max 3 per day)\n"
        "/check sell xau 4246.49 0.01 – how the market looks for a position you already hold\n"
        "/help – this message\n"
        "<i>The agent wakes every ~15 minutes on weekdays, so replies can take up to 15 minutes.</i>")


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def fromiso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Agent:
    def __init__(self, cfg: Config, now: datetime | None = None, telegram: Telegram | None = None,
                 llm_client=None, fetch=None, session=None, history=None):
        self.cfg = cfg
        self.now = now or datetime.now(timezone.utc)
        self.state = state_mod.load(cfg.state_dir)
        self.tg = telegram or Telegram(cfg.telegram_token, cfg.telegram_chat_id, cfg.dry_run,
                                       outbox=os.path.join(cfg.state_dir, "outbox") if cfg.dry_run else None)
        if telegram is None and self.state.get("chat_id_override"):
            self.tg.chat_id = self.state["chat_id_override"]   # group was upgraded to a supergroup
        self.llm = LLM(cfg, self.state, client=llm_client)
        self.fetch = fetch or get_live_candles
        self.history = history or get_history
        self.session = session
        self.ctx, self.evals, self.summaries, self.live_price, self.bias = {}, {}, {}, {}, {}
        self.errors: list[str] = []
        self.feed_status: dict = {}

    # ------------------------------------------------------------------ helpers
    def redact(self, text: str) -> str:
        for secret in (self.cfg.telegram_token, self.cfg.twelvedata_key, self.cfg.anthropic_key):
            if secret:
                text = text.replace(secret, "***")
        return text

    def fail(self, where: str, e: Exception) -> None:
        log.exception("%s failed", where)
        self.errors.append(self.redact(f"{where}: {type(e).__name__}: {e}")[:300])

    def local_day(self) -> str:
        return self.now.astimezone(ZoneInfo(self.cfg.display_tz)).strftime("%a %d %b")

    def events(self) -> list[dict]:
        return self.state["calendar"]["events"]

    # ------------------------------------------------------------------ calendar
    def step_calendar(self) -> None:
        cal = self.state["calendar"]
        fetched = cal.get("fetched_at")
        if not fetched or self.now - fromiso(fetched) > timedelta(minutes=55):
            try:
                cal["events"] = news.fetch_calendar(self.session)
                cal["fetched_at"] = iso(self.now)
            except Exception as e:  # noqa: BLE001
                if not cal["events"]:
                    self.fail("calendar", e)
                else:
                    log.warning("calendar refresh failed, using cached copy: %s", e)
        for e in news.relevant_events(cal["events"], self.cfg):
            key = e["time"] + "|" + e["title"]
            mins = (fromiso(e["time"]) - self.now).total_seconds() / 60
            if 0 < mins <= 50 and key not in cal["alerted"]:
                self.tg.send(msg.event_alert(e, self.cfg, int(round(mins))))
                cal["alerted"].append(key)

    # ------------------------------------------------------------------ news
    def step_news(self) -> None:
        try:
            items, self.feed_status = news.fetch_headlines(self.now, session=self.session)
        except Exception as e:  # noqa: BLE001
            self.fail("news", e)
            items = []
        store = self.state["news"]
        new = [it for it in items if it["id"] not in store["seen"]]
        for it in new:
            store["seen"][it["id"]] = iso(self.now)
        if new:
            scores = self.llm.score_headlines(new) if self.llm.available else {}
            for it in new:
                it["score"] = scores.get(it["id"]) or news.rule_score(it)
            store["items"].extend(new)
            fresh = [it for it in new
                     if self.now - fromiso(it["published"]) <= timedelta(minutes=90)
                     and it["score"]["impact"] >= self.cfg.news_alert_min_impact
                     and (it["score"]["xau"] or it["score"]["btc"])]
            fresh.sort(key=lambda it: -it["score"]["impact"])
            if fresh:
                self.tg.send(msg.news_alert(fresh[:5], self.cfg))
        for key, spec in self.cfg.specs.items():
            self.bias[key] = news.news_bias(store["items"], spec.asset, self.now)

    def headlines_text(self, hours: float = 12, limit: int = 10, min_impact: int = 2) -> str:
        cut = self.now - timedelta(hours=hours)
        items = [it for it in self.state["news"]["items"]
                 if fromiso(it["published"]) >= cut and it.get("score", {}).get("impact", 1) >= min_impact]
        items.sort(key=lambda it: (it["score"]["impact"], it["published"]), reverse=True)
        return "\n".join(
            f"[{it['source']} {msg.when(it['published'], self.cfg, both=False)}] {it['score'].get('summary') or it['title']} "
            f"(gold {it['score']['xau']:+d}, btc {it['score']['btc']:+d}, impact {it['score']['impact']})"
            for it in items[:limit])

    def events_text(self, hours: float) -> str:
        return "\n".join(
            f"{msg.when(e['time'], self.cfg, both=False)} {e['country']} {e['title']} (impact {e['impact']}, "
            f"forecast {e['forecast'] or 'n/a'}, previous {e['previous'] or 'n/a'})"
            for e in news.upcoming(self.events(), self.cfg, self.now, hours))

    # ------------------------------------------------------------------ symbols
    def step_symbols(self) -> None:
        for key, spec in self.cfg.specs.items():
            try:
                d = self.fetch(spec, self.cfg, now=self.now)
                ctx = build_context(d["M15"], d["H1"], d["H4"], spec)
                self.ctx[key] = ctx
                self.live_price[key] = d.get("price")
                self.track(key, spec, ctx)
                self.evaluate(key, spec, ctx)
            except Exception as e:  # noqa: BLE001
                self.fail(key, e)

    def track(self, key, spec, ctx) -> None:
        still_open = []
        for sig in self.state["open_signals"]:
            if sig["symbol"] != key:
                still_open.append(sig)
                continue
            m = ctx.m[ctx.m.index > fromiso(sig["last_checked"])]
            bars = list(zip((t.isoformat() for t in m.index), m["high"], m["low"], m["close"]))
            events = advance(sig, bars, int(self.cfg.signal_expiry_hours * 4), spec.spread)
            for ev, price, _t in events:
                self.tg.send(msg.track_message(sig, spec, ev, price), silent=(ev == "tp1"))
            if sig["status"] == "closed":
                self.state["closed_signals"].append(sig)
                self.state["last_close"][key] = sig["closed_at"]
            else:
                still_open.append(sig)
        self.state["open_signals"] = still_open

    def evaluate(self, key, spec, ctx) -> None:
        m, n = ctx.m, len(ctx.m)
        if n < 60:
            raise RuntimeError(f"only {n} M15 bars available")
        bias = self.bias.get(key, 0.0)
        blackout = news.blackout_reason(self.events(), self.cfg, self.now)
        latest = evaluate_at(ctx, n - 1, self.cfg, bias, blackout)
        self.evals[key] = latest
        self.summaries[key] = summarize(ctx, n - 1, latest, self.live_price.get(key))

        last_eval = self.state["last_eval_bar"].get(key)
        candidates = [i for i in (n - 1, n - 2)
                      if last_eval is None or m.index[i] > fromiso(last_eval)]
        self.state["last_eval_bar"][key] = m.index[n - 1].isoformat()

        if any(s["symbol"] == key for s in self.state["open_signals"]):
            return
        lc = self.state["last_close"].get(key)
        if lc and self.now - fromiso(lc) < timedelta(minutes=self.cfg.cooldown_minutes):
            return
        for i in candidates:
            ev = latest if i == n - 1 else evaluate_at(ctx, i, self.cfg, bias, blackout)
            sig = ev.signal
            if not sig:
                continue
            close_t = m.index[i] + timedelta(minutes=15)
            if self.now - close_t > timedelta(minutes=45):
                log.info("%s signal on %s is stale, skipped", key, close_t)
                continue
            s = 1 if sig["side"] == "BUY" else -1
            price = self.live_price.get(key)
            if price is not None and (s * (price - sig["entry"]) > 0.5 * sig["atr"] or s * (price - sig["sl"]) <= 0):
                log.info("%s signal missed: price %.2f already moved away from entry %.2f", key, price, sig["entry"])
                continue
            self.publish(sig, spec, ctx, bias)
            break

    def publish(self, sig: dict, spec, ctx, bias: float) -> None:
        review = None
        if self.cfg.llm_review_signals and self.llm.available:
            review = self.llm.review_signal(sig, summary_text(self.summaries[spec.key]),
                                            self.headlines_text(), self.events_text(6))
            if review and review["verdict"] == "reject" and self.cfg.llm_can_veto:
                log.info("signal vetoed by Claude: %s", review["reason"])
                return
        sig["id"] = self.state["next_id"]
        self.state["next_id"] += 1
        sig["created"] = iso(self.now)
        sig["review"] = review
        sig["news_bias"] = bias
        init_tracking(sig)
        self.state["open_signals"].append(sig)
        calib = (self.state.get("calibration", {}).get(spec.key) or {}).get(sig["grade"])
        text = msg.signal_message(sig, spec, self.cfg, bias, review, calib)
        png = (signal_chart(ctx.m, sig, f"{spec.display} M15 · {sig['side']} #{sig['id']}", decimals=spec.decimals)
               if self.cfg.send_charts else None)
        if png and len(strip_tags(text)) <= 1000:
            self.tg.send_photo(png, text)
        elif png:
            self.tg.send_photo(png, msg.short_caption(sig, spec))
            self.tg.send(text)
        else:
            self.tg.send(text)
        log.info("signal #%s %s %s score %s", sig["id"], sig["side"], spec.key, sig["score"])

    # ------------------------------------------------------------------ commands
    def step_commands(self) -> None:
        updates = self.tg.get_updates(self.state.get("tg_offset"))
        wanted, checks = [], []
        for u in updates:
            self.state["tg_offset"] = u["update_id"] + 1
            m = u.get("message") or u.get("channel_post") or {}
            if str(m.get("chat", {}).get("id", "")) not in (str(self.cfg.telegram_chat_id), str(self.tg.chat_id)):
                continue
            words = (m.get("text") or "").strip().split()
            if words and words[0].startswith("/"):
                cmd = words[0].split("@")[0].lower()
                if cmd == "/check":
                    checks.append(words[1:])
                elif cmd not in wanted:
                    wanted.append(cmd)
        for cmd in wanted:
            if cmd in ("/status", "/now"):
                self.send_status()
            elif cmd == "/score":
                self.tg.send(msg.scoreboard("Scoreboard — all signals", stats(self.state["closed_signals"]),
                                            self.state["open_signals"]))
            elif cmd == "/brief":
                today = self.now.strftime("%Y-%m-%d")
                used = self.state["cmd_brief"].get(today, 0)
                if used >= 3:
                    self.tg.send("You've used today's 3 on-demand briefs. The next scheduled one will arrive as usual.")
                else:
                    self.state["cmd_brief"] = {today: used + 1}
                    self.run_brief(f"⚡ On-demand brief — {msg.when(self.now, self.cfg, both=False)}")
            elif cmd in ("/help", "/start"):
                self.tg.send(HELP)
        for args in checks[:5]:
            self.check_position(args)

    def check_position(self, args: list[str]) -> None:
        """/check sell xau 4246.49 [0.01] -> how the market looks for a position you already hold."""
        alias = {"xau": "XAUUSD", "gold": "XAUUSD", "xauusd": "XAUUSD", "xauusdm": "XAUUSD",
                 "btc": "BTCUSD", "bitcoin": "BTCUSD", "btcusd": "BTCUSD", "btcusdm": "BTCUSD"}
        usage = "Usage: <code>/check sell xau 4246.49 0.01</code> (side, symbol, entry price, lots)"
        try:
            side = args[0].upper()
            key = alias[args[1].lower()]
            entry = float(args[2].replace(",", "."))
            lots = float(args[3].replace(",", ".")) if len(args) > 3 else 0.01
            assert side in ("BUY", "SELL") and lots > 0
        except (IndexError, KeyError, ValueError, AssertionError):
            self.tg.send(usage)
            return
        if key not in self.ctx or key not in self.summaries:
            self.tg.send(f"No live data for {key} this run — try again in 15 minutes.")
            return
        spec, ctx, summ = self.cfg.specs[key], self.ctx[key], self.summaries[key]
        i = len(ctx.m) - 1
        price = self.live_price.get(key) or summ["price"]
        s = 1 if side == "BUY" else -1
        atr_h1 = summ["atr_h1"] or summ["atr_m15"] * 2
        # key levels on the losing side, at least half an H1 ATR away (so the stop is not inside the noise)
        against = [p for n, p in key_levels(ctx, i) if -s * (p - price) > 0.5 * atr_h1]
        if against:
            nearest = min(against, key=lambda p: abs(p - price))
            stop = nearest - s * 0.3 * atr_h1
        else:
            stop = price - s * 1.5 * atr_h1
        ev = self.evals.get(key)
        mine = ev.buy if s > 0 else ev.sell
        other = ev.sell if s > 0 else ev.buy
        events = news.upcoming(self.events(), self.cfg, self.now, 12, impacts=("High",))
        self.tg.send(msg.position_check(spec, self.cfg, side, entry, lots, price, summ, mine.trend_pts,
                                        other.trend_pts, stop, events))

    def send_status(self) -> None:
        sums = [self.summaries[k] for k in self.cfg.specs if k in self.summaries]
        if not sums:
            self.tg.send("⚠️ No market data this run — see the error notice.")
            return
        self.tg.send(msg.status_message(sums, self.bias, self.cfg, self.now, self.state["open_signals"],
                                        news.upcoming(self.events(), self.cfg, self.now, 12)))

    # ------------------------------------------------------------------ briefs
    def brief_context(self, title: str) -> str:
        parts = [f"NOW: {msg.when(self.now, self.cfg)} — {self.now:%A %d %B %Y}. Brief: {title}",
                 "", "TECHNICAL SNAPSHOT (computed from live candles; use these for levels):"]
        parts += [summary_text(self.summaries[k]) for k in self.cfg.specs if k in self.summaries] or ["unavailable"]
        parts += ["", "ECONOMIC CALENDAR, next 24h (Rome time):", self.events_text(24) or "no medium/high-impact events"]
        parts += ["", "HEADLINES, last 12h, pre-scored:", self.headlines_text(12, 12) or "none"]
        parts += ["", "NEWS BIAS (-1..+1): " + ", ".join(f"{k} {v:+.2f}" for k, v in self.bias.items())]
        if self.state["open_signals"]:
            parts += ["", "OPEN SIGNALS: " + "; ".join(
                f"#{o['id']} {o['side']} {o['symbol']} entry {o['entry']} SL {o['sl_current']} TP2 {o['tp2']}"
                for o in self.state["open_signals"])]
        return "\n".join(parts)

    def run_brief(self, title: str) -> None:
        res = self.llm.brief(title, self.brief_context(title)) if self.llm.available else None
        if not res:
            # Free mode (no AI key) or AI failure: rule-based brief from candles, calendar and headlines.
            cut = self.now - timedelta(hours=12)
            items = [it for it in self.state["news"]["items"] if fromiso(it["published"]) >= cut]
            items.sort(key=lambda it: (it.get("score", {}).get("impact", 1), it["published"]), reverse=True)
            self.tg.send(msg.free_brief(title, [self.summaries[k] for k in self.cfg.specs if k in self.summaries],
                                        news.upcoming(self.events(), self.cfg, self.now, 24), items[:6], self.bias,
                                        self.cfg, ai_failed=self.llm.available))
            return
        text, sources = res
        body = sanitize_html(text)
        if sources:
            body += "\n\n<b>Sources</b>\n" + "\n".join(
                f"• <a href=\"{msg.esc(u)}\">{msg.esc(t[:70])}</a>" for t, u in sources)
        body += "\n<i>Bias, not certainty · not financial advice</i>"
        self.tg.send(body)

    # ------------------------------------------------------------------ weekly / backtest
    def run_weekly(self) -> None:
        week_ago = iso(self.now - timedelta(days=7))
        closed = self.state["closed_signals"]
        self.tg.send(msg.scoreboard("Weekly report — last 7 days",
                                    stats([s for s in closed if (s.get("closed_at") or "") >= week_ago]),
                                    self.state["open_signals"]))
        self.tg.send(msg.scoreboard("All-time scoreboard", stats(closed), []))
        self.run_backtest()

    def run_backtest(self) -> None:
        for key, spec in self.cfg.specs.items():
            try:
                d = self.history(spec, self.cfg)
                ctx = build_context(d["M15"], d["H1"], d["H4"], spec)
                trades, res = bt.run(ctx, self.cfg)
                bt.save_csv(trades, os.path.join(self.cfg.state_dir, f"backtest_{key}.csv"))
                self.state["calibration"][key] = {"A": res["A"], "B": res["B"], "all": res["all"],
                                                  "period": res["period"], "updated": iso(self.now)}
                self.tg.send(msg.backtest_message(spec, res, self.cfg))
            except Exception as e:  # noqa: BLE001
                self.fail(f"backtest {key}", e)

    # ------------------------------------------------------------------ test
    def run_test(self) -> None:
        cfg = self.cfg
        lines = ["🔧 <b>Connection test</b>"]

        def check(name, fn):
            try:
                lines.append(f"✅ {name}: {msg.esc(fn())}")
            except Exception as e:  # noqa: BLE001
                lines.append(f"❌ {name}: {msg.esc(self.redact(str(e))[:160])}")

        lines.append(f"{'✅' if cfg.telegram_token and cfg.telegram_chat_id else '❌'} Telegram: "
                     f"{'this message arrived, so it works' if cfg.telegram_token and cfg.telegram_chat_id else 'token or chat id missing'}")
        check("Twelve Data (gold)", lambda: f"XAU/USD last M15 close {fetch_twelvedata('XAU/USD', 'M15', 3, cfg.twelvedata_key)['close'].iloc[-1]:.2f}")
        check("Kraken (BTC)", lambda: f"BTC/USD last M15 close {fetch_kraken('XBTUSD', 'M15')['close'].iloc[-1]:.1f}")

        def cal():
            ev = news.fetch_calendar(self.session)
            hi = news.upcoming(ev, cfg, self.now, 24 * 7, impacts=("High",))
            nxt = f"; next high-impact: {hi[0]['country']} {hi[0]['title']} {msg.when(hi[0]['time'], cfg, both=False)}" if hi else ""
            return f"{len(ev)} events this week{nxt}"
        check("Economic calendar", cal)
        items, status = news.fetch_headlines(self.now, session=self.session)
        for name, s in status.items():
            lines.append(f"{'✅' if s.startswith('ok') else '⚠️'} News · {msg.esc(name)}: {msg.esc(s)}")
        check("Claude API", lambda: self.llm.ping())
        lines += ["", "<b>Settings</b>",
                  f"Symbols {', '.join(s.display for s in cfg.specs.values())} · risk {cfg.risk_pct:g}% of "
                  f"${cfg.account_balance:,.0f} · min score {cfg.min_score} · sells {'on' if cfg.allow_sells else 'off'}",
                  f"Models: {cfg.model_fast} (headlines), {cfg.model_smart} (briefs, reviews)",
                  f"Times shown in {cfg.display_tz} and MT5 server time (UTC)."]
        self.tg.send("\n".join(lines))
        self.tg.send(HELP)

    # ------------------------------------------------------------------ run
    def run(self, mode: str) -> None:
        if mode == "test":
            self.run_test()
            return
        if mode == "backtest":
            self.run_backtest()
            return
        self.step_calendar()
        self.step_news()
        self.step_symbols()
        self.step_commands()
        if mode == "brief-london":
            self.run_brief(f"🌅 London open brief — {self.local_day()}")
        elif mode == "brief-ny":
            self.run_brief(f"🗽 New York open brief — {self.local_day()}")
        elif mode == "weekly":
            self.run_weekly()
        elif mode == "status":
            self.send_status()

    def finish(self) -> None:
        if self.tg.migrated_to and self.state.get("chat_id_override") != self.tg.migrated_to:
            self.state["chat_id_override"] = self.tg.migrated_to
            self.tg.send("ℹ️ This group was upgraded to a supergroup, so its chat id changed. The agent switched "
                         "automatically. To keep things tidy, update the <code>TELEGRAM_CHAT_ID</code> secret on "
                         f"GitHub to <code>{msg.esc(self.tg.migrated_to)}</code>.")
        if self.errors:
            last = self.state.get("last_error_notice")
            if not last or self.now - fromiso(last) > timedelta(hours=6):
                self.tg.send("⚠️ <b>Agent issue</b> (repeats are muted for 6h)\n" +
                             "\n".join(f"• {msg.esc(e)}" for e in self.errors[:6]), silent=True)
                self.state["last_error_notice"] = iso(self.now)
        state_mod.save(self.cfg.state_dir, self.state, self.now)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="XAU/BTC signal agent")
    p.add_argument("mode", choices=MODES)
    p.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"
    cfg = Config()
    agent = Agent(cfg)
    try:
        agent.run(args.mode)
    except Exception as e:  # noqa: BLE001
        agent.fail(args.mode, e)
    finally:
        agent.finish()
    log.info("done: mode=%s errors=%d", args.mode, len(agent.errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
