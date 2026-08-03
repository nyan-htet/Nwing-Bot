# Nwing-Bot — eToro Long-Only Swing Signal System

Scans a personal universe of US stocks and ETFs on 1h / 4h / daily charts and
sends complete BUY alerts (entry, take-profit, position size, eToro P/L amount,
full reasoning) by **email + Telegram**, with a **web dashboard** for decisions
and history.

**You execute manually on eToro.** The bot never places orders — it finds
setups, explains them, sizes them, and tracks what you own.

- Dashboard: `https://<user>.github.io/<repo>/`
- Backtest:  `https://<user>.github.io/<repo>/backtest.html`

---

## 1. Core rules (current settings — all in `config.py`)

| Rule | Value | Why |
|---|---|---|
| Direction | **Long only** | Shorts skipped entirely |
| Stop loss | **None** | Unleveraged stock/ETF; "thesis broken" alerts instead |
| Take profit | **min +8%, max +50%** | Trend-aware ladder: strong trends get bigger targets |
| Position size | **5% of account**, min $250 | Advisory — you choose your real size |
| Quality floor | **SCORE_MIN = 0.30** | Fewer, higher-conviction signals |
| Watchlist | **top 120** of the universe | Ranked nightly by trend strength |
| Alert cooldown | 3 days | No repeats after you enter or skip |
| Earnings | blocked within 7 days | No entries into an earnings print |
| Fees modelled | $1 buy + $1 sell (stocks), $0 (ETFs) | Trades where fees eat >10% of expected profit are skipped |

---

## 2. How a signal is produced

```
NIGHTLY (weekdays 21:30 UTC)
  tickers.csv (554 symbols)
    -> daily data for all
    -> rank by trend strength -> top 120 watchlist
    -> FMP: earnings calendar (1 call) + quality screens (2 calls/stock)
    -> options context cache
    -> watchlist.json

EVERY 4 HOURS (weekdays, :15 UTC)
  watchlist -> exclusion rules -> skip open/recently-skipped tickers
    -> DAILY  : trend gate (EMA20/50/200, ADX>=25, RS vs SPY, 52w high, higher highs)
    -> 4-HOUR : setup = pullback to EMA20 | range breakout on volume
    -> 1-HOUR : momentum trigger (confirmation bar)
    -> QUALITY SCORE (must reach 0.30)
    -> TP >= 8% at structure -> fee check -> position size
    -> email + Telegram + dashboard + signals_log.csv
  then: check open positions -> TP reached / thesis broken alerts
```

### Quality score — context, not just levels
Each factor votes −1…+1; the weighted sum must reach `SCORE_MIN`.
Realistic maximum is ~0.8 (options is usually neutral).

| Factor | Weight | Rewards / punishes |
|---|---|---|
| RSI momentum | 0.22 | Rising *into* the zone, oversold reset. Punishes falling from overbought, bearish divergence |
| VWAP | 0.18 | Reclaim after a dip (buyers defended). Punishes below-VWAP, >2 ATR stretch |
| Extension from EMA | 0.17 | Close to the 20 EMA. Punishes parabolic (>3 ATR), flat slope; rewards 20>50>200 stack |
| Bollinger Bands | 0.15 | Squeeze→expansion, healthy band walk. Punishes upper band while bands contract |
| Volume | 0.13 | **Dry-up on pullbacks** (sellers exhausted), **surge on breakouts** |
| Options | 0.15 | Put/call ratio, TP vs call wall, IV expected move (neutral if chain illiquid) |

### Trend score (0–5) and ⭐RUNNER
Points for: uptrend (EMA20>EMA50), ADX ≥ 25, RS vs SPY > 0, within 15% of the
52-week high, higher highs. **RUNNER** = uptrend + strong ADX + positive RS +
near 52w high — the highest-conviction profile.

---

## 3. Your universe — `tickers.csv`

Edit this file (pencil icon on GitHub, or in Excel). It is the single source of
truth for symbols and names.

```
symbol,name,type,leverage,inverse,category,note
```

- `name` — shown in alerts and on the dashboard
- `type` — `etf` (commission-free) or `stock` ($2 round trip)
- `leverage` ≥ 2 → alert warns about volatility decay
- `inverse: yes` → alert warns **"THIS IS AN INVERSE ETF"**
- `category` — `benchmark` / `etf-core` / `top50` / `sp500`
- **Keep SPY** — benchmark only, downloaded but never traded

After editing: run **check-symbols** (optional) then **nightly-watchlist**.

---

## 4. Workflows (GitHub Actions)

| Workflow | Trigger | Purpose |
|---|---|---|
| `nightly.yml` | weekdays 21:30 UTC | Build watchlist, cache fundamentals + options |
| `hourly.yml` | every 4h, weekdays :15 UTC | Scan, alert, publish dashboard |
| `resend.yml` | manual | Re-send current signals (0 API cost) — format testing |
| `check-symbols.yml` | manual | Verify symbols on Twelve Data (`sp500` / blank / `AAPL,MSFT`) |
| `backtest.yml` | manual | Historical simulation → `backtest.html` |
| `sweep.yml` | manual | Compare 9 strategy variants vs buy & hold SPY |

All need `permissions: contents: write`, plus repo Settings → Actions →
General → Workflow permissions → **Read and write**.

---

## 5. Secrets required

| Secret | Source |
|---|---|
| `TWELVEDATA_KEY` | twelvedata.com (Grow: no daily limit, 55 req/min) |
| `FMP_KEY` | financialmodelingprep.com (free: 250 calls/day) |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` |
| `SMTP_USER` / `SMTP_PASS` | Gmail address + **App Password** |
| `EMAIL_TO` | recipient address |
| `TELEGRAM_TOKEN` | @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | @userinfobot (message your bot once first) |

Daily API budget: Twelve Data ≈ 555 (nightly) + ~122 per scan — unlimited on
Grow. **FMP ≈ 240 of 250 → run nightly only once per day.**

---

## 6. Dashboard (`docs/index.html`)

- **Current signals** — full reasoning, "Why this score?" breakdown, and your
  actual fill fields; the **eToro P/L amount** recalculates live as you type
- **Position alerts** — 🎯 TP reached / ⚠️ thesis broken for holdings
- **Exclusion Portal** — include/exclude by ticker, sector or industry with
  from/to dates → `overrides.yaml`
- **Position Portal** — log fills, edit, Close, Delete → `positions.yaml`
- **Token Portal** — fine-grained GitHub token (Contents: read+write, this repo
  only) so buttons can save; stored in your browser only

Tickers you enter or skip vanish from the list and stop re-alerting.

---

## 7. Files

```
scan.py              orchestrates nightly + hourly
analysis.py          trend, setups, quality score, TP ladder, thesis-broken
indicators.py        EMA/ATR/ADX/RSI/Bollinger/VWAP + context helpers
data.py              Twelve Data client (throttled, retries)
fundamentals.py      FMP earnings calendar + screens + macro regime
options_context.py   OI walls, put/call, IV expected move, opex proximity
cycles.py            seasonality / presidential cycle (informative only)
universe.py          reads tickers.csv
portfolio_files.py   positions.yaml / overrides.yaml + cooldown logic
notify.py            email + Telegram formatting
resend.py            re-send current signals without rescanning
backtest_hist.py     historical portfolio simulation
sweep.py             strategy variant comparison
check_symbols.py     Twelve Data availability probe
tickers.csv          YOUR UNIVERSE — edit this
signals_log.csv      every signal ever generated
actions_log.csv      every decision (entered / skipped / closed / deleted)
docs/index.html      dashboard + portals
docs/backtest.html   backtest results page
```

---

## 8. Backtesting

**backtest** → years / max_stocks / include_etfs → publishes `backtest.html`:
equity curve, monthly + yearly returns, win rate, drawdown, best/worst trades,
and **buy & hold SPY over the same period**.

**sweep** → 9 variants (score thresholds, trailing exits, RS filter, 200-EMA
filter, longer holds, more slots), ranked.

Two honest limitations:
1. Backtests run on **daily bars** — no 1h trigger or VWAP factor, so the live
   bot is slightly more selective than the simulation.
2. The universe is **survivor-biased** (today's winners tested backwards), so
   real results will be worse than any backtest here.

Don't pick the best sweep row blindly — that's curve fitting. Prefer changes
that also make economic sense and hold up across periods.

---

## 9. Daily routine

1. Alert arrives (Telegram / email)
2. Read the plan and the "why" — regime, RUNNER tag, warnings
3. If taking it: buy on eToro, set Take Profit = **the P/L amount** in the alert
4. Log it in the Position Portal with your real fill (or hit ✖ Skipped)
5. The bot watches it and tells you when TP is hit or the thesis breaks

---

## 10. Honest notes

- These are **decision-support signals, not financial advice**. You own every
  trade.
- Leveraged (2x/3x/4x) and inverse ETFs decay over time — alerts warn you, but
  the no-stoploss rule is most dangerous exactly there.
- No stoploss means a broken position can sit at a large loss indefinitely.
  Thesis-broken alerts exist so you're never blindsided; acting is your call.
- The first backtest showed the strategy **underperforming buy & hold SPY**.
  Decide what this system is for — smoother drawdowns and selective trades, or
  beating the index — before optimizing further.
- Data sources are third-party and occasionally flaky; every layer degrades to
  neutral rather than failing loudly.
