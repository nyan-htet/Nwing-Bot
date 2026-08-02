"""universe.py — Ticker universe from tickers.csv (edit that file to add/
remove symbols; the bot consumes it directly).

CSV columns: symbol,type,leverage,inverse,note
  type     : etf | stock  (etf = commission-free on eToro)
  leverage : 1 | 2 | 3 | 4
  inverse  : yes | no     (inverse = a bearish product)
Edit on GitHub (pencil icon) or in Excel and re-upload. SPY must stay (benchmark).
"""
import csv

FILE = "tickers.csv"


def load():
    """Returns (tickers, meta) where meta[symbol] = {type, leverage, inverse, note}."""
    tickers, meta = [], {}
    with open(FILE, newline="") as f:
        for row in csv.DictReader(f):
            s = row["symbol"].strip().upper()
            if not s:
                continue
            tickers.append(s)
            meta[s] = {
                "name": (row.get("name") or "").strip(),
                "type": row.get("type", "stock").strip().lower(),
                "leverage": int(row.get("leverage", 1) or 1),
                "inverse": row.get("inverse", "no").strip().lower() in ("yes", "y", "true"),
                "note": (row.get("note") or "").strip(),
            }
    return tickers, meta


def get_universe():
    return load()[0]
