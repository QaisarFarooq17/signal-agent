"""Claude: headline scoring, signal review and web-search market briefs."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from .config import NEWS_DOMAINS, Config

log = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None


def _extract_json(text: str, opener: str):
    closer = "]" if opener == "[" else "}"
    a, b = text.find(opener), text.rfind(closer)
    if a < 0 or b <= a:
        raise ValueError("no JSON found in model output")
    return json.loads(text[a:b + 1])


class LLM:
    def __init__(self, cfg: Config, state: dict, client=None):
        self.cfg = cfg
        self.state = state
        self.client = client
        if self.client is None and cfg.anthropic_key and anthropic is not None:
            self.client = anthropic.Anthropic(api_key=cfg.anthropic_key, timeout=300, max_retries=2)

    @property
    def available(self) -> bool:
        return self.client is not None

    def _budget(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        calls = self.state.setdefault("llm_calls", {})
        for k in list(calls):
            if k != today:
                del calls[k]
        if calls.get(today, 0) >= self.cfg.llm_daily_cap:
            log.warning("LLM daily call cap reached (%s)", self.cfg.llm_daily_cap)
            return False
        calls[today] = calls.get(today, 0) + 1
        return True

    def _text(self, resp) -> str:
        return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")

    # ------------------------------------------------------------------ headlines
    def score_headlines(self, items: list[dict]) -> dict[str, dict]:
        if not self.available or not items or not self._budget():
            return {}
        payload = [{"id": it["id"], "source": it["source"], "published": it["published"][:16],
                    "title": it["title"], "summary": it["summary"][:280]} for it in items[:20]]
        system = ("You are a precise financial-news classifier for short-term traders of gold (XAU/USD) and "
                  "Bitcoin (BTC/USD). Judge the likely directional effect of each item on price over the next "
                  "hours to 2 days. Use only the given text; never invent facts.")
        prompt = (
            "Score every item. Return ONLY a JSON array with one object per item:\n"
            '{"id": str, "xau": int, "btc": int, "impact": int, "summary": str}\n'
            "xau/btc: +2 strongly bullish, +1 mildly bullish, 0 neutral or unclear, -1 mildly bearish, -2 strongly bearish.\n"
            "impact: 5 = market-moving surprise (Fed decision, CPI/NFP surprise, war escalation, major ETF or "
            "regulatory decision, exchange collapse), 4 = significant, 3 = moderate, 2 = minor, 1 = noise. "
            "Most items are 1-2.\n"
            "summary: max 18 words, plain English: what happened and why it matters for gold/BTC.\n\n"
            f"Items:\n{json.dumps(payload, ensure_ascii=False)}")
        try:
            resp = self.client.messages.create(model=self.cfg.model_fast, max_tokens=2500, system=system,
                                               messages=[{"role": "user", "content": prompt}])
            data = _extract_json(self._text(resp), "[")
        except Exception as e:  # noqa: BLE001
            log.warning("headline scoring failed: %s", e)
            return {}
        out = {}
        for d in data:
            try:
                out[str(d["id"])] = {"xau": max(-2, min(2, int(d.get("xau", 0)))),
                                     "btc": max(-2, min(2, int(d.get("btc", 0)))),
                                     "impact": max(1, min(5, int(d.get("impact", 1)))),
                                     "summary": str(d.get("summary", ""))[:200], "by": "claude"}
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------ signal review
    def review_signal(self, signal: dict, snapshot: str, headlines: str, events: str) -> dict | None:
        if not self.available or not self._budget():
            return None
        system = ("You are a skeptical, risk-focused trading-desk reviewer. A rules-based system produced an "
                  "intraday signal. You are not predicting price; you are checking whether the timing is poor "
                  "given news flow and scheduled event risk. Be brief.")
        prompt = (
            f"Signal: {signal['side']} {signal['symbol']} at {signal['entry']}, SL {signal['sl']}, "
            f"TP1 {signal['tp1']}, TP2 {signal['tp2']}, score {signal['score']}/100, setup {signal['setup']}.\n"
            f"Reasons: {'; '.join(signal['reasons'])}\n\nTechnical snapshot:\n{snapshot}\n\n"
            f"Recent scored headlines:\n{headlines or 'none'}\n\nScheduled events next 6h:\n{events or 'none'}\n\n"
            'Return ONLY JSON: {"verdict": "confirm" | "caution" | "reject", "reason": "max 25 words"}. '
            "Use reject only for a clear conflict (e.g. major opposing news in the last hours, or a top-tier "
            "event within the trade's likely holding time).")
        try:
            resp = self.client.messages.create(model=self.cfg.model_smart, max_tokens=1500, system=system,
                                               messages=[{"role": "user", "content": prompt}])
            d = _extract_json(self._text(resp), "{")
            verdict = str(d.get("verdict", "")).lower()
            if verdict not in ("confirm", "caution", "reject"):
                return None
            return {"verdict": verdict, "reason": str(d.get("reason", ""))[:220]}
        except Exception as e:  # noqa: BLE001
            log.warning("signal review failed: %s", e)
            return None

    # ------------------------------------------------------------------ briefs
    def brief(self, title: str, context: str) -> tuple[str, list[tuple[str, str]]] | None:
        if not self.available or not self._budget():
            return None
        system = (
            "You are the market analyst for a small team that trades XAUUSD and BTCUSD CFDs intraday on MT5 "
            "(entries on M15, trend from H1/H4; they mostly buy but also sell). Be factual and concise. Use web "
            "search for the latest news; rely on the provided technical snapshot for all price levels. Never "
            "invent numbers, quotes or events; if something is unconfirmed, say so. Express views as a bias with a "
            "confidence level, never as certainty.")
        prompt = (
            f"{context}\n\n"
            "TASK\n1) Search the web for the most important news of the last 24 hours for gold and bitcoin: Fed and "
            "rate expectations, US data, the dollar and Treasury yields, geopolitics, central-bank gold buying, "
            "ETF flows, crypto regulation, hacks and large liquidations.\n"
            "2) Write the brief for Telegram using ONLY these HTML tags: <b>, <i>. No markdown, no links, no tables. "
            "Max 2,800 characters. Use exactly this structure:\n"
            f"<b>{title}</b>\n"
            "<b>What's driving markets</b> - 3 to 5 short bullets starting with '• '\n"
            "<b>XAUUSD</b> - bias (bullish / bearish / neutral, confidence low / medium / high), key levels from the "
            "snapshot, and a plan such as 'buy dips toward X while above Y; below Y stand aside'\n"
            "<b>BTCUSD</b> - same format\n"
            "<b>Event risk</b> - scheduled events with Rome times, or 'none major'\n"
            "<b>Bottom line</b> - one sentence.\n")
        tool = {"type": self.cfg.web_search_tool, "name": "web_search", "max_uses": 6,
                "allowed_domains": NEWS_DOMAINS,
                "user_location": {"type": "approximate", "country": "IT", "timezone": "Europe/Rome"}}
        messages = [{"role": "user", "content": prompt}]
        try:
            resp = self._create_with_search(system, messages, tool)
            for _ in range(3):
                if getattr(resp, "stop_reason", "") != "pause_turn":
                    break
                messages = messages + [{"role": "assistant", "content": resp.content}]
                resp = self._create_with_search(system, messages, tool)
        except Exception as e:  # noqa: BLE001
            log.warning("brief failed: %s", e)
            return None
        # Keep only the text written after the last search (drops "Let me search..." preambles).
        blocks = list(resp.content)
        last_tool = max((k for k, b in enumerate(blocks)
                         if getattr(b, "type", "") in ("server_tool_use", "web_search_tool_result")), default=-1)
        final = [b for b in blocks[last_tool + 1:] if getattr(b, "type", "") == "text"]
        text = "".join(b.text for b in final).strip()
        sources, seen = [], set()
        for b in final:
            for c in (getattr(b, "citations", None) or []):
                url = getattr(c, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    sources.append((getattr(c, "title", "") or url, url))
        return (text, sources[:6]) if text else None

    def _create_with_search(self, system, messages, tool):
        try:
            return self.client.messages.create(model=self.cfg.model_smart, max_tokens=6000, system=system,
                                               messages=messages, tools=[tool])
        except Exception as e:  # noqa: BLE001
            # Unknown tool version on this model/account: retry once with the basic version.
            if tool["type"] != "web_search_20250305" and getattr(e, "status_code", None) == 400:
                log.warning("web search tool %s rejected, retrying with web_search_20250305", tool["type"])
                return self.client.messages.create(model=self.cfg.model_smart, max_tokens=6000, system=system,
                                                   messages=messages,
                                                   tools=[{**tool, "type": "web_search_20250305"}])
            raise

    # ------------------------------------------------------------------ connectivity test
    def ping(self) -> str:
        if not self.available:
            return "not configured"
        resp = self.client.messages.create(model=self.cfg.model_fast, max_tokens=20,
                                           messages=[{"role": "user", "content": "Reply with the word OK."}])
        return "ok" if "ok" in self._text(resp).lower() else "unexpected reply"


def sanitize_html(text: str) -> str:
    """Keep only Telegram-safe tags (<b>, <i>, <code>), escape everything else, convert **markdown**."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"^#+\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"&lt;(/?)(b|i|code)&gt;", r"<\1\2>", text)
    return text
