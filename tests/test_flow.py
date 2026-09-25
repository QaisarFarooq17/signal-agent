"""End-to-end runs of the agent with fake market data, fake Claude and a dry-run Telegram."""
import json
from datetime import timedelta

import pandas as pd
import pytest

from agent import backtest as bt
from agent import main as agent_main
from agent import news
from agent.config import Config
from agent.data import split_closed
from agent.strategy import build_context
from agent.telegram import Telegram, strip_tags
from tests.synthetic import frames


class Block:
    def __init__(self, text, citations=None):
        self.type, self.text, self.citations = "text", text, citations or []


class Cite:
    def __init__(self, url, title):
        self.url, self.title = url, title


class Resp:
    def __init__(self, blocks, stop="end_turn"):
        self.content, self.stop_reason = blocks, stop


class FakeClaude:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        prompt = kw["messages"][0]["content"]
        if kw.get("tools"):
            assert kw["tools"][0]["name"] == "web_search"
            return Resp([Block("<b>🌅 London open brief</b>\n**What's driving markets**\n• Dollar softer <script>x</script>"),
                         Block(" after weak data.", [Cite("https://www.reuters.com/markets/x", "Reuters: Dollar slips")])])
        if "Score every item" in prompt:
            items = json.loads(prompt.split("Items:\n", 1)[1])
            return Resp([Block(json.dumps([{"id": it["id"], "xau": 1, "btc": -1, "impact": 4,
                                            "summary": "Fed official signals patience"} for it in items]))])
        if prompt.startswith("Signal:"):
            return Resp([Block('Sure: {"verdict": "caution", "reason": "US data due within the holding window"}')])
        return Resp([Block("OK")])


class FakeTelegram(Telegram):
    def __init__(self, outbox, commands=None):
        super().__init__(None, "-100123", dry_run=True, outbox=outbox)
        self.commands = list(commands or [])

    def get_updates(self, offset):
        if not self.commands:
            return []
        cmd = self.commands.pop(0)
        return [{"update_id": 10, "message": {"chat": {"id": -100123}, "text": cmd}}]


def make_fetch(data):
    def fetch(spec, cfg, now=None, session=None):
        m15, h1, h4 = data[spec.key]
        out = {}
        for tf, df in (("M15", m15), ("H1", h1), ("H4", h4)):
            started = df[df.index < now]
            out[tf], last = split_closed(started, tf, now)
            if tf == "M15":
                out["price"] = last
        out["source"] = "fake"
        return out
    return fetch


@pytest.fixture(scope="module")
def market():
    xau = frames(40, seed=5)
    btc = tuple(df * 20 for df in frames(40, seed=9, start_price=4200))  # BTC-like prices
    return {"XAUUSD": xau, "BTCUSD": btc}


@pytest.fixture
def cfg(tmp_path):
    return Config(state_dir=str(tmp_path / "state"), telegram_chat_id="-100123", anthropic_key="x", dry_run=True,
                  send_charts=True)


def _first_signal_time(market, cfg):
    m15, h1, h4 = market["XAUUSD"]
    trades, _ = bt.run(build_context(m15, h1, h4, cfg.specs["XAUUSD"]), cfg)
    assert trades, "synthetic data should produce at least one backtest signal"
    return pd.Timestamp(trades[len(trades) // 2]["bar_time"])


def test_tick_sequence(market, cfg, tmp_path, monkeypatch):
    t_sig = _first_signal_time(market, cfg)
    start = t_sig - timedelta(minutes=45) + timedelta(minutes=3)
    event_time = start + timedelta(minutes=40)
    monkeypatch.setattr(news, "fetch_calendar", lambda session=None: [
        {"title": "Core CPI m/m", "country": "USD", "time": event_time.isoformat(), "impact": "High",
         "forecast": "0.3%", "previous": "0.4%"}])

    def fake_headlines(now, max_age_hours=12, session=None):
        it = {"id": "h1", "source": "FXStreet", "title": "Fed's Waller: no rush to cut", "summary": "…",
              "link": "https://www.fxstreet.com/a", "published": (start - timedelta(minutes=10)).isoformat(), "kind": "macro"}
        return [it], {"FXStreet": "ok (1 relevant)"}
    monkeypatch.setattr(news, "fetch_headlines", fake_headlines)

    claude = FakeClaude()
    fetch = make_fetch(market)
    sent = []
    outbox = str(tmp_path / "outbox")
    for k in range(0, 4 * 30):  # 30 hours of 15-minute ticks
        now = (start + timedelta(minutes=15 * k)).to_pydatetime()
        cmds = {2: ["/status@my_bot"], 3: ["/check sell xau 1700.5 0.02"], 4: ["/check nonsense"]}.get(k)
        tg = FakeTelegram(outbox, commands=cmds)
        a = agent_main.Agent(cfg, now=now, telegram=tg, llm_client=claude, fetch=fetch)
        a.run("tick")
        a.finish()
        assert not a.errors, a.errors
        sent += [m["text"] for m in tg.sent]

    joined = "\n".join(strip_tags(s) for s in sent)
    assert joined.count("In ~") == 1, "calendar alert must be sent exactly once"
    assert joined.count("Market-moving news") == 1, "news alert must be sent exactly once"
    assert "Market status" in joined
    assert "Position check — SELL XAUUSDm 0.02 @ 1700.50" in joined and "Structure-based stop" in joined
    assert "Usage: /check sell xau" in joined
    assert "BUY XAUUSDm" in joined or "SELL XAUUSDm" in joined or "BUY BTCUSDm" in joined or "SELL BTCUSDm" in joined
    assert "Claude check" in joined
    assert any(w in joined for w in ("hit TP1", "stopped out", "hit TP2", "expired", "back to entry"))

    st = json.load(open(f"{cfg.state_dir}/state.json"))
    assert st["next_id"] >= 2
    assert st["closed_signals"] or st["open_signals"]
    # Every signal message must stay within Telegram's caption/message limits.
    assert all(len(strip_tags(s)) < 4096 for s in sent)


def test_brief_and_weekly_and_backtest(market, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(news, "fetch_calendar", lambda session=None: [])
    monkeypatch.setattr(news, "fetch_headlines", lambda now, max_age_hours=12, session=None: ([], {}))
    now = (market["XAUUSD"][0].index[-1] + timedelta(minutes=20)).to_pydatetime()

    def history(spec, cfg):
        m15, h1, h4 = market[spec.key]
        return {"M15": m15, "H1": h1, "H4": h4}

    tg = FakeTelegram(str(tmp_path / "o"))
    a = agent_main.Agent(cfg, now=now, telegram=tg, llm_client=FakeClaude(), fetch=make_fetch(market), history=history)
    a.run("brief-london")
    a.run_weekly()
    a.finish()
    assert not a.errors, a.errors
    texts = [m["text"] for m in tg.sent]
    brief = next(t for t in texts if "London open brief" in t)
    assert "<script>" not in brief and "&lt;script&gt;" in brief
    assert "<b>What's driving markets</b>" in brief
    assert 'href="https://www.reuters.com/markets/x"' in brief
    assert any("Weekly report" in t for t in texts)
    assert sum("Backtest" in t for t in texts) == 2
    st = json.load(open(f"{cfg.state_dir}/state.json"))
    assert set(st["calibration"]) == {"XAUUSD", "BTCUSD"}


def test_connection_test_mode(cfg, tmp_path, monkeypatch):
    import agent.main as m
    idx = pd.date_range("2026-09-25 10:00", periods=3, freq="15min", tz="UTC")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [4260.1, 4261.2, 4262.4], "volume": 0.0}, index=idx)
    monkeypatch.setattr(m, "fetch_twelvedata", lambda *a, **k: df)
    monkeypatch.setattr(m, "fetch_kraken", lambda *a, **k: df * 20)
    monkeypatch.setattr(news, "fetch_calendar", lambda session=None: (_ for _ in ()).throw(RuntimeError("blocked")))
    monkeypatch.setattr(news, "fetch_headlines", lambda now, max_age_hours=12, session=None: ([], {"CoinDesk": "ok (3 relevant)"}))
    tg = FakeTelegram(str(tmp_path / "o"))
    a = agent_main.Agent(cfg, telegram=tg, llm_client=FakeClaude())
    a.run("test")
    out = strip_tags(tg.sent[0]["text"])
    assert "Twelve Data (gold): XAU/USD last M15 close 4262.40" in out
    assert "❌ Economic calendar: blocked" in out
    assert "✅ Claude API: ok" in out


def test_secrets_are_redacted(cfg):
    c = Config(state_dir=cfg.state_dir, twelvedata_key="SECRETKEY123", telegram_token="123:ABC")
    a = agent_main.Agent(c, telegram=FakeTelegram(None))
    a.fail("x", RuntimeError("GET /time_series?apikey=SECRETKEY123 and bot123:ABC/sendMessage"))
    assert "SECRETKEY123" not in a.errors[0] and "123:ABC" not in a.errors[0]


def test_free_mode_without_ai_key(market, tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path / "s2"), telegram_chat_id="-100123", anthropic_key=None, dry_run=True)
    ev_t = market["XAUUSD"][0].index[-1] + timedelta(hours=3)
    monkeypatch.setattr(news, "fetch_calendar", lambda session=None: [
        {"title": "Non-Farm Employment Change", "country": "USD", "time": ev_t.isoformat(), "impact": "High",
         "forecast": "", "previous": ""}])
    now = (market["XAUUSD"][0].index[-1] + timedelta(minutes=20)).to_pydatetime()
    monkeypatch.setattr(news, "fetch_headlines", lambda now_, max_age_hours=12, session=None: ([{
        "id": "a1", "source": "CoinDesk", "title": "Bitcoin ETF inflows hit record", "summary": "",
        "link": "https://www.coindesk.com/x", "published": (now - timedelta(hours=1)).isoformat(), "kind": "crypto"}], {}))
    tg = FakeTelegram(str(tmp_path / "o2"))
    a = agent_main.Agent(cfg, now=now, telegram=tg, fetch=make_fetch(market))
    assert not a.llm.available
    a.run("brief-london")
    a.finish()
    assert not a.errors, a.errors
    brief = strip_tags(next(m["text"] for m in tg.sent if "London open brief" in m["text"]))
    assert "bias" in brief and "Plan:" in brief and "Non-Farm Employment Change" in brief
    assert "Bitcoin ETF inflows hit record" in brief and "BTC ▲▲" in brief
    assert "Free mode" in brief
