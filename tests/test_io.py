"""Parsing of the real API response formats (mocked HTTP)."""
from datetime import datetime, timezone

from agent import news
from agent.data import fetch_kraken, fetch_twelvedata, split_closed
from agent.telegram import Telegram, chunks


class R:
    def __init__(self, payload=None, content=b"", status=200, ctype="application/json"):
        self._p, self.content, self.status_code = payload, content, status
        self.headers = {"content-type": ctype}

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class S:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)

    post = get


def test_twelvedata_parse():
    payload = {"meta": {"symbol": "XAU/USD", "interval": "15min"}, "status": "ok", "values": [
        {"datetime": "2026-09-25 14:00:00", "open": "4260.1", "high": "4263.0", "low": "4258.2", "close": "4262.4"},
        {"datetime": "2026-09-25 14:15:00", "open": "4262.4", "high": "4264.9", "low": "4261.0", "close": "4263.3"}]}
    df = fetch_twelvedata("XAU/USD", "M15", 2, "k", S([R(payload)]))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC" and df["close"].iloc[-1] == 4263.3 and df["volume"].iloc[0] == 0
    closed, last = split_closed(df, "M15", datetime(2026, 9, 25, 14, 20, tzinfo=timezone.utc))
    assert len(closed) == 1 and last == 4263.3


def test_twelvedata_error_message():
    try:
        fetch_twelvedata("XAU/USD", "M15", 2, "k", S([R({"status": "error", "code": 401, "message": "bad key"})]))
    except Exception as e:  # noqa: BLE001
        assert "401" in str(e) and "bad key" in str(e)
    else:
        raise AssertionError("expected an error")


def test_kraken_parse():
    rows = [[1790000100 + 900 * k, "84000.1", "84100.0", "83900.0", "84050.5", "84010.0", "3.2", 120] for k in range(3)]
    df = fetch_kraken("XBTUSD", "M15", S([R({"error": [], "result": {"XXBTZUSD": rows, "last": 1790001900}})]))
    assert len(df) == 3 and df["close"].iloc[0] == 84050.5 and str(df.index.tz) == "UTC"


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Gold climbs as dollar slides after soft PCE</title><link>https://x.com/a</link>
<pubDate>Fri, 25 Sep 2026 13:40:00 GMT</pubDate><description>Bullion up 1%</description></item>
<item><title>Local sports result</title><link>https://x.com/b</link><pubDate>Fri, 25 Sep 2026 13:30:00 GMT</pubDate></item>
<item><title>Old gold story</title><link>https://x.com/c</link><pubDate>Mon, 21 Sep 2026 13:30:00 GMT</pubDate></item>
</channel></rss>"""


def test_rss_and_rules(monkeypatch):
    monkeypatch.setattr(news, "RSS_FEEDS", [("FXStreet", "u", "macro"), ("CoinDesk", "v", "crypto")])
    now = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)
    items, status = news.fetch_headlines(now, session=S([R(content=RSS, ctype="text/xml"), R(status=500)]))
    titles = [i["title"] for i in items]
    assert titles == ["Gold climbs as dollar slides after soft PCE"]  # keyword filter + age filter
    assert status["FXStreet"].startswith("ok") and status["CoinDesk"].startswith("error")
    sc = news.rule_score(items[0])
    assert sc["xau"] > 0
    items[0]["score"] = {"xau": 2, "btc": 0, "impact": 5}
    assert news.news_bias(items, "XAU", now) > 0.5 and news.news_bias(items, "BTC", now) == 0


def test_calendar_parse_and_blackout():
    from agent.config import Config
    payload = [{"title": "CPI m/m", "country": "USD", "date": "2026-09-25T08:30:00-04:00", "impact": "High",
                "forecast": "0.3%", "previous": "0.4%"},
               {"title": "German Ifo", "country": "EUR", "date": "2026-09-25T04:00:00-04:00", "impact": "High"}]
    ev = news.fetch_calendar(S([R(payload)]))
    assert ev[0]["time"].startswith("2026-09-25T12:30:00")
    cfg = Config()
    assert news.blackout_reason(ev, cfg, datetime(2026, 9, 25, 12, 10, tzinfo=timezone.utc))
    assert news.blackout_reason(ev, cfg, datetime(2026, 9, 25, 13, 10, tzinfo=timezone.utc)) is None
    assert news.blackout_reason(ev, cfg, datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)) is None  # EUR ignored


def test_telegram_fallback_and_chunks():
    tg = Telegram("123:ABC", "-100", dry_run=False)
    tg.session = S([R({"ok": False, "description": "Bad Request: can't parse entities"}, status=400),
                    R({"ok": True})])
    assert tg.send("<b>broken <i>html</b>")
    assert "parse_mode" not in tg.session.calls[1][1]["data"]
    parts = chunks("\n".join(["x" * 100] * 100), 4000)
    assert all(len(p) <= 4000 for p in parts) and sum(len(p) for p in parts) >= 100 * 100


def test_brief_pause_turn_and_tool_fallback():
    from agent.config import Config
    from agent.llm import LLM
    from tests.test_flow import Block, Cite, Resp

    class Err(Exception):
        status_code = 400

    class C:
        def __init__(self):
            self.messages, self.tools_seen, self.n = self, [], 0

        def create(self, **kw):
            self.tools_seen.append(kw["tools"][0]["type"])
            if kw["tools"][0]["type"] != "web_search_20250305":
                raise Err("unknown tool version")
            self.n += 1
            if self.n == 1:
                return Resp([Block("partial")], stop="pause_turn")
            assert kw["messages"][-1]["role"] == "assistant"
            return Resp([Block("<b>Brief</b> done", [Cite("https://www.cnbc.com/a", "CNBC")])])

    cfg = Config(anthropic_key="x", web_search_tool="web_search_20260318")
    c = C()
    text, sources = LLM(cfg, {}, client=c).brief("Brief", "ctx")
    assert text == "<b>Brief</b> done" and sources == [("CNBC", "https://www.cnbc.com/a")]
    assert c.tools_seen[0] == "web_search_20260318" and c.tools_seen[1] == "web_search_20250305"


def test_brief_drops_preamble_before_searches():
    from agent.config import Config
    from agent.llm import LLM
    from tests.test_flow import Block, Cite, Resp

    class Tool:
        def __init__(self, t):
            self.type = t

    class C:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            return Resp([Block("I'll search for the latest news."), Tool("server_tool_use"),
                         Tool("web_search_tool_result"), Block("<b>Brief</b>\n• Gold firm", [Cite("https://www.reuters.com/z", "Reuters")])])

    text, sources = LLM(Config(anthropic_key="x"), {}, client=C()).brief("Brief", "ctx")
    assert text.startswith("<b>Brief</b>") and "I'll search" not in text and sources[0][1] == "https://www.reuters.com/z"


def test_telegram_follows_supergroup_upgrade():
    tg = Telegram("123:ABC", "-5151282813", dry_run=False)
    tg.session = S([R({"ok": False, "error_code": 400, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                       "parameters": {"migrate_to_chat_id": -1001234567890}}, status=400),
                    R({"ok": True})])
    assert tg.send("hello")
    assert tg.chat_id == "-1001234567890" and tg.migrated_to == "-1001234567890"
    assert tg.session.calls[1][1]["data"]["chat_id"] == "-1001234567890"
