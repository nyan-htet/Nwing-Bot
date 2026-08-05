# Nwing-Bot — eToro Long-Only Swing Signal System

Scans a personal universe of US stocks and ETFs on 1h / 4h / daily charts and
sends BUY alerts — entry, take-profit, position size, **eToro P/L amount**, and
the full reasoning behind the score — by **email + Telegram**, with a read-only
**web dashboard** and a **backtest page**.

**You execute manually on eToro.** The bot never places orders.

- Signals:  `https://<user>.github.io/<repo>/`
- Backtest: `https://<user>.github.io/<repo>/backtest.html`

---

## 1. Current rules (`config.py`)

| Rule | Value |
|---|---|
| Direction | Long only (shorts skipped) |
| Stop loss | None — "thesis broken" alerts instead |
| Take profit | **+9% minimum, +20% cap** |
| Position size | 5% of a $10,000 reference account, min $250 (advisory) |
| Quality floor | **Stocks 0.70 · ETFs 0.50** (score max = 1.00) |
| Watchlist | Top 120 of the universe, ranked nightly |
| Repeat alerts | **Muted until price clears that signal's target** |
| Earnings | Blocked within 7 days (US stocks) |
| Fees modelled | $1 buy + $1 sell (stocks), $0 (ETFs) |

### Why the 20% cap is not a limit
A ticker alerts once, then goes silent until price **exceeds** its target.
MU at 100 → target 120 → muted → clears 120 → re-alerts at ~121 with a new
target (~145). Long runs are captured in stages, each with a realistic target.

---

## 2. Pipeline

```
NIGHTLY (weekdays 21:30 UTC)
  tickers.csv (574 symbols)
    -> daily data for all
    -> rank by trend strength -> top 120 watchlist
    -> FMP: earnings calendar (1 call) + quality screens (2 calls/stock)
    -> watchlist.json

EVERY 4 HOURS (weekdays, :15 UTC)
  watchlist -> exclusions -> skip tickers already alerted (alerted.json)
    -> DAILY  : trend gate (EMA20/50/200, ADX>=25, RS vs SPY, 52w high, higher highs)
    -> 4-HOUR : setup = pullback to EMA20 | range breakout on volume
    -> 1-HOUR : momentum trigger
    -> QUALITY SCORE (stocks >= 0.70, ETFs >= 0.50)
    -> target 9-20% (Fibonacci extension preferred) -> fee check -> size
    -> email + Telegram + dashboard + signals_log.csv
```

### Quality score — context, not snapshots (max 1.00)

| Factor | Weight | Rewards / punishes |
|---|---|---|
| RSI momentum | 0.26 | Rising *into* the zone, oversold reset. Punishes falling from overbought, bearish divergence |
| VWAP | 0.21 | Reclaim after a dip. Punishes below-VWAP, >2 ATR stretch |
| Extension from EMA | 0.20 | Near the 20 EMA. Punishes parabolic (>3 ATR), flat slope; rewards 20>50>200 |
| Bollinger Bands | 0.18 | Squeeze→expansion, band walk. Punishes upper band while bands contract |
| Volume | 0.15 | **Dry-up on pullbacks**, **surge on breakouts** |

Options factor was removed (free chain data became unusable) and the remaining
five were renormalized, so a perfect setup now scores 1.00.

### Target selection
1. **Fibonacci extension** (1.272 / 1.618 / 2.0 of the last up-leg) inside 9–20%
2. Swing-high resistance inside the window
3. Blue-sky ATR measured move
If nothing sensible sits in the window, **the trade is skipped**.

### Trend score (0–5) and ⭐RUNNER
Uptrend · ADX ≥ 25 · RS vs SPY > 0 · within 15% of 52-week high · higher highs.
**RUNNER** = uptrend + strong ADX + positive RS + near 52w high.

---

## 3. `tickers.csv` — your universe (574 rows: 59 ETFs, 515 stocks)

```
symbol,name,type,leverage,inverse,category,sector,note
```
- `name` — shown in alerts and on the dashboard
- `type` — `etf` (no commission, 0.50 floor) or `stock` ($2 round trip, 0.70 floor)
- `leverage` ≥ 2 → decay warning in the alert
- `sector` — used by the stratified backtest sampler
- **Keep SPY** — benchmark, downloaded but never traded

No inverse ETFs (removed — bearish products in a long-only system).
No structurally capped funds (covered-call, bond, muni, merger-arb) — they
cannot reach +9% swings.

---

## 4. Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `nightly.yml` | weekdays 21:30 UTC | Watchlist + fundamentals cache |
| `hourly.yml` | every 4h, weekdays :15 UTC | Scan, alert, publish |
| `resend.yml` | manual | Re-send current signals (0 API cost) |
| `check-symbols.yml` | manual | Verify symbols (`sp500` / blank / `AAPL,MSFT`) |
| `probe-formats.yml` | manual | Find TD format for foreign listings (.L/.DE) |
| `backtest.yml` | manual | Historical simulation → `backtest.html` |
| `sweep.yml` | manual | 9 strategy variants vs buy & hold SPY |

All need `permissions: contents: write` plus repo Settings → Actions →
General → Workflow permissions → **Read and write**.

---

## 5. Secrets

| Secret | Source |
|---|---|
| `TWELVEDATA_KEY` | twelvedata.com — **Grow plan** (no daily cap, 55/min) |
| `FMP_KEY` | financialmodelingprep.com (free: 250/day) |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` |
| `SMTP_USER` / `SMTP_PASS` | Gmail + **App Password** |
| `EMAIL_TO` | recipient |
| `TELEGRAM_TOKEN` | @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | your ID (@userinfobot) **or** a channel ID (`-100…`, bot must be admin with Post Messages) |

Daily usage: Twelve Data ≈ 574 (nightly) + ~122 per scan — unlimited on Grow.
**FMP ≈ 240 of 250 → run nightly only once per day.**

---

## 6. Backtesting

`backtest.yml` inputs:
- **`mode: strat`** — sector-balanced sample: `n_stocks` spread across all 11
  sectors, `n_etfs` across exposures, `n_baseline` (GLD/QQQ/VOO + random).
  Seeded, so repeated runs test the *same* sample — settings changes are
  measured fairly. (`include_etfs` is ignored in this mode.)
- `mode: <number>` — that many stocks; `mode:` blank — the whole universe

The results page shows: equity curve, monthly/yearly grid, **trade outcome
distribution** (loss >20% … win >20% with counts and P/L), **capital
utilisation** (average deployed $, % of equity, average/max open positions,
% of days in cash, monthly table), best/worst trades, and **buy & hold SPY**.

`sweep.yml` compares 9 variants (score thresholds, trailing exits, RS filter,
200-EMA filter, longer holds, more slots) against the benchmark.

Two limitations, stated plainly:
1. Backtests use **daily bars** — no 1h trigger, no VWAP factor.
2. The universe is **survivor-biased** (today's winners tested backwards).
Real results will be worse than any backtest here.

---

## 7. Files

```
scan.py              nightly + hourly orchestration
analysis.py          trend, setups, quality score, TP ladder, thesis-broken
indicators.py        EMA/ATR/ADX/RSI/Bollinger/VWAP, fib extensions, context helpers
data.py              Twelve Data client (throttled ~50/min, retries)
fundamentals.py      FMP earnings calendar + screens + macro regime (SPY realized vol)
alerts_ledger.py     alerted.json — mutes a ticker until it clears its target
cycles.py            seasonality / presidential cycle (informative only)
universe.py          reads tickers.csv (symbol, name, type, sector…)
portfolio_files.py   positions.yaml / overrides.yaml helpers
notify.py            email + Telegram formatting
resend.py            re-send current signals, no rescan
backtest_hist.py     portfolio simulation + stratified sampler
sweep.py             variant comparison
check_symbols.py     Twelve Data availability probe
probe_formats.py     symbol-format probe for non-US listings
tickers.csv          YOUR UNIVERSE — edit this
alerted.json         which tickers are currently muted
signals_log.csv      every signal ever generated
docs/index.html      read-only signals dashboard
docs/backtest.html   backtest results
```

---

## 8. Daily routine

1. Alert arrives (Telegram / email)
2. Read the plan and the factor breakdown
3. If taking it: buy on eToro, set Take Profit = **the P/L amount** in the alert
4. That ticker stays silent until price clears the target, then may re-alert
   with a new, higher target

---

## 9. Honest notes

- **Decision support, not financial advice.** You own every trade.
- Backtests to date show the strategy **underperforming buy & hold SPY** in a
  strong bull market, with smaller drawdowns. Per-trade expectancy is positive
  (~40% win rate, +13% average win vs −7% average loss); the shortfall comes
  from partial capital deployment, exiting into pullbacks, and fees.
- Leveraged (2x/3x) ETFs decay over time; alerts warn you, and the no-stoploss
  rule is most dangerous there.
- No stoploss means a broken position can sit at a large loss indefinitely.
  Thesis-broken alerts exist so you are never blindsided — acting is your call.
- Don't tune after every quiet week. Change one setting, re-run the *same*
  stratified backtest, and judge on evidence.
