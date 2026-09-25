# XAU + BTC Signal Agent (Telegram)

A signal agent for **XAUUSD** and **BTCUSD** that runs for free on GitHub Actions, so you don't need a computer switched on. It sends everything to your Telegram group:

| What | When |
|---|---|
| 🟢/🔴 **Trade signals**: entry, stop-loss, targets, lot size, reasons, chart. Marked 📝 **PAPER** until the strategy passes the research test | When a setup triggers (checked every 15 min, Mon–Fri) |
| ✅❌ **Signal updates**: TP1 hit (move SL to entry), TP2, stop, expiry | As they happen |
| 📰 **Market-moving news** alerts (keyword rules, or Claude if you add a key) | Within ~15 min of publication |
| 🗞️ **News digest**: newest gold/BTC headlines with links | Every 2 h, 08:00–22:00 Rome (summer) |
| ⏰ **Event warnings** (CPI, NFP, FOMC…); new signals paused ±30 min | ~35–50 min before the event |
| 🌅 **London brief** and 🗽 **New York brief**: trend, levels, events, headlines (AI-written with a Claude key) | 08:25 and 14:10 Rome time (summer) |
| 📊 **Weekly report + 🔬 strategy research** (tests 18 rule sets on ~10 months of data) | Friday evening |
| Replies to `/status` `/score` `/brief` `/check` | Next run (≤ 15 min) |

> **Please read this first.** Nobody can predict the next candle, and neither can this agent. What it does is combine trend across several timeframes, momentum, volatility, key levels, news and event risk into a **score**. It stays quiet when there's no clear setup, and it always sends a stop-loss. **Signals are marked 📝 PAPER until a rule set passes the research test on data it wasn't tuned on (step 7). Even then, paper-trade for 2 weeks before you risk real money.** This is not financial advice.

---

## Example signal

```
📝 PAPER SIGNAL — for testing only, this strategy has not passed validation yet
🟢 BUY XAUUSDm · score 83/100
Pullback in trend · M15 close 16:15 Rome (14:15 MT5)

Entry 4262.40 (still valid up to 4264.60)
SL 4253.10 (−9.30)
TP1 4276.35 (+13.95 · 1.5R) → close ½, move SL to entry
TP2 4290.30 (+27.90 · 3.0R)
Lot 0.01 → risk $9.30 (1.5% of $600)

Why
• H4 uptrend (price > EMA50 > EMA200)
• H4 EMA50 rising · H4 Supertrend up
• H1 uptrend (EMA20 > EMA50) · H1 MACD momentum building · H1 ADX 27
• M15 pullback into EMA20/50 zone, momentum resuming (RSI 52)
• bullish engulfing candle
• clear room: next level prev-day high 4281.20 (2.0R)

🔬 Strategy v1: not validated
(once a rule set passes the research, the PAPER line disappears and this shows its unseen-data results)
```
(A chart with the entry, SL and TP lines is attached to every signal.)

---

## Setup (about 20 minutes, nothing to install)

### 1. Create the Telegram bot and group
1. In Telegram, open **@BotFather** → send `/newbot` → pick a name → copy the **token** (looks like `8123456789:AAH...`).
2. Create a **group** for your team and add the bot to it.
3. In the group, send: `/start@YourBotName`
4. In a browser open `https://api.telegram.org/bot<TOKEN>/getUpdates`, replacing `<TOKEN>` (including the `<` `>`) with your real token.
   Find `"chat":{"id":-…` and copy that number **including the minus sign**. That's your **chat id**. Normal groups look like `-5151282813`; supergroups look like `-1001234567890`. Both work.
   *(Do this before the agent's first run: once it starts, it reads those updates itself.)*

### 2. Get a free Twelve Data key (gold prices)
Sign up at **twelvedata.com** → Dashboard → copy the **API key**. The free plan (800 requests/day) is enough; the agent uses about 300/day.
*BTC prices come from Kraken's public API, which needs no key.*

### 3. (Optional, paid) Claude API key: AI news scoring and briefs
**You can skip this step.** Without a key the agent runs 100% free: news is scored with keyword rules, and the London/NY briefs are rule-based (trend, levels, events, headlines). You can add a key later without changing anything else.

**console.anthropic.com** → API Keys → Create key. Add some credit and **set a monthly spend limit** (e.g. $15) under Billing → Limits.

### 4. Put the code on GitHub
1. Create a free account on github.com → **New repository** → name it e.g. `signal-agent` → choose **Public** → Create.
   *Why public? GitHub gives public repos unlimited free Actions minutes and reliable schedules. Private repos on the free plan get limited minutes, and their schedules may not run at all. Your keys stay secret (step 5), and GitHub hides them in the public run logs. If you'd like to keep your balance private too, add `ACCOUNT_BALANCE` as a **secret** instead of a variable.*
2. Upload the files: click **"uploading an existing file"** and drag in **everything** from this folder, including the hidden `.github` folder.
   - On macOS, press **Cmd + Shift + .** in Finder to show hidden folders.
   - If `.github` won't upload, click **Add file → Create new file**, type `.github/workflows/agent.yml` as the name, and paste in the contents of that file.
3. Commit.

### 5. Add your secrets
Repository → **Settings → Secrets and variables → Actions**.

**Secrets** tab → *New repository secret* (one at a time):

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from step 1 |
| `TELEGRAM_CHAT_ID` | chat id from step 1 (e.g. `-5151282813`) |
| `TWELVEDATA_API_KEY` | key from step 2 |
| `ANTHROPIC_API_KEY` | key from step 3 (**optional**; leave it out for free mode) |

**Variables** tab → *New repository variable* (optional, defaults shown):

| Name | Default | Meaning |
|---|---|---|
| `ACCOUNT_BALANCE` | `600` | used for lot sizing, so update it now and then (can also be a secret) |
| `RISK_PCT` | `1` | % of balance to risk per trade |
| `MIN_SCORE` | `65` | raise it (e.g. 75) for fewer, stronger signals |
| `ALLOW_SELLS` | `true` | `false` = BUY signals only |
| `SYMBOLS` | `XAUUSD,BTCUSD` | remove one to follow a single market |
| `XAU_PRICE_OFFSET` / `BTC_PRICE_OFFSET` | `0` | if your MT5 price is always e.g. 0.40 higher than the signal prices, set `0.4` |
| `XAU_SPREAD` / `BTC_SPREAD` | `0.25` / `20` | your typical spread (used for stops and cost) |
| `CLAUDE_MODEL_SMART` | `claude-sonnet-5` | set `claude-haiku-4-5-20251001` to cut Claude costs by ~half |
| `LLM_CAN_VETO` | `false` | `true` = Claude can block a signal it rejects |
| `LLM_DAILY_CALL_CAP` | `150` | hard cap on Claude calls per day |
| `COOLDOWN_MINUTES` / `NEWS_BLACKOUT_MINUTES` | `60` / `30` | wait after a closed signal / pause around high-impact events |
| `NEWS_DIGEST_HOURS` | `2` | headline digest every N hours (`0` = off) |
| `NEWS_ALERT_MIN_IMPACT` | `4` | instant news alerts from this impact level (1–5); `3` = more alerts |
| `AUTO_STRATEGY` | `true` | use the research winner automatically; `false` = stay on `v1` unless you choose |
| `XAU_STRATEGY` / `BTC_STRATEGY` | *(empty)* | force a rule set by name, e.g. `pb_aligned_1R` (names are in the research report) |
| `RESEARCH_DAYS` | `300` | how much history the research uses |

### 6. Switch it on and test
Repository → **Actions** tab → enable workflows if asked → **signal-agent** → **Run workflow** → mode `test` → Run.
Within ~1 minute your Telegram group should get a **🔧 Connection test** message with a ✅/❌ for each service.

### 7. Run the strategy research
**Run workflow** → mode `research` (takes about 5–8 minutes). For each symbol it downloads ~10 months of candles and tests **18 rule sets** (see below). Each one is tuned on the first 65% of the period and then checked on the last 35%, which it has never seen.
- If a rule set makes money in **both** periods, it becomes the live strategy for that symbol and its signals lose the 📝 PAPER tag.
- If none does, signals stay 📝 PAPER (for testing only).
The research repeats every Friday, and the full table is under the run's **Artifacts**. Mode `backtest` gives a quick 2-month check of the current live strategy.

### 8. Done
The schedule starts on its own. Try `/status` in the group (the reply arrives on the next run).

---

## Schedule (UTC = Exness MT5 server time)

| Job | Cron (UTC) | Rome, summer | Rome, winter |
|---|---|---|---|
| Tick: news, events, signals, tracking, commands | every 15 min, Mon–Fri | — | — |
| London brief | 06:25 | 08:25 | 07:25 |
| New York brief | 12:10 | 14:10 | 13:10 |
| Weekly report + strategy research | Fri 20:40 | 22:40 | 21:40 |

GitHub runs scheduled jobs on a "best effort" basis, so a run can start 5–20 minutes late at busy times. Signals whose entry has already been missed are skipped automatically.

## Telegram commands

| Command | What you get |
|---|---|
| `/status` | trend on H4/H1/M15, RSI, ADX, ATR, nearest support/resistance, buy & sell scores, news bias, open signals, next events |
| `/score` | the live scoreboard (every signal is followed to TP/SL, nothing is hidden) |
| `/brief` | a fresh market brief now (max 3 per day) |
| `/check sell xau 4246.49 0.01` | for a position **you already hold**: live P/L, whether the trend is with or against you, a structure-based stop and its cost, and upcoming events |
| `/help` | the list above |

---

## Strategy research and 📝 PAPER signals

The original strategy (`v1`) did not make money in its first real-data backtest, so the agent now tests alternatives and only trusts one that proves itself on unseen data.

| Family | Rule sets | Idea |
|---|---|---|
| Original score | `v1`, `v1_buy`, `v1_strict`, `v1_sessions`, `v1_adx25`, `v1_wide_sl`, `pullback_only`, `breakout_only` | confluence score, with one change each (BUY only, score ≥ 75, London/NY hours only, strong trend only, wider stops, one setup type) |
| Aligned pullback | `pb_aligned`, `pb_aligned_1R`, `pb_aligned_2R`, `pb_aligned_sess`, `pb_aligned_buy` | pullback only when the H4 **and** H1 trends agree; different targets and hours |
| RSI(2) dip | `rsi2_trend`, `rsi2_trend_1.5` | buy short-term oversold dips (sell overbought rips) in the direction of the H4 trend |
| London breakout | `london_break`, `london_break_h1`, `london_break_2R` | trade the break of the Asian-session range (00–07 UTC) between 07 and 11 UTC |

**How the winner is chosen:** rank by results on the tuning period only, then walk down the top 3. The first rule set that also passes on the unseen period wins: at least 10 trades, ≥ +0.05R per trade and profit factor ≥ 1.1 (tuning period: ≥ 20 trades, ≥ +0.10R, PF ≥ 1.15). Choosing on one period and confirming on another stops us from just picking the luckiest of 18. Still, a pass is evidence, not proof.

## How a signal is scored (0–100)

| Block | Points | What it measures |
|---|---|---|
| H4 bias | up to 25 | price vs EMA50/EMA200, EMA50 slope, Supertrend |
| H1 trend | up to 30 | EMA20 vs EMA50, MACD histogram and its direction, ADX strength with +DI/−DI |
| M15 trigger (**required**) | up to 30 | *Pullback:* dip into the EMA20/50 zone with the trend intact, then a candle closing back in the trend direction with rising momentum. *Breakout:* close beyond the 5-hour range on expanding volatility. Bonus for engulfing / pin-bar candles |
| News | −10 … +10 | Claude-scored headlines, weighted by impact and fading over ~6 h |
| Room | −5 … +5 | distance to the next key level (previous-day high/low, pivots, H1 swings) in R |
| Penalties | −15 / −10 | chasing an H1 move > 2.5 ATR from EMA20; stretched H1 RSI |

**Hard blocks:** ±30 min around high-impact USD events · gold's daily rollover (20:45–22:15 UTC) · very low or spiking volatility · an opposing key level closer than 1R · a signal already open on that symbol · 60 min cooldown after a signal closes.

**Stops and targets:** SL goes beyond the recent M15 swing plus a buffer (0.3×ATR plus the spread), kept between 1 and 2.5×ATR. TP1 = 1.5R (close half, move SL to entry). TP2 = 3R, or just before the next key level. Signals expire after 24 h.
**Lot size:** balance × risk% ÷ (stop distance × contract size), rounded down, minimum 0.01. If even 0.01 risks more than your target, the message says so.

The backtest uses the **exact same code** as the live signals. It's checked for look-ahead bias (evaluating a bar with all the data gives the same result as with the data cut at that bar), and it shows no edge on a pure random walk, which is what an honest backtest should show. It **can't** replay news or calendar filters (no free history), and it ignores slippage.

---

## Running costs

| Service | Cost |
|---|---|
| GitHub Actions (public repo) | free |
| Twelve Data, Kraken, RSS feeds, ForexFactory calendar | free |
| Claude API | about **$10–15 / month** with Sonnet for the briefs, or about **$5 / month** with `CLAUDE_MODEL_SMART=claude-haiku-4-5-20251001` or with the New York brief removed (delete that cron line). A daily cap (`LLM_DAILY_CALL_CAP`, default 150 calls) and your Anthropic spend limit cap the worst case |

## Using it well (suggested rules)

1. **Always place the stop-loss** from the signal. One held loser can wipe out many small winners.
2. One position per symbol. Don't average down against the trend.
3. At TP1, close half and move the SL to entry. After that the trade can't lose.
4. Skip a signal if the price is already past the "still valid" level.
5. Check `/score` every week. If after 30+ signals the average R is negative, raise `MIN_SCORE` or stop.

## Troubleshooting

| Problem | Fix |
|---|---|
| No test message | Check the run log in Actions. `chat not found` means the chat id is wrong or the bot isn't in the group. If the group is later upgraded to a supergroup (its id changes to `-100…`), the agent switches automatically and tells you the new id to put in the secret |
| Schedule doesn't run | GitHub can take a while to start new schedules. Make sure the repo is public. After 60 days with no commits GitHub pauses schedules; the weekly job makes an empty commit to prevent that |
| `Twelve Data error 429` | Per-minute limit; the agent waits and retries once. Occasional is fine |
| Calendar ❌ in the test | The ForexFactory feed rate-limits sometimes. The agent keeps the last copy and retries hourly |
| Prices differ from MT5 | Set `XAU_PRICE_OFFSET` / `BTC_PRICE_OFFSET`, or apply the same stop/target **distances** to your own entry |
| Claude ❌ | Wrong key or no credit. Signals still work without Claude (rule-based news scoring) |

## Run it on your own computer (optional)

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... TWELVEDATA_API_KEY=... ANTHROPIC_API_KEY=...
python -m agent.main test
python -m agent.main tick --dry-run     # prints the messages instead of sending them
python -m pytest -q                     # offline tests (synthetic data)
```

## Files

```
agent/config.py      settings (env vars), symbols, news sources
agent/data.py        candles: Twelve Data (gold, long history), Kraken (BTC)
agent/indicators.py  EMA, RSI, ATR, MACD, ADX, Supertrend, Bollinger width, patterns, swings
agent/strategy.py    multi-timeframe scoring, setups, stops/targets, lot size
agent/variants.py    the 18 named rule sets
agent/research.py    tuning vs unseen-data test and winner selection
agent/tracker.py     follows signals to TP/SL, scoreboard stats
agent/backtest.py    walk-forward backtest with the live logic
agent/news.py        RSS headlines, rule-based scoring, economic calendar
agent/llm.py         Claude: headline scoring, signal review, web-search briefs
agent/telegram.py    sending messages/charts, reading commands
agent/messages.py    message formats
agent/main.py        orchestration and modes
.github/workflows/agent.yml   the schedule
tests/               offline tests
```

*Trading CFDs on gold and crypto with leverage carries a high risk of losing money. This tool gives information, not financial advice. You are responsible for your trades.*
