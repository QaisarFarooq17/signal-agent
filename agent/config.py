"""All settings come from environment variables (GitHub Secrets / Variables).

Nothing secret is stored in the code, so the repository can be public.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v not in (None, "") else default


def _float(name: str, default: float) -> float:
    v = _env(name)
    try:
        return float(v) if v is not None else default
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    return int(_float(name, default))


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SymbolSpec:
    key: str                 # internal key, e.g. "XAUUSD"
    display: str             # broker symbol shown in messages, e.g. "XAUUSDm"
    asset: str               # "XAU" or "BTC" (used for news scoring)
    td_symbol: str           # Twelve Data symbol
    kraken_pair: str | None  # Kraken pair (free, no key) or None
    contract_size: float     # units per 1.00 lot (gold: 100 oz, BTC: 1 coin)
    min_lot: float
    lot_step: float
    spread: float            # typical broker spread in price units (cost model)
    decimals: int
    price_offset: float      # added to feed prices so levels match your MT5 chart
    # UTC windows with no new signals (daily rollover: wide spreads)
    blackouts_utc: tuple[tuple[str, str], ...] = ()


def _symbols() -> dict[str, SymbolSpec]:
    return {
        "XAUUSD": SymbolSpec(
            key="XAUUSD", display=_env("XAU_DISPLAY", "XAUUSDm"), asset="XAU",
            td_symbol="XAU/USD", kraken_pair=None,
            contract_size=100.0, min_lot=0.01, lot_step=0.01,
            spread=_float("XAU_SPREAD", 0.25), decimals=2,
            price_offset=_float("XAU_PRICE_OFFSET", 0.0),
            blackouts_utc=(("20:45", "22:15"),),
        ),
        "BTCUSD": SymbolSpec(
            key="BTCUSD", display=_env("BTC_DISPLAY", "BTCUSDm"), asset="BTC",
            td_symbol="BTC/USD", kraken_pair="XBTUSD",
            contract_size=1.0, min_lot=0.01, lot_step=0.01,
            spread=_float("BTC_SPREAD", 20.0), decimals=1,
            price_offset=_float("BTC_PRICE_OFFSET", 0.0),
            blackouts_utc=(),
        ),
    }


@dataclass(frozen=True)
class Config:
    # --- secrets ---
    telegram_token: str | None = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str | None = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    twelvedata_key: str | None = field(default_factory=lambda: _env("TWELVEDATA_API_KEY"))
    anthropic_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))

    # --- account & risk ---
    account_balance: float = field(default_factory=lambda: _float("ACCOUNT_BALANCE", 600.0))
    risk_pct: float = field(default_factory=lambda: _float("RISK_PCT", 1.0))
    max_risk_pct_warn: float = field(default_factory=lambda: _float("MAX_RISK_PCT_WARN", 3.0))

    # --- strategy ---
    symbols: tuple[str, ...] = field(
        default_factory=lambda: tuple(s.strip().upper() for s in _env("SYMBOLS", "XAUUSD,BTCUSD").split(",") if s.strip()))
    min_score: int = field(default_factory=lambda: _int("MIN_SCORE", 65))
    allow_sells: bool = field(default_factory=lambda: _bool("ALLOW_SELLS", True))
    signal_expiry_hours: float = field(default_factory=lambda: _float("SIGNAL_EXPIRY_HOURS", 24))
    cooldown_minutes: int = field(default_factory=lambda: _int("COOLDOWN_MINUTES", 60))
    news_blackout_minutes: int = field(default_factory=lambda: _int("NEWS_BLACKOUT_MINUTES", 30))
    calendar_currencies: tuple[str, ...] = field(
        default_factory=lambda: tuple(c.strip().upper() for c in _env("CALENDAR_CURRENCIES", "USD").split(",")))

    # --- Claude ---
    model_fast: str = field(default_factory=lambda: _env("CLAUDE_MODEL_FAST", "claude-haiku-4-5-20251001"))
    model_smart: str = field(default_factory=lambda: _env("CLAUDE_MODEL_SMART", "claude-sonnet-5"))
    web_search_tool: str = field(default_factory=lambda: _env("CLAUDE_WEB_SEARCH_TOOL", "web_search_20250305"))
    llm_daily_cap: int = field(default_factory=lambda: _int("LLM_DAILY_CALL_CAP", 150))
    llm_review_signals: bool = field(default_factory=lambda: _bool("LLM_REVIEW_SIGNALS", True))
    llm_can_veto: bool = field(default_factory=lambda: _bool("LLM_CAN_VETO", False))
    news_alert_min_impact: int = field(default_factory=lambda: _int("NEWS_ALERT_MIN_IMPACT", 4))
    news_digest_hours: int = field(default_factory=lambda: _int("NEWS_DIGEST_HOURS", 2))

    # --- strategy selection & research ---
    xau_strategy: str | None = field(default_factory=lambda: _env("XAU_STRATEGY"))
    btc_strategy: str | None = field(default_factory=lambda: _env("BTC_STRATEGY"))
    auto_strategy: bool = field(default_factory=lambda: _bool("AUTO_STRATEGY", True))
    research_days: int = field(default_factory=lambda: _int("RESEARCH_DAYS", 300))

    # --- misc ---
    display_tz: str = field(default_factory=lambda: _env("DISPLAY_TZ", "Europe/Rome"))
    state_dir: str = field(default_factory=lambda: _env("STATE_DIR", ".state"))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", False))
    send_charts: bool = field(default_factory=lambda: _bool("SEND_CHARTS", True))

    def strategy_override(self, key: str) -> str | None:
        return {"XAUUSD": self.xau_strategy, "BTCUSD": self.btc_strategy}.get(key)

    @property
    def specs(self) -> dict[str, SymbolSpec]:
        all_specs = _symbols()
        return {k: all_specs[k] for k in self.symbols if k in all_specs}


# Trusted domains Claude may use when it searches the web for briefs.
NEWS_DOMAINS = [
    "reuters.com", "bloomberg.com", "cnbc.com", "wsj.com", "ft.com", "apnews.com",
    "marketwatch.com", "federalreserve.gov", "bls.gov", "bea.gov", "treasury.gov",
    "cmegroup.com", "gold.org", "kitco.com", "fxstreet.com", "investing.com",
    "coindesk.com", "theblock.co", "cointelegraph.com", "decrypt.co", "sec.gov",
    "farside.co.uk", "ecb.europa.eu",
]

# RSS feeds polled every run. (name, url, kind) — kind "macro" is keyword-filtered.
RSS_FEEDS = [
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", "macro"),
    ("FXStreet", "https://www.fxstreet.com/rss/news", "macro"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto"),
    ("Cointelegraph", "https://cointelegraph.com/rss", "crypto"),
    ("Decrypt", "https://decrypt.co/feed", "crypto"),
]

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
