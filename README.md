# Nwing-Bot — eToro Long-Only Swing Signal System

Scans a personal universe of US stocks/ETFs, builds a nightly watchlist,
checks it every 4 hours for technical setups, runs an LLM news/confidence
check on anything that fires, and alerts by **email + Telegram**.

**You execute manually on eToro. The bot never places orders.**

- Signals: `https://<user>.github.io/<repo>/`
- Backtest: `.../backtest.html` · Explain a ticker: `.../explain.html` · FMP diagnostic: `.../fmp-check.html`

---

## Pipeline

```
NIGHTLY (Sun–Thu 22:30 UTC → watchlist ready for Mon–Fri)
  Stage 1  FMP screener funnel     hard eligibility + tier-ranked budget
  Stage 2  Daily trend (TD)        SPY preflight, tier score floor
  Stage 3  Setup check             4H pullback/breakout OR daily 200MA pullback
  Stage 4  Context + finalize      options/earnings, writes watchlist.json
  Stage 5  Notify + publish        nightly summary + universe report email

HOURLY (Mon–Fri, every 4h at :15 UTC)
  new BUY signal        → email only
  target hit / thesis broken → email + Telegram
  quiet run              → "NO NEW SIGNAL" → email + Telegram

NEWS FOLLOW-UP (auto, right after hourly-scan)
  LLM reads news/analysts on recently-alerted tickers → confidence score
  email: every ticker, always      telegram: only score >= 70%
  score < 70% → removed from alerted.json (free to re-fire later)
```

Backtesting is fully isolated — only ever writes `docs/backtest.json` /
`backtest_trades.csv`, never touches `watchlist.json`, `alerted.json`, or
any nightly/hourly state.

---

## Nightly, stage by stage

**Stage 1 — FMP screener funnel.** Loose on purpose: only rejects sub-$100M
market cap, no FMP match, invalid price, or an implausible volume/cap
combo. Survivors are then ranked by market cap within tier (A/B/C) and
capped as a *budget* (200/200/100, scales with `NIGHTLY_DAILY_STOCK_CAP`) —
if fewer qualify than the budget, all of them pass through.
> Liquidity is **not** gated — FMP's `volume` field reads `0` for most
> symbols (even AAPL/NVDA) on non-realtime plans. No `country: "US"`
> filter either — it excludes real US-listed names domiciled abroad
> (Accenture, Linde, foreign-bank ADRs, etc).

**Stage 2 — daily trend.** SPY is fetched first, on its own, hardcoded
independent of `tickers.csv` — fails fast if the benchmark is down, before
spending the budget on everything else. Computes a 0–5 trend score + RS vs
SPY, with a tier floor (A ≥2.0, B ≥2.5, C ≥3.0). Also computes the macro
reading here (the one point SPY data is guaranteed good).

**Stage 3 — setup.** A ticker qualifies on **either**:
- an active 4H setup (pullback-to-EMA20 or volume breakout), or
- a **daily 200MA pullback** — 200MA rising, price was ≥8% above it within
  the last 40 days, now back within 3% and closed *above* it (not below —
  that's thesis-broken territory, not an entry), ADX ≥20, RS not < -10%.

Capped at `NIGHTLY_4H_STOCK_CAP` (default 150).

**Stage 4 — context + finalize.** Options/earnings context, writes
`watchlist.json`.

**Stage 5 — notify.** Nightly summary: full detail to email, condensed to
Telegram (rejected-ticker lists can run thousands of lines — Telegram caps
at 4096 chars). Separate **universe report** email: every ticker's fate,
tier by tier. Notify failures are logged, never raised — a delivery hiccup
can't block the git-publish step.

---

## Hourly

Every 4h, :15 UTC, Mon–Fri 00:15–20:15 (spans Tokyo's Monday open to NY's
Friday close).

- New signals → **email only** (Telegram is reserved for the scored
  follow-up, so nothing shows up raw *and* scored).
- Target hit / thesis broken → both, immediately. Target hit clears the
  mute unconditionally — re-fires naturally if still trending, stays quiet
  if not. Thesis-broken warns once, stays muted.
- `alerted.json` is the mute ledger.

---

## News follow-up (`news_llm.py`)

Runs on tickers alerted in the last 90 min (`NEWS_LOOKBACK_MIN`).

- Email: every ticker, always.
- Telegram: only **≥70%** confidence (`TELEGRAM_CONFIDENCE_MIN`). All
  filtered → one summary line instead of silence.
- Below 70% → removed from `alerted.json`, free to re-fire later.
- `news_log.csv` (append-only) logs the numeric score — this is what
  backtest's `conf` mode reads from.
- Icon/bar color bands (`<40` red, `40–65` amber, `65–100` green) are
  **visual only** — a 67% score sends nowhere but still shows green.

---

## Backtesting (`backtest_hist.py` / `backtest.yml`)

| Mode | What |
|---|---|
| `strat` | sector-balanced sample, ignores confidence |
| number / blank | that many stocks / the whole universe |
| `conf` | only tickers that ever cleared confidence in `news_log.csv` history, sector-balanced |

`conf` mode config: `starting_equity`, `position_pct` (0.5–5%), `n_stocks`
+ `n_etfs` (≤200), `years` (≤6), `min_confidence`, `balance_sectors`.
Standard/`strat` mode now also respects `starting_equity`/`position_pct` —
they used to be silently ignored outside `conf` mode.

`conf` mode is a **snapshot filter, not a historical simulation** — it
tests today's qualifying tickers against their own real price history, not
"what if the filter ran for 6 years" (impossible without point-in-time
historical news data). If the qualifying pool is under 5 tickers, a
`WARNING` prints — that's a likelier cause of "0 trades" than any cap.
Dropped-for-short-history tickers are also logged by name.

`sweep.yml` compares strategy variants against buy & hold — research tool,
not part of the live pipeline.

---

## FMP diagnostic (`fmp_check.py` / `fmp-check.yml`)

Standalone, isolated tool — type comma-separated tickers, get a step-by-
step check of FMP's `profile`/`quote` endpoints for each. Only ever writes
`docs/fmp_check.json`. Useful for telling apart "FMP is actually down for
this symbol" from "this is an ETF and doesn't have a company-profile record
on FMP" (which explain.py's live fallback now correctly skips entirely for
ETFs, via `is_etf=True`).

---

## Workflows

| Workflow | Trigger | Shares `twelvedata-api` lock? |
|---|---|---|
| `nightly.yml` | Sun–Thu 22:30 UTC | yes |
| `hourly.yml` | Mon–Fri every 4h, :15 UTC | yes |
| `news-followup.yml` | auto, after `hourly-scan` | yes |
| `explain.yml` | manual | yes |
| `check-symbols.yml` | manual | yes |
| `fmp-check.yml` | manual | no (FMP only, no TD calls) |
| `backtest.yml` / `backtest-v2.yml` / `sweep.yml` | manual | **no** — hours-long, would block live runs |

All Twelve-Data workflows share one concurrency lock so they queue instead
of colliding and blowing the per-minute rate limit. Don't manually run
backtest/sweep while nightly or hourly might be active — they're
deliberately excluded from the lock.

All workflows need `permissions: contents: write` + repo Settings →
Actions → General → **Read and write** workflow permissions.

---

## Secrets

| Secret | Source |
|---|---|
| `TWELVEDATA_KEY` | twelvedata.com |
| `FMP_KEY` | financialmodelingprep.com |
| `SMTP_HOST/PORT/USER/PASS`, `EMAIL_TO` | your email provider |
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | @BotFather |
| `NEWS_ENABLED`, `LLM_PROVIDER` + key | news-followup |

`TWELVEDATA_MIN_INTERVAL` (env, seconds/request) tunes spacing to your
plan. `NIGHTLY_DAILY_STOCK_CAP` / `NIGHTLY_4H_STOCK_CAP` tune Stage 1/3
budgets.

---

## Files

```
nightly.py         5-stage nightly orchestration
scan.py            hourly scan + delivery
news_llm.py        LLM confidence follow-up
analysis.py        trend, setups (4H + daily 200MA), quality score
fundamentals.py    FMP screener funnel, earnings, macro
alerts_ledger.py   alerted.json — mute-until-cleared
notify.py          email + Telegram formatting/delivery
backtest_hist.py   standard + confidence-filtered + stratified backtest
fmp_check.py       standalone FMP data-availability diagnostic
explain.py         single-ticker gate-by-gate walkthrough
tickers.csv        YOUR UNIVERSE — edit this
alerted.json       current mutes
watchlist.json     current nightly watchlist
```

---

## Honest notes

- Decision support, not financial advice — you own every trade.
- Confidence scores are a weighted LLM composite, not a probability, never
  a signal on their own.
- Backtests use daily bars (no 1h trigger/VWAP) and are survivor-biased —
  real results will differ.
- No stop-loss. "Thesis broken" alerts exist so you're never blindsided,
  but acting on one is your call.
