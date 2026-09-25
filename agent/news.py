"""Headlines (RSS from official / established outlets) and the economic calendar."""
from __future__ import annotations

import calendar as _cal
import hashlib
import logging
import math
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from .config import CALENDAR_URL, RSS_FEEDS, Config

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (compatible; xau-btc-signal-agent/1.0)"}

MACRO_KEYWORDS = re.compile(
    r"\b(gold|xau|bullion|precious|silver|fed|fomc|powell|rate[s]? (cut|hike)|interest rate|inflation|cpi|pce|ppi|"
    r"payroll|nfp|jobs|jobless|unemployment|yield|treasur|dollar|dxy|greenback|tariff|war|sanction|geopolit|"
    r"central bank|recession|gdp|stimulus|debt ceiling|shutdown|bitcoin|crypto|stablecoin|etf|risk[- ]off|"
    r"safe[- ]haven|missile|ceasefire|opec|oil)\b", re.I)


CRYPTO_KEYWORDS = re.compile(
    r"\b(bitcoin|btc|etf|sec|fed|federal reserve|powell|stablecoin|regulat\w*|hack\w*|exploit|liquidat\w*|"
    r"treasur\w*|blackrock|microstrategy|saylor|halving|miners?|whales?|reserve|tariffs?|inflation|"
    r"rate (cut|hike)s?|outflows?|inflows?|crash\w*|rall(y|ies)|record high|all-time high|sell-?off)\b", re.I)


def digest_relevant(item: dict) -> bool:
    """Macro items are already keyword-filtered; crypto items must be about BTC or the wider market."""
    if item.get("kind") != "crypto":
        return True
    return bool(CRYPTO_KEYWORDS.search(item["title"] + " " + item.get("summary", "")))


def _hash(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode()).hexdigest()[:16]


def _clean(html: str, limit: int = 300) -> str:
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:limit]


def fetch_headlines(now: datetime, max_age_hours: float = 12, session: requests.Session | None = None):
    """Return (items, feed_status). Items: dict(id, source, title, summary, link, published, kind)."""
    session = session or requests.Session()
    items, status = [], {}
    for name, url, kind in RSS_FEEDS:
        try:
            r = session.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            count = 0
            for e in feed.entries[:40]:
                title = _clean(getattr(e, "title", ""), 240)
                if not title:
                    continue
                tp = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                pub = datetime.fromtimestamp(_cal.timegm(tp), tz=timezone.utc) if tp else now
                if now - pub > timedelta(hours=max_age_hours):
                    continue
                summary = _clean(getattr(e, "summary", ""))
                if kind == "macro" and not MACRO_KEYWORDS.search(title + " " + summary):
                    continue
                link = getattr(e, "link", "")
                items.append({"id": _hash(link, title), "source": name, "title": title, "summary": summary,
                              "link": link, "published": pub.isoformat(), "kind": kind})
                count += 1
            status[name] = f"ok ({count} relevant)"
        except Exception as ex:  # noqa: BLE001 - one bad feed must not stop the others
            status[name] = f"error: {str(ex)[:80]}"
            log.warning("feed %s failed: %s", name, ex)
    items.sort(key=lambda x: x["published"], reverse=True)
    return items, status


# ---------------------------------------------------------------- rule-based fallback scoring

_RULES = [
    # (pattern, xau, btc, impact)
    (r"rate cut|dovish|cuts rates|easing|weaker dollar|dollar (falls|slides|weakens)", +1, +1, 3),
    (r"rate hike|hawkish|raises rates|stronger dollar|dollar (rises|jumps|strengthens)|yields (rise|jump|surge)", -1, -1, 3),
    (r"war|missile|attack|invasion|escalat|geopolitic|safe[- ]haven", +2, -1, 4),
    (r"ceasefire|peace (deal|talks)|de-escalat", -1, +1, 3),
    (r"central bank.*(buy|purchas).*gold|gold (reserves|purchases)", +1, 0, 3),
    (r"hot(ter)? (cpi|inflation)|inflation (rises|accelerat|surges)", -1, -1, 4),
    (r"(cpi|inflation) (cools|eases|slows)", +1, +1, 4),
    (r"etf (inflow|approval|approved)|record inflows", 0, +2, 3),
    (r"etf outflow|outflows", 0, -1, 2),
    (r"hack|exploit|stolen|breach", 0, -1, 3),
    (r"\bban\b|crackdown|lawsuit|sues|charges|sec (sues|charges)", 0, -1, 3),
    (r"strategic (bitcoin )?reserve|adopts bitcoin|legal tender", 0, +2, 4),
]


def rule_score(item: dict) -> dict:
    text = (item["title"] + " " + item.get("summary", "")).lower()
    xau = btc = 0
    impact = 1
    for pat, dx, db, imp in _RULES:
        if re.search(pat, text):
            xau += dx
            btc += db
            impact = max(impact, imp)
    if item["kind"] == "crypto":
        xau = 0
    return {"xau": max(-2, min(2, xau)), "btc": max(-2, min(2, btc)), "impact": impact if (xau or btc) else 1,
            "summary": item["title"][:140], "by": "rules"}


def news_bias(items: list[dict], asset: str, now: datetime, half_life_h: float = 6.0) -> float:
    """Decaying, impact-weighted average direction in [-1, 1]."""
    key = asset.lower()
    total = 0.0
    for it in items:
        sc = it.get("score")
        if not sc:
            continue
        age_h = (now - datetime.fromisoformat(it["published"])).total_seconds() / 3600
        if age_h < 0 or age_h > 48:
            continue
        w = 0.5 ** (age_h / half_life_h)
        total += sc.get(key, 0) * sc.get("impact", 1) * w
    return round(math.tanh(total / 12.0), 3)


# ---------------------------------------------------------------- economic calendar

def fetch_calendar(session: requests.Session | None = None) -> list[dict]:
    session = session or requests.Session()
    r = session.get(CALENDAR_URL, headers=UA, timeout=20)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError as e:
        raise RuntimeError("calendar feed returned non-JSON (probably rate-limited)") from e
    events = []
    for ev in data:
        try:
            t = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        events.append({"title": ev.get("title", ""), "country": ev.get("country", ""), "time": t.isoformat(),
                       "impact": ev.get("impact", ""), "forecast": ev.get("forecast", ""),
                       "previous": ev.get("previous", "")})
    return events


def relevant_events(events: list[dict], cfg: Config, impacts=("High",)) -> list[dict]:
    return [e for e in events if e["country"] in cfg.calendar_currencies and e["impact"] in impacts]


def blackout_reason(events: list[dict], cfg: Config, t: datetime) -> str | None:
    win = timedelta(minutes=cfg.news_blackout_minutes)
    for e in relevant_events(events, cfg):
        et = datetime.fromisoformat(e["time"])
        if et - win <= t <= et + win:
            return f"high-impact news window: {e['country']} {e['title']} at {et:%H:%M} UTC"
    return None


def upcoming(events: list[dict], cfg: Config, now: datetime, hours: float = 24,
             impacts=("High", "Medium")) -> list[dict]:
    end = now + timedelta(hours=hours)
    out = [e for e in relevant_events(events, cfg, impacts)
           if now - timedelta(minutes=5) <= datetime.fromisoformat(e["time"]) <= end]
    return sorted(out, key=lambda e: e["time"])
