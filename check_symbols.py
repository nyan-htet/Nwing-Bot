"""check_symbols.py — Verify ticker availability on Twelve Data (free tier).

Usage:
  TWELVEDATA_KEY=yourkey python check_symbols.py          # check ALL tickers.csv
  TWELVEDATA_KEY=yourkey python check_symbols.py sp500    # only category=sp500 rows
  TWELVEDATA_KEY=yourkey python check_symbols.py AAPL,MSFT  # specific symbols

Probes one 1h candle per symbol (cheapest possible), throttled to free-tier
limits (~8s per symbol). QUOTA WARNING: every probe costs 1 of your 800/day
Twelve Data requests — checking all 554 costs 554 requests AND ~75 minutes.
Prefer the category or explicit-symbol modes.
"""
import csv
import json
import os
import sys
import time
import urllib.request

KEY = os.getenv("TWELVEDATA_KEY", "")


def load_targets():
    arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    rows = list(csv.DictReader(open("tickers.csv")))
    if "," in arg or (arg and arg.upper() == arg and "," not in arg and len(arg) <= 6 and arg not in {c["category"] for c in rows if "category" in c}):
        # explicit symbol list
        return [s.strip().upper() for s in arg.split(",") if s.strip()]
    if arg:  # category filter, e.g. sp500
        return [r["symbol"] for r in rows if r.get("category", "") == arg]
    return [r["symbol"] for r in rows]


def probe(symbol):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval=1h&outputsize=2&apikey={KEY}")
    try:
        raw = json.loads(urllib.request.urlopen(url, timeout=20).read())
        if raw.get("values"):
            return True, ""
        return False, str(raw.get("message", raw.get("status", "no data")))[:90]
    except Exception as e:
        return False, str(e)[:90]


def main():
    if not KEY:
        raise SystemExit("Set TWELVEDATA_KEY first.")
    available, missing = [], []
    todo = load_targets()
    print(f"Probing {len(todo)} symbols (~8s each on free tier)…\n")
    for s in todo:
        ok, msg = probe(s)
        alt = None
        if not ok and "." in s:              # try dash variant
            alt = s.replace(".", "-")
        elif not ok and "-" in s:
            alt = s.replace("-", ".")
        if not ok and alt:
            time.sleep(8)
            ok, msg2 = probe(alt)
            if ok:
                s = alt
        (available if ok else missing).append((s, msg if not ok else ""))
        print(f"  {'OK  ' if ok else 'MISS'} {s}" + (f"  ({msg})" if not ok else ""))
        time.sleep(8)                        # 8 req/min free-tier limit

    print("\n==== AVAILABLE ====")
    print(", ".join(s for s, _ in available))
    print("\n==== MISSING ====")
    for s, m in missing:
        print(f"  {s}: {m}")
    print(f"\n{len(available)} available, {len(missing)} missing "
          f"out of {len(todo)}")


if __name__ == "__main__":
    main()
