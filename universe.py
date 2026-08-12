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
    """Returns (unique_tickers, meta).

    Duplicate rows from overlapping S&P 500 / Nasdaq lists collapse to one
    ticker. ETF classification is preserved; if either duplicate row marks a
    ticker as an ETF, it remains an ETF.
    """
    ordered, meta = [], {}
    with open(FILE, newline="") as f:
        for row in csv.DictReader(f):
            s = row["symbol"].strip().upper()
            if not s:
                continue
            typ = row.get("type", "stock").strip().lower()
            if s in meta:
                # Merge duplicate universe rows instead of double-processing.
                if typ == "etf" or meta[s].get("type") == "etf":
                    meta[s]["type"] = "etf"
                if row.get("name") and not meta[s].get("name"):
                    meta[s]["name"] = row["name"].strip()
                if row.get("sector") and meta[s].get("sector") in ("", "Other"):
                    meta[s]["sector"] = row["sector"].strip()
                if row.get("note") and not meta[s].get("note"):
                    meta[s]["note"] = row["note"].strip()
                continue
            ordered.append(s)
            meta[s] = {
                "name": (row.get("name") or "").strip(),
                "type": typ,
                "leverage": int(row.get("leverage", 1) or 1),
                "inverse": row.get("inverse", "no").strip().lower() in ("yes", "y", "true"),
                "sector": (row.get("sector") or "Other").strip(),
                "note": (row.get("note") or "").strip(),
                "source": (row.get("source") or "").strip().lower(),
            }
    # Configured ETFs always remain ETFs even if the CSV source omitted type.
    for etf in getattr(__import__('config'), 'ETF_TICKERS', []):
        if etf in meta:
            meta[etf]["type"] = "etf"
    return ordered, meta


def get_universe():
    return load()[0]
