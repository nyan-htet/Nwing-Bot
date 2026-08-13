# Nwing-Bot — eToro Long-Only Swing Signal System

Scans a personal universe of US stocks and ETFs, builds a nightly watchlist,
scans it every 4 hours for technical setups, runs a news/LLM confidence
check on anything that fires, and delivers alerts by **email + Telegram**,
with a read-only **web dashboard** and a **backtest page**.

**You execute manually on eToro. The bot never places orders.**

- Signals:  `https://<user>.github.io/<repo>/`
- Backtest: `https://<user>.github.io/<repo>/backtest.html`

---

## 1. Pipeline overview

```
NIGHTLY (Sun-Thu 22:30 UTC -> watchlist ready for Mon-Fri)
  tickers.csv (~3,100 symbols)
    Stage 1 — FMP screener funnel (hard eligibility + tier-ranked budget)
    Stage 2 — Twelve Data daily trend, SPY preflight, tier score floor
    Stage 3 — Twelve Data 4H setup trim (pullback / breakout)
    Stage 4 — options + earnings context, finalize watchlist.json
    Stage 5 — notify (nightly summary + universe report) + git publish

HOURLY (Mon-Fri, every 4h at :15 UTC, 00:15 through 20:15)
  watchlist -> 1h/4h technical scan -> new BUY signals -> email only
  position updates (target reached / thesis broken) -> email + Telegram
  quiet run -> "NO NEW SIGNAL" summary -> email + Telegram

NEWS FOLLOW-UP (auto-triggered after hourly-scan finishes)
  recently-alerted tickers -> LLM news/analyst read -> confidence score
  email: every ticker, always, full report
  telegram: only tickers scoring >=60% confidence
  cleans alerted.json: removes tickers that scored <60% so they can re-fire
```

Nothing in the nightly/hourly/news-followup pipeline is affected by
backtesting — `backtest_hist.py` only ever writes `docs/backtest.json` and
`backtest_trades.csv`, and never touches `watchlist.json`, `alerted.json`,
or any nightly/hourly state file.

---

## 2. Nightly — 5 stages, why each exists

### Stage 1 — FMP screener funnel
Two-step design, deliberately loose in step 1:

- **Hard eligibility** — rejects only: market cap < $100M (Tier D), no FMP
  match at all, invalid/missing price data, or an implausible
  volume/market-cap combination. Everything else passes through.
- **Tier-ranked processing budget** — within each tier, ranks survivors by
  market cap (reliable) and takes the top N as a *budget*, not a quota: if
  fewer than N genuinely qualify, all of them pass; the cap only trims
  genuine excess.
  - Tier A (>=$10B): up to 200
  - Tier B ($1B-10B): up to 200
  - Tier C ($100M-1B): up to 100
  - Scales proportionally if `NIGHTLY_DAILY_STOCK_CAP` isn't 500.

**Known data-quality caveat:** the FMP `company-screener` endpoint's
`volume` field reads `0` for most symbols — including real mega-caps like
AAPL/NVDA/MSFT — on plans without real-time entitlement (confirmed on the
Starter plan). Because of this, Stage 1 does **not** gate on liquidity —
only on market cap. `dollar_volume` is still stored per-ticker for
visibility, just not used to reject anyone. Also: the FMP query
deliberately has **no `country: "US"` filter** — that filter reflects legal
domicile, not exchange listing, and was silently excluding real US-listed
names (Accenture, Linde, Johnson Controls, Eaton, Chubb, TE Connectivity,
Garmin, and all the foreign-bank ADRs).

### Stage 2 — Twelve Data daily technical trend
- **SPY preflight**: fetched first, on its own, hardcoded independent of
  `tickers.csv` — never relies on SPY happening to be in the universe list.
  Fails fast if the benchmark is unavailable, before spending the budget on
  the other ~500 tickers.
- Computes a 0-5 daily trend score + relative strength vs SPY for each
  Stage-1 survivor.
- **Tier-specific daily score floor** — smaller companies must clear a
  higher bar: Tier A >= 2.0, Tier B >= 2.5, Tier C >= 3.0
  (`TIER_A/B/C_DAILY_SCORE_MIN` in `config.py`).
- Ranks and caps to `cfg.WATCHLIST_SIZE`.
- Also computes the macro reading (`fnd.macro_context`) here, since this is
  the one point SPY data is guaranteed good — carried through to the
  nightly notification's `Macro: risk-on (SPY 20d realized vol X%)` line.

### Stage 3 — Twelve Data 4H setup trim
Fetches 4H bars for Stage-2 survivors, keeps only tickers with an active
setup (pullback-to-EMA20 or volume breakout), capped at
`NIGHTLY_4H_STOCK_CAP` (default 150). 1H entry timing is deliberately not
touched here — hourly owns that.

### Stage 4 — context + finalize
Adds options/earnings context, writes the final `watchlist.json`, and
compiles the full stock/ETF disposition (`build_universe_report`) for the
Stage 5 email.

### Stage 5 — notify + publish
- Nightly summary: full detail to email, condensed to Telegram (the
  rejected-ticker list alone can run into the thousands of lines — Telegram
  has a hard 4096-character limit).
- **Universe report** (separate email): every ticker's fate, tier by tier —
  watchlist stocks/ETFs by tier, rejected stocks/ETFs by tier with reasons,
  and unknown/no-data stocks/ETFs (no tier ever established). ETFs don't
  have a tier concept (they bypass the fundamental funnel entirely), so
  rejected ETFs is a flat list, not tier-bucketed.
- Notify failures (email or Telegram down) are logged as warnings, never
  raised — a delivery hiccup must not block the git-publish step after it.

---

## 3. Hourly

Runs Mon-Fri, every 4 hours at :15 UTC (00:15 through 20:15) — aligned to
span Tokyo's Monday open through NY's Friday close.

- New technical BUY signals -> **email only**. Telegram is reserved for the
  news-followup's confidence-scored message, so nothing shows up twice
  (once raw, once scored) or gets stuck unscored forever.
- Position updates — target reached or thesis broken — go to **both**
  email and Telegram immediately; these aren't new signals waiting on LLM
  review.
  - **Target reached**: the mute clears unconditionally. If the ticker's
    still in an uptrend on a later scan, it naturally re-fires with a new
    leg. If the trend's broken, nothing re-fires — no explicit check
    needed, this falls out of the normal signal-detection logic.
  - **Thesis broken**: warns once, stays muted (does not clear) — separate
    path from target-reached.
- Quiet run ("NO NEW SIGNAL") -> both email and Telegram, and the ticker
  count is broken out by stock/ETF split.

`alerted.json` is the mute ledger — checked out fresh, updated, and
committed back each run.

---

## 4. News follow-up (`news_llm.py`)

Auto-triggered by `news-followup.yml` right after `hourly-scan` completes,
on tickers alerted in the last `NEWS_LOOKBACK_MIN` (default 90) minutes.

- Pulls news/analyst/fundamental context, has an LLM assess it, computes a
  weighted 0-100 confidence score (`buy_confidence`).
- **Email**: every ticker, always, full report.
- **Telegram**: only tickers scoring **>=60%** (`TELEGRAM_CONFIDENCE_MIN` in
  `news_llm.py`). If every ticker in a run is filtered out, Telegram still
  gets one summary line instead of going silent.
- **Ledger cleanup**: a ticker that scores below 60% is removed from
  `alerted.json` — it was never actually surfaced to you, so it shouldn't
  stay permanently muted. This lets it re-fire on a future hourly scan
  (only in automatic mode — a manual test run like
  `python news_llm.py NVDA,UPS` never touches real ledger state).
- `news_log.csv` (append-only) logs every run including the numeric
  `buy_confidence` — this is what backtesting's confidence-filtered mode
  reads from.

### Confidence colors — two separate thresholds, not the same number
- **Telegram send gate**: `>= 60` (`TELEGRAM_CONFIDENCE_MIN`). Sharp
  cutoff — a score either clears it or doesn't.
- **Icon/bar color bands** (visual only, doesn't affect delivery):
  `< 40` red, `40-65` amber, `65-100` green. A score of 62% *is* sent to
  Telegram (clears 60) but still renders amber (below the 65 green
  threshold) — this is intentional, not a bug, but easy to misremember as
  "the same 60."

---

## 5. Backtesting (`backtest_hist.py` / `backtest.yml`)

Fully isolated from production — only ever writes `docs/backtest.json` and
`backtest_trades.csv`.

**Modes** (`mode` input in `backtest.yml`):
- `strat` — sector-balanced sample from the whole universe, ignores
  confidence entirely.
- a number — that many stocks, blank — the whole universe.
- `conf` — confidence-filtered: only tickers that have **ever** cleared
  the confidence bar in `news_log.csv`'s accumulated history (unioned with
  the latest `docs/news.json` snapshot), picked with the same
  sector-balancing method as `strat` mode.

**`conf` mode configuration** (all in `run_confidence_filtered()` /
`conf_*` workflow inputs):

| Config | Bound | Default |
|---|---|---|
| `starting_equity` | none | $10,000 |
| `position_pct` | 0.5%-5.0% | 2% |
| `n_stocks` + `n_etfs` | <= 200 total | 35 + 15 |
| `years` | <= 6 | 6 |
| `min_confidence` | none | 60 |
| `balance_sectors` | yes/no | yes |

`docs/backtest.html` shows which mode produced the current results (a
`Mode: confidence-filtered - ...` or `Mode: standard - ...` line), since
both modes write to the same `backtest.json` and would otherwise silently
overwrite each other with no visual distinction.

**Scope limit, stated plainly**: `conf` mode is a *snapshot* filter, not a
historical simulation. It answers "how has the strategy performed on the
kind of stocks the LLM currently likes," using each qualifying ticker's
real historical price data. It does **not** (and can't, without
point-in-time historical news/analyst data) simulate the confidence filter
having been running for the past N years.

If the qualifying pool is small (under 5 tickers), a `WARNING` prints —
results from a tiny pool aren't statistically meaningful, and this is a
much likelier cause of "0 trades" than any of the caps above.

`sweep.yml` (separate tool) compares strategy variants against buy & hold
SPY on the same data — a research tool, not part of the live pipeline.

---

## 6. Workflows

| Workflow | Trigger | Shares `twelvedata-api` lock? |
|---|---|---|
| `nightly.yml` | Sun-Thu 22:30 UTC | yes |
| `hourly.yml` | Mon-Fri every 4h, :15 UTC | yes |
| `news-followup.yml` | auto, after `hourly-scan` completes | yes |
| `explain.yml` | manual | yes |
| `check-symbols.yml` | manual | yes |
| `backtest.yml` | manual | **no** — 180min timeout |
| `backtest-v2.yml` | manual | **no** |
| `sweep.yml` | manual | **no** |

All workflows that touch Twelve Data share one concurrency group
(`twelvedata-api`) so GitHub Actions queues them instead of letting them
collide and blow through the per-minute rate limit — e.g. nightly running
long enough to overlap the next scheduled hourly run.

`backtest*`/`sweep` are deliberately **not** in that lock — they can run
for hours, and locking them in would block live nightly/hourly for that
long if triggered at the wrong time. Avoid manually running a
backtest/sweep while nightly or hourly might be active.

All workflows need `permissions: contents: write` plus repo Settings ->
Actions -> General -> Workflow permissions -> **Read and write**.

---

## 7. Secrets

| Secret | Source |
|---|---|
| `TWELVEDATA_KEY` | twelvedata.com |
| `FMP_KEY` | financialmodelingprep.com |
| `SMTP_HOST` / `SMTP_PORT` | e.g. `smtp.gmail.com` / `587` |
| `SMTP_USER` / `SMTP_PASS` | Gmail + App Password |
| `EMAIL_TO` | recipient |
| `TELEGRAM_TOKEN` | @BotFather -> `/newbot` |
| `TELEGRAM_CHAT_ID` | your ID or a channel ID (bot must be admin) |
| `NEWS_ENABLED` / `LLM_PROVIDER` / provider API key | news-followup config |

`TWELVEDATA_MIN_INTERVAL` (env var, seconds/request) tunes request spacing
to your actual plan's per-minute limit if the default (1.6s, safe for a
55/min plan) doesn't match. `NIGHTLY_DAILY_STOCK_CAP` and
`NIGHTLY_4H_STOCK_CAP` tune the Stage 1 / Stage 3 processing budgets.

---

## 8. Files

```
nightly.py             5-stage nightly orchestration (universe -> watchlist)
scan.py                hourly technical scan + notification delivery
news_llm.py             news-followup: LLM confidence scoring
analysis.py            trend, setups, quality score
indicators.py           EMA/ATR/ADX/RSI/Bollinger/VWAP, fib extensions
data.py                Twelve Data client (throttled, retries, progress log)
fundamentals.py         FMP screener funnel + earnings calendar + macro
alerts_ledger.py        alerted.json — mutes a ticker until cleared
notify.py               email + Telegram formatting, delivery
universe.py             reads tickers.csv
backtest_hist.py        standard + confidence-filtered + stratified backtest
backtest_v2.py          stop-loss + time-boxed variant
sweep.py                strategy variant comparison
check_symbols.py        Twelve Data availability probe
tickers.csv             YOUR UNIVERSE — edit this
alerted.json            which tickers are currently muted
news_log.csv            append-only log of every news-followup run
watchlist.json          current nightly watchlist
docs/index.html         read-only signals dashboard
docs/backtest.html      backtest results (mode-labeled)
```

---

## 9. Honest notes

- **Decision support, not financial advice.** You own every trade.
- Confidence scores (`buy_confidence`) are a weighted composite from an
  LLM's read of news/analyst/fundamental context — not a probability,
  never a new trading signal on their own.
- Backtests use daily bars — no 1h trigger, no VWAP factor — and the
  universe is inherently survivor-biased. Real results will differ from
  any backtest here, and `conf` mode specifically is a snapshot filter, not
  a historical replay (see section 5).
- No stop-loss — "thesis broken" alerts exist so you're never blindsided,
  but a broken position can otherwise sit at a loss indefinitely. Acting on
  a thesis-broken alert is your call.
