"""universe.py — Ticker universe.

S&P 500: scraped once from Wikipedia's constituents table (stable, allowed).
Russell 2000: no free official list; we approximate with IWM ETF holdings
(iShares publishes a CSV) — fetched when available, else skipped.
Falls back to a bundled starter list offline.
"""
STARTER = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM",
    "V", "UNH", "XOM", "LLY", "MA", "HD", "COST", "MU", "AMD", "CAT", "GE",
]


def get_universe():
    tickers = set(STARTER)
    try:
        import pandas as pd
        sp = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        tickers |= set(sp["Symbol"].str.replace(".", "-", regex=False))
    except Exception:
        pass
    try:
        import pandas as pd
        url = ("https://www.ishares.com/us/products/239710/"
               "ishares-russell-2000-etf/1467271812596.ajax?fileType=csv"
               "&fileName=IWM_holdings&dataType=fund")
        iwm = pd.read_csv(url, skiprows=9)
        tickers |= set(iwm["Ticker"].dropna().astype(str))
    except Exception:
        pass
    return sorted(t for t in tickers if t.isascii() and 1 <= len(t) <= 6)
