"""config.py — All settings for the eToro swing-signal bot."""

# ---- Account / trade rules ----
ACCOUNT_SIZE = 10_000.0        # used for suggested position size only (advisory)
POSITION_PCT = 0.05            # suggested position = 5% of account per trade
MIN_TRADE_USD = 250.0          # never suggest positions below this
MIN_TP_PCT = 0.08              # take-profit must be at least 8% above entry
# Target selection: in strong trends the bot reaches PAST the nearest
# resistance for a bigger target (up to MAX_TP_PCT). Nearest resistance is
# only used when the trend is weak.
TP_STRETCH = {
    "strong": 0.60,   # trend score >=4 & ADX>=30: aim for the 60th percentile
    "normal": 0.30,   # otherwise: nearer resistance
}
TP_BLUESKY_ATR = 6.0           # no overhead resistance -> target = entry + N*ATR
MAX_TP_PCT = 0.50              # hard ceiling on any target (50%)
LONG_ONLY = True               # shorts silently skipped
FEE_PER_STOCK_TRADE = 1.0      # $1 buy + $1 sell for stocks
FEE_PER_ETF_TRADE = 0.0        # ETFs commission-free on eToro
MAX_FEE_PCT_OF_PROFIT = 0.10   # skip if fees > 10% of expected profit
ALERT_COOLDOWN_DAYS = 3        # don't re-alert a ticker you opened/skipped recently

# ---- Universe ----
ETF_TICKERS = ["GLD", "SPY", "QQQ", "IWM"]
# Stock universe is built nightly (S&P 500 + Russell 2000 via universe source);
# for local testing, SAMPLE_TICKERS is used.
SAMPLE_TICKERS = ["AAPL", "MSFT", "NVDA", "MU", "AMD", "JPM", "XOM", "CAT"]
WATCHLIST_SIZE = 120           # Grow plan (no daily limits): wide intraday spotlight

# ---- Technical settings ----
EMA_FAST = 20
EMA_SLOW = 50
ADX_PERIOD = 14
ADX_MIN = 25                   # trend strength gate (daily)
ATR_PERIOD = 14
VOL_SPIKE = 1.5                # breakout volume must exceed 1.5x average
RS_LOOKBACK = 63               # ~3 months relative strength vs SPY (daily bars)
HIGH_52W_PROXIMITY = 0.15      # within 15% of 52-week high = "runner" candidate
SWING_LOOKBACK = 5             # bars each side for swing levels (per timeframe)

# ---- Weighted confirmation score (RSI / Bollinger / VWAP / volume) ----
# Each factor scores in [-1, +1]; weighted sum must reach SCORE_MIN to alert.
# None of these is a hard trigger on its own.
QUALITY_WEIGHTS = {
    "rsi": 0.25,        # 4h RSI: healthy zone good, overstretched penalized
    "bollinger": 0.20,  # position in bands + squeeze-expansion bonus
    "vwap": 0.20,       # price vs session VWAP (institutional level)
    "volume": 0.15,     # participation vs 20-bar average
    "options": 0.20,    # P/C ratio, call-wall vs TP, IV expected move (nightly cache)
}
OPEX_CAUTION_DAYS = 1          # flag entries within N trading days of monthly opex
SCORE_MIN = 0.30               # minimum weighted score to allow an alert (raised: fewer, better)
RSI_PERIOD = 14
BB_PERIOD = 20

# ---- Thesis-broken (no hard stoploss; alert-only) ----
THESIS_EMA = 50                # daily close below EMA50 with structure break

# ---- Fundamentals gates ----
EARNINGS_BLOCK_DAYS = 5        # no entry within N trading days before earnings
MAX_DEBT_TO_EQUITY = 2.0
MIN_REV_GROWTH = 0.0           # revenue growth must be non-negative
SMALLCAP_MIN_MARKETCAP = 300e6 # quality floor for Russell 2000 names

# ---- Cycles layer (informative only) ----
CYCLES_HISTORY_YEARS = 45
CYCLES_SYMBOL = "^GSPC"        # S&P 500 index for long history

# ---- Files ----
OVERRIDES_FILE = "overrides.yaml"
POSITIONS_FILE = "positions.yaml"
WATCHLIST_FILE = "watchlist.json"
SIGNALS_FILE = "docs/signals.json"   # docs/ is published by GitHub Pages

# ---- Notifications (set real values via GitHub Secrets / env vars) ----
import os
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT") or "587")   # tolerates empty secret
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DRY_RUN = (os.getenv("DRY_RUN") or "1") == "1"   # 1 = print alerts, don't send
