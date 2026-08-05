"""indicators.py — Pure indicator functions."""
import numpy as np
import pandas as pd


def ema(s, period):
    return s.ewm(span=period, adjust=False).mean()


def atr(df, period=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def adx(df, period=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = atr(df, period)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(span=period, adjust=False).mean() / tr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(span=period, adjust=False).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean().fillna(0)


def relative_strength(stock_close, spy_close, lookback):
    """Stock return minus SPY return over lookback bars (aligned tails)."""
    n = min(len(stock_close), len(spy_close), lookback)
    if n < 5:
        return 0.0
    s = stock_close.iloc[-n:]
    b = spy_close.iloc[-n:]
    return float(s.iloc[-1] / s.iloc[0] - b.iloc[-1] / b.iloc[0])


def last_swing_low(df, lookback):
    lows = df["low"].values
    for j in range(len(lows) - lookback - 2, lookback, -1):
        w = lows[j - lookback: j + lookback + 1]
        if lows[j] == w.min():
            return float(lows[j])
    return None


def last_swing_high(df, lookback, before_idx=None):
    highs = df["high"].values
    end = before_idx if before_idx is not None else len(highs)
    for j in range(end - lookback - 2, lookback, -1):
        w = highs[j - lookback: j + lookback + 1]
        if highs[j] == w.max():
            return float(highs[j])
    return None


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def bollinger(close, period=20, n_std=2.0):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std()
    upper, lower = mid + n_std * sd, mid - n_std * sd
    width = (upper - lower) / mid                      # band width (squeeze detection)
    pos = (close - lower) / (upper - lower).replace(0, 1e-9)  # 0=lower band, 1=upper
    return mid, upper, lower, width, pos


def vwap(df):
    """Rolling session-anchored VWAP: anchored to each trading day (intraday df)."""
    d = df.copy()
    tp = (d["high"] + d["low"] + d["close"]) / 3
    day = d["time"].dt.date
    pv = (tp * d["volume"]).groupby(day).cumsum()
    vv = d["volume"].groupby(day).cumsum().replace(0, 1e-9)
    return pv / vv


def slope_pct(series, bars=10):
    """% change of a series over `bars` (EMA slope, RSI direction, etc)."""
    if len(series) <= bars:
        return 0.0
    a, b = float(series.iloc[-bars - 1]), float(series.iloc[-1])
    return (b - a) / abs(a) if a else 0.0


def extension_atr(close, ema, atr_val):
    """How far price is above/below an EMA, measured in ATRs."""
    if not atr_val or atr_val <= 0:
        return 0.0
    return float((close - ema) / atr_val)


def bearish_divergence(df, rsi_series, lookback=30):
    """Price makes a higher high while RSI makes a lower high."""
    if len(df) < lookback + 5:
        return False
    h = df["high"].iloc[-lookback:]
    r = rsi_series.iloc[-lookback:]
    half = lookback // 2
    p1, p2 = float(h.iloc[:half].max()), float(h.iloc[half:].max())
    r1, r2 = float(r.iloc[:half].max()), float(r.iloc[half:].max())
    return bool(p2 > p1 and r2 < r1 - 3)



def rsi_context(close, period=14, lookback=8):
    """How RSI ARRIVED at its current level.
    Returns (value, direction, recent_min, recent_max, reset_flag).
    reset_flag = RSI dipped below 40 within lookback and is now rising
    (the 'oversold -> recovering' pattern)."""
    r = rsi(close, period)
    if len(r.dropna()) < lookback + 2:
        return float(r.iloc[-1]), "flat", None, None, False
    now = float(r.iloc[-1])
    prev = float(r.iloc[-lookback])
    win = r.iloc[-lookback:]
    lo, hi = float(win.min()), float(win.max())
    if now > prev + 3:
        direction = "rising"
    elif now < prev - 3:
        direction = "falling"
    else:
        direction = "flat"
    reset = bool(lo < 40 and direction == "rising")
    return now, direction, lo, hi, reset


def rsi_divergence(df, period=14, lookback=30):
    """Bearish divergence: price higher high, RSI lower high over lookback."""
    if len(df) < lookback + 5:
        return False
    r = rsi(df["close"], period)
    half = lookback // 2
    p_recent, p_prior = df["high"].iloc[-half:].max(), df["high"].iloc[-lookback:-half].max()
    r_recent, r_prior = r.iloc[-half:].max(), r.iloc[-lookback:-half].max()
    return bool(p_recent > p_prior and r_recent < r_prior - 2)


def ema_extension(df, ema_series, atr_series):
    """How far price sits above its EMA, measured in ATRs (stretch gauge)."""
    a = float(atr_series.iloc[-1])
    if not a:
        return 0.0
    return float((df["close"].iloc[-1] - ema_series.iloc[-1]) / a)


def ema_slope(ema_series, bars=10):
    """% change of the EMA over N bars — is the trend actually moving up?"""
    if len(ema_series) < bars + 1:
        return 0.0
    old = float(ema_series.iloc[-bars - 1])
    return float((ema_series.iloc[-1] - old) / old * 100) if old else 0.0


def ema_stack(fast, slow, long_):
    """Textbook structure: 20 > 50 > 200 (bullish stack)."""
    try:
        f, s, l = float(fast.iloc[-1]), float(slow.iloc[-1]), float(long_.iloc[-1])
    except Exception:
        return "unknown"
    if f > s > l:
        return "bullish stack (20>50>200)"
    if f < s < l:
        return "bearish stack (20<50<200)"
    return "mixed / tangled"


def volume_context(df, period=20):
    """Volume vs average AND whether it is drying up (pullbacks) or
    surging (breakouts). Returns (ratio, trend)."""
    avg = df["volume"].rolling(period).mean()
    if len(avg.dropna()) < 3:
        return 1.0, "flat"
    ratio = float(df["volume"].iloc[-1] / (avg.iloc[-1] or 1))
    recent = float(df["volume"].iloc[-3:].mean())
    prior = float(df["volume"].iloc[-8:-3].mean() or 1)
    trend = "drying up" if recent < prior * 0.8 else ("surging" if recent > prior * 1.3 else "steady")
    return ratio, trend


def adx_context(df, period=14, lookback=8):
    """ADX level plus whether trend strength is building or fading."""
    a = adx(df, period)
    now = float(a.iloc[-1])
    if len(a) < lookback + 1:
        return now, "flat"
    prev = float(a.iloc[-lookback])
    return now, ("rising" if now > prev + 2 else "falling" if now < prev - 2 else "flat")



def swing_leg(df, lookback=120):
    """Most recent completed up-leg: (swing_low, swing_high) before now."""
    if len(df) < 30:
        return None, None
    win = df.iloc[-lookback:] if len(df) > lookback else df
    hi_idx = int(win["high"].values.argmax())
    lo_before = win["low"].values[:hi_idx + 1]
    if len(lo_before) < 2:
        return None, None
    lo_idx = int(lo_before.argmin())
    return float(win["low"].values[lo_idx]), float(win["high"].values[hi_idx])


def fib_extension_targets(df, entry, lookback=120):
    """Projected targets from the last up-leg: 1.272 / 1.618 / 2.0 extensions.
    Returns a sorted list of prices ABOVE entry."""
    lo, hi = swing_leg(df, lookback)
    if lo is None or hi is None or hi <= lo:
        return []
    leg = hi - lo
    out = [lo + leg * r for r in (1.272, 1.618, 2.0)]
    return sorted(p for p in out if p > entry)



def time_to_target(daily, entry, target, atr_period=14, ema_period=20):
    """Rough estimate of how many TRADING DAYS the move may take.

    Two independent paths:
      ATR   : distance / (daily ATR * 0.4)   — 0.4 because price zigzags,
              so a stock ranging 2%/day nets far less than 2%/day.
      SLOPE : distance / current EMA20 daily drift — the trend's actual pace.
    Returns (low, high) as trading days, or (None, None) if not computable.
    Estimate only — real hold times vary widely.
    """
    if target <= entry:
        return None, None
    dist = target - entry
    ests = []

    a = float(atr(daily, atr_period).iloc[-1] or 0)
    if a > 0:
        ests.append(dist / (a * 0.4))

    e = ema(daily["close"], ema_period)
    if len(e) > 11:
        drift = (float(e.iloc[-1]) - float(e.iloc[-11])) / 10.0   # per-day drift
        if drift > 0:
            ests.append(dist / drift)

    if not ests:
        return None, None
    lo, hi = min(ests), max(ests)
    if len(ests) == 1:                     # single method -> widen by +/-35%
        lo, hi = lo * 0.65, hi * 1.35
    lo = max(3, int(round(lo)))
    hi = min(250, int(round(hi)))
    if hi <= lo:
        hi = lo + max(3, int(lo * 0.4))
    # If the two methods disagree wildly, the stock is volatile but not
    # actually advancing -> cap the range and let the caller flag it.
    wide = hi > 3 * lo
    if wide:
        hi = 3 * lo
    return lo, hi
