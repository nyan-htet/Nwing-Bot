"""data.py — Data layer. yfinance for live use; synthetic data for offline tests."""
import numpy as np
import pandas as pd


def fetch(tickers, period="730d", interval="1h", retries=3, chunk=80):
    """Batch download from Yahoo with retries + chunking (Yahoo throttles
    datacenter IPs like GitHub runners). Returns {ticker: df} — possibly
    partial; callers must handle missing tickers."""
    import time
    import yfinance as yf
    session = None
    try:  # newer yfinance + curl_cffi can impersonate a browser (beats blocks)
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate="chrome")
    except Exception:
        pass
    if isinstance(tickers, str):
        tickers = [tickers]
    out = {}
    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        for attempt in range(retries):
            try:
                kw = dict(period=period, interval=interval, group_by="ticker",
                          auto_adjust=True, threads=True, progress=False)
                if session is not None:
                    kw["session"] = session
                raw = yf.download(batch, **kw)
                got = 0
                for t in batch:
                    try:
                        df = raw[t].dropna() if len(batch) > 1 else raw.dropna()
                        if df.empty:
                            continue
                        df = df.rename(columns=str.lower).reset_index()
                        df = df.rename(columns={"date": "time", "datetime": "time"})
                        out[t] = df[["time", "open", "high", "low", "close", "volume"]]
                        got += 1
                    except Exception:
                        continue
                if got > 0 or len(batch) == 0:
                    break  # batch ok
            except Exception as e:
                print(f"fetch attempt {attempt+1} failed for batch "
                      f"{start//chunk+1}: {e}")
            time.sleep(5 * (attempt + 1))   # back off before retry
    return out


def resample(df, rule):
    """Resample 1h candles to e.g. '4h' or '1D'."""
    g = df.set_index("time").resample(rule).agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}).dropna().reset_index()
    return g


def make_sample_1h(n_days=400, seed=7, start_price=100.0, trend=0.10):
    """Synthetic 1h candles (~7 bars/day US session) for offline testing.
    trend: total drift over the whole series (0.10 = +10%)."""
    rng = np.random.default_rng(seed)
    n = n_days * 7
    drift = trend * start_price / n
    vol = np.abs(rng.normal(0.3, 0.15, n)) + 0.1
    # regime switching for realism
    regime = np.cumsum(rng.random(n) < 0.005) % 3
    mult = np.where(regime == 0, 1.0, np.where(regime == 1, 2.2, 0.6))
    rets = drift * mult * rng.choice([1, 1, 1, -0.5], n) + vol * rng.standard_normal(n) * 0.4
    close = np.maximum(start_price + np.cumsum(rets), 5.0)
    o = np.roll(close, 1); o[0] = start_price
    h = np.maximum(o, close) + np.abs(rng.normal(0, 0.3, n))
    l = np.minimum(o, close) - np.abs(rng.normal(0, 0.3, n))
    times = pd.date_range("2024-01-02 14:30", periods=n, freq="1h")
    return pd.DataFrame({"time": times, "open": o, "high": h, "low": l,
                         "close": close,
                         "volume": rng.integers(1e5, 3e6, n).astype(float)})


def make_sample_daily_long(years=45, seed=3):
    """Synthetic ~45y daily index series for testing the cycles module."""
    rng = np.random.default_rng(seed)
    n = years * 252
    t = np.arange(n)
    # secular growth + annual seasonality + 4y cycle + noise
    log_p = (0.0003 * t
             + 0.02 * np.sin(2 * np.pi * t / 252)
             + 0.04 * np.sin(2 * np.pi * t / (252 * 4))
             + np.cumsum(rng.normal(0, 0.01, n)))
    close = 100 * np.exp(log_p)
    times = pd.date_range("1981-01-02", periods=n, freq="B")
    return pd.DataFrame({"time": times, "close": close})


def _stooq_symbol(t):
    return t.lower().replace("-", ".") + ".us"


def fetch_daily_stooq(tickers, retries=2):
    """Daily OHLCV from Stooq (free CSV, no key, tolerant of datacenter IPs).
    Format: https://stooq.com/q/d/l/?s=spy.us&i=d  Returns {ticker: df}."""
    import io
    import time
    import urllib.request
    out = {}
    if isinstance(tickers, str):
        tickers = [tickers]
    for t in tickers:
        url = f"https://stooq.com/q/d/l/?s={_stooq_symbol(t)}&i=d"
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=20).read().decode()
                if raw.startswith("Date,"):
                    df = pd.read_csv(io.StringIO(raw))
                    df.columns = [c.lower() for c in df.columns]
                    df = df.rename(columns={"date": "time"})
                    df["time"] = pd.to_datetime(df["time"])
                    df["volume"] = df.get("volume", 0).fillna(0)
                    out[t] = df[["time", "open", "high", "low", "close", "volume"]]                         .tail(400).reset_index(drop=True)
                break
            except Exception:
                time.sleep(2 * (attempt + 1))
    return out


def fetch_daily(tickers, period="1y"):
    """Daily data from Twelve Data (sole source per user choice)."""
    if isinstance(tickers, str):
        tickers = [tickers]
    return fetch_td(tickers, interval="1day", outputsize=400)


# ---------------- Twelve Data (free API key; datacenter-friendly) -------------
import os as _os
TD_KEY = _os.getenv("TWELVEDATA_KEY", "")
_TD_LAST = [0.0]


def _td_throttle(min_interval=8.0):
    """Free tier: 8 req/min. Space requests ~8s apart."""
    import time
    wait = _TD_LAST[0] + min_interval - time.time()
    if wait > 0:
        time.sleep(wait)
    _TD_LAST[0] = time.time()


def fetch_td(tickers, interval="1h", outputsize=1500):
    """Twelve Data time series. interval: 1h | 1day. Returns {ticker: df}."""
    import json
    import urllib.request
    out = {}
    if isinstance(tickers, str):
        tickers = [tickers]
    if not TD_KEY:
        print("TWELVEDATA_KEY not set — skipping Twelve Data")
        return out
    for t in tickers:
        _td_throttle()
        url = (f"https://api.twelvedata.com/time_series?symbol={t}"
               f"&interval={interval}&outputsize={outputsize}&apikey={TD_KEY}")
        try:
            raw = json.loads(urllib.request.urlopen(url, timeout=20).read())
            vals = raw.get("values")
            if not vals:
                print(f"TD: no data for {t}: {raw.get('message', '')[:80]}")
                continue
            df = pd.DataFrame(vals)[::-1].reset_index(drop=True)
            df = df.rename(columns={"datetime": "time"})
            df["time"] = pd.to_datetime(df["time"])
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df.get(c, 0), errors="coerce")
            df["volume"] = df["volume"].fillna(0)
            out[t] = df[["time", "open", "high", "low", "close", "volume"]].dropna()
        except Exception as e:
            print(f"TD error {t}: {e}")
    return out


def fetch_intraday(tickers):
    """1h candles from Twelve Data (sole source per user choice)."""
    return fetch_td(tickers, interval="1h")
