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
