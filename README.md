# SwingBot — eToro Long-Only Swing Signal Bot

Scans US stocks/ETFs on 1h/4h/daily, emails + Telegrams you complete BUY
alerts (entry, TP >= 8%, size >= $250, reasons, macro & cycles context).
You execute manually on eToro. No stoploss — thesis-broken alerts instead.

## Pipeline
nightly: universe (S&P500 + IWM holdings) -> liquidity + trend prefilter
         -> fundamentals screen -> watchlist.json (~150 tickers)
hourly : watchlist -> your overrides -> daily trend gate (EMA/ADX/RS/52w-high)
         -> 4h setup (pullback/breakout) -> 1h trigger -> TP>=8% -> fee gate
         -> alert + docs/signals.json (dashboard)
         + open-position checks: TP reached / thesis broken

## Files
scan.py (entry: test | nightly | hourly), config.py (all settings),
data.py, indicators.py, analysis.py, fundamentals.py, cycles.py,
universe.py, portfolio_files.py, notify.py,
overrides.yaml + positions.yaml (managed via portal),
docs/index.html (dashboard + portal, GitHub Pages),
.github/workflows/ (hourly + nightly schedules)

## Local test (your laptop, pre-phase 1)
pip install pandas numpy yfinance pyyaml
python scan.py test      # offline synthetic data, prints dry-run alerts
python scan.py hourly    # real data scan (DRY_RUN=1 prints, doesn't send)

## Deploy (when ready)
1. Push this folder to a PUBLIC GitHub repo (Actions minutes are free there)
2. Settings -> Pages -> deploy from branch, /docs folder  -> your dashboard URL
3. Settings -> Secrets -> add SMTP_HOST/PORT/USER/PASS, EMAIL_TO,
   TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (Gmail: use an App Password)
4. Actions tab -> enable workflows. Hourly scan runs at :15, nightly at 21:30 UTC
5. Open dashboard -> portal section -> paste a fine-grained token
   (contents: read/write on this repo only) to manage rules & positions

## Honest notes
- Signals are decision support, not advice. Validate on real data first.
- yfinance is unofficial; occasional gaps happen. The bot degrades gracefully.
- Cycles/seasonality layer is weak-evidence context, deliberately non-triggering.
