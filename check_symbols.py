"""check_symbols.py — Verify ticker availability on Twelve Data (free tier).

Usage:  TWELVEDATA_KEY=yourkey python check_symbols.py
Or run the 'check-symbols' workflow on GitHub (uses the repo secret).

Requests one 1h candle per symbol (cheapest possible probe), throttled to
free-tier limits. Prints AVAILABLE / MISSING lists at the end.
"""
import json
import os
import time
import urllib.request

KEY = os.getenv("TWELVEDATA_KEY", "")

ETFS = ["UNG", "USO", "ERX", "COPX", "URA", "EWY", "TZA", "JDST", "SOXS",
        "SPXS", "FAZ", "SLV", "NUGT", "YINN", "TMF", "TNA", "NAIL", "GUSH",
        "GLD", "SOXL", "FAS", "DFEN", "JNUG", "DPST", "LABU", "TECL", "SPYU"]
# Note: "Natgas" mapped to UNG and "oil" to USO — confirm these are the ones
# you want, or swap for BOIL (2x natgas) / others.

STOCKS = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "AVGO", "META",
          "TSLA", "BRK.B", "LLY", "MU", "JPM", "WMT", "AMD", "V", "XOM",
          "JNJ", "MA", "INTC", "ABBV", "CSCO", "BAC", "COST", "AMAT", "LRCX",
          "CVX", "UNH", "KO", "ORCL", "CAT", "GE", "PG", "HD", "MS", "MRK",
          "GS", "NFLX", "PM", "PLTR", "RTX", "PANW", "GEV", "DELL", "WFC",
          "TXN", "KLAC", "LIN", "AXP", "C"]
# Note: BRK-B is written BRK.B on Twelve Data; checker tries both.

BENCH = ["SPY"]


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
    todo = BENCH + ETFS + STOCKS
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
