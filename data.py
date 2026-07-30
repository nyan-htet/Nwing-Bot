"""data.py — Data layer. yfinance for live use; synthetic data for offline tests."""
import numpy as np
import pandas as pd


def fetch(tickers, period="730d", interval="1h", retries=3, chunk=80):
    """Batch download from Yahoo with retries + chunking (Yahoo throttles
    datacenter IPs like GitHub runners). Returns {ticker: df} — possibly
    partial; callers must handle missing tickers."""
    import time
    import yfinance as yf
    if isinstance(tickers, str):
        tickers = [tickers]
    out = {}
    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        for attempt in range(retries):
            try:
                raw = yf.download(batch, period=period, interval=interval,
                                  group_by="ticker", auto_adjust=True,
                                  threads=True, progress=False)
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
