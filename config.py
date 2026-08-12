"""config.py — All settings for the eToro swing-signal bot."""
import os

# ---- Account / trade rules ----
ACCOUNT_SIZE = 10_000.0        # used for suggested position size only (advisory)
POSITION_PCT = 0.025            # suggested position = 2.5% of account per trade
MIN_TRADE_USD = 150.0          # never suggest positions below this
FRACTIONAL_SHARES = True       # eToro supports fractional units
SHARE_DECIMALS = 2             # round suggested units to this many decimals
MIN_TP_PCT = 0.12              # skip anything not worth at least +9%
# Target selection: in strong trends the bot reaches PAST the nearest
# resistance for a bigger target (up to MAX_TP_PCT). Nearest resistance is
# only used when the trend is weak.
TP_STRETCH = {
    "strong": 0.60,   # trend score >=4 & ADX>=30: aim for the 60th percentile
    "normal": 0.30,   # otherwise: nearer resistance
}
TP_BLUESKY_ATR = 6.0           # no overhead resistance -> target = entry + N*ATR
USE_FIB_TARGETS = True         # prefer Fibonacci extension levels inside the
                               # MIN..MAX window when they exist
MAX_TP_PCT = 0.25              # cap at +20%; runners re-alert in stages via the ledger
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
WATCHLIST_SIZE = 450           # stock spotlight; ETFs are added separately and are never cut by this cap

# ---- Nightly pipeline caps (cost control on the cheapest FMP / Twelve Data plans) ----
# Stage 1->2: cap on how many fundamental survivors get a Twelve Data DAILY call.
# Set via GitHub secret/env NIGHTLY_DAILY_STOCK_CAP; falls back to WATCHLIST_SIZE.
DAILY_STOCK_CAP = int(os.getenv("NIGHTLY_DAILY_STOCK_CAP") or WATCHLIST_SIZE)
# Stage 2->3: cap on how many daily survivors are carried into the options/earnings
# context calls (stage 4), after the Twelve Data 4H setup trim (stage 3).
STAGE3_4H_CAP = int(os.getenv("NIGHTLY_4H_STOCK_CAP") or 150)

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
    "rsi": 0.26,        # level + DIRECTION of approach + reset + divergence
    "bollinger": 0.18,  # band position, squeeze-expansion, band-walk vs snap-back
    "vwap": 0.21,       # above/below + reclaim + extension from VWAP
    "volume": 0.15,     # setup-aware: dry-up on pullbacks, surge on breakouts
    "extension": 0.20,  # distance from EMA20 in ATRs (don't chase stretched moves)
}

# ---- context-modifier settings (the "path, not snapshot" logic) ----
CONTEXT_RULES = True           # master switch for all modifiers below
RSI_DIR_BARS = 8               # bars back used to judge RSI direction
RSI_RESET_LOOKBACK = 12        # a dip below RSI_RESET_LEVEL recently = healthy reset
RSI_RESET_LEVEL = 42
EXT_GOOD_ATR = 1.0             # <=1 ATR above EMA20 = near the line, ideal entry
EXT_STRETCHED_ATR = 3.0        # >=3 ATR above EMA20 = parabolic, correction likely
VWAP_NEAR_PCT = 0.015          # within 1.5% of VWAP = good entry proximity
EMA_STACK_BONUS = 0.15         # 20>50>200 stacked & rising
OPEX_CAUTION_DAYS = 1          # flag entries within N trading days of monthly opex
SCORE_MIN = 0.50               # fallback / ETFs
SCORE_MIN_STOCK = 0.70         # stocks must clear a higher bar
SCORE_MIN_ETF = 0.50           # ETFs (broad baskets, less single-name risk)
RSI_PERIOD = 14
BB_PERIOD = 20

# ---- Thesis-broken (no hard stoploss; alert-only) ----
THESIS_EMA = 50                # daily close below EMA50 with structure break

# ---- Fundamentals gates ----
EARNINGS_BLOCK_DAYS = 5        # no entry within N trading days before earnings
MAX_DEBT_TO_EQUITY = 2.0
MIN_REV_GROWTH = 0.0           # revenue growth must be non-negative
# ---- Market-cap / technical tiers ----
# Stocks are filtered in four tiers. ETFs bypass stock market-cap filters.
TIER_A_MIN_MARKETCAP = 10e9       # >= $10B: normal processing
TIER_B_MIN_MARKETCAP = 1e9        # $1B-$10B: stronger technical quality
TIER_C_MIN_MARKETCAP = 100e6      # $100M-$1B: stronger fundamentals + liquidity + technicals
# Tier C is the full $100M-$1B range. No gap between C and B.
MICROCAP_MIN_MARKETCAP = 100e6    # < $100M: skip
SMALLCAP_MIN_MARKETCAP = 300e6    # backwards-compatible alias; not a hard reject anymore
TIER_B_SCORE_MIN = 0.76
TIER_C_SCORE_MIN = 0.82
TIER_C_MAX_DEBT_TO_EQUITY = 1.50
TIER_C_MIN_NET_MARGIN = 0.00
TIER_C_MIN_DOLLAR_VOLUME = 5e6
SMALLCAP_MIN_DOLLAR_VOLUME = 2e6


# ---- Cycles layer (informative only) ----
CYCLES_HISTORY_YEARS = 45
CYCLES_SYMBOL = "^GSPC"        # S&P 500 index for long history

# ---- Files ----
OVERRIDES_FILE = "overrides.yaml"
POSITIONS_FILE = "positions.yaml"
WATCHLIST_FILE = "watchlist.json"
SIGNALS_FILE = "docs/signals.json"   # docs/ is published by GitHub Pages

# ---- Notifications (set real values via GitHub Secrets / env vars) ----
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT") or "587")   # tolerates empty secret
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DRY_RUN = (os.getenv("DRY_RUN") or "1") == "1"   # 1 = print alerts, don't send
