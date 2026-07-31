# SwingBot — eToro Long-Only Swing Signal Bot (Twelve Data edition)

Scans YOUR ticker list (tickers.csv) on 1h/4h/daily via Twelve Data,
emails + Telegrams complete BUY alerts (entry, TP >= 8%, sizing, reasons,
leverage/inverse warnings, macro & cycles context). You execute manually
on eToro. No stoploss — thesis-broken alerts monitor open positions.

## Your ticker list = tickers.csv
Edit it on GitHub (pencil icon) or in Excel and re-upload. Columns:
symbol,type(etf|stock),leverage(1-4),inverse(yes|no),note
- etf = commission-free on eToro; stocks get the $2 round-trip fee model
- inverse: yes -> alert says "THIS IS AN INVERSE ETF (Nx BEAR)"
- leverage >= 2 -> alert says "THIS IS A Nx LEVERAGED ETF" + decay warning
- Keep SPY (benchmark). Changes take effect on the next nightly run.

## Jobs (GitHub Actions)
- nightly-watchlist (21:30 UTC weekdays): daily data for all csv tickers,
  ranks by trend, caches options context -> watchlist.json
- hourly.yml (every 4 hours at :15 UTC): 1h candles for watchlist,
  multi-timeframe scan -> alerts + docs/signals.json (dashboard)
- check-symbols (manual): probes every csv symbol against Twelve Data

## Setup
1. Secrets: TWELVEDATA_KEY (free key from twelvedata.com) + SMTP_HOST/PORT/
   USER/PASS, EMAIL_TO, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
2. Settings -> Actions -> General -> Workflow permissions: Read and write
3. Settings -> Pages -> Deploy from branch -> main -> /docs
4. Actions -> run nightly-watchlist first, then the 4-hourly scan

## Free-tier budget (800 req/day)
~78 tickers nightly (daily bars) + ~78 x 6 scans (1h bars) fits if the
watchlist stays under ~100 names. Requests are auto-throttled to 8/min,
so jobs run slowly by design (nightly ~12 min, each scan ~12 min).

## Fundamentals (FMP free key)
Secret FMP_KEY (free at financialmodelingprep.com -> dashboard).
Nightly: one earnings-calendar call for ALL tickers + 2 screening calls per
stock (debt/equity, margins, market cap), cached into watchlist.json.
4-hourly scans read the cache -> zero FMP quota intraday. Stocks reporting
earnings within 7 days are blocked from new alerts; failed screens too.
No key -> everything degrades to neutral (clearly noted in logs).

## Notes
- Local test: pip install pandas numpy pyyaml; python scan.py test
- Signals are decision support, not financial advice.
