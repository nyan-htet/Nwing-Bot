"""Fast Twelve Data universe validation for stocks AND ETFs.

The checker intentionally validates against Twelve Data reference catalogs instead
of making one /time_series request per ticker.

- Stocks  -> /stocks
- ETFs    -> /etfs (with /etf fallback for compatibility)

This means ETF symbols are no longer incorrectly reported as missing just because
Twelve Data keeps them outside the stock reference catalog.

Usage:
  TWELVEDATA_KEY=yourkey python check_symbols.py
      # all tickers.csv symbols
  TWELVEDATA_KEY=yourkey python check_symbols.py sp500
      # one CSV category
  TWELVEDATA_KEY=yourkey python check_symbols.py AAPL,MSFT,SPY
      # explicit symbols

Optional:
  TWELVEDATA_PROBE_N=10
      # additionally probe 10 available symbols with /time_series.
      # Each probe costs 1 API credit. Default is 0 (disabled).
"""

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter

KEY = os.getenv("TWELVEDATA_KEY", "").strip()
BASE = "https://api.twelvedata.com"
CSV_FILE = "tickers.csv"


def load_rows():
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_etf(row):
    """Use the CSV classification; ETFs must never enter the stock-only catalog."""
    type_value = str(row.get("type", "")).strip().lower()
    category = str(row.get("category", "")).strip().lower()
    return type_value == "etf" or category.startswith("etf-") or category == "etf"


def load_targets(rows):
    arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    categories = {r.get("category", "").strip() for r in rows}

    if arg in ("", "reference"):
        # Preserve first occurrence and remove duplicate ticker rows.
        return list(dict.fromkeys(
            r.get("symbol", "").strip().upper()
            for r in rows
            if r.get("symbol", "").strip()
        ))

    if "," in arg:
        return list(dict.fromkeys(s.strip().upper() for s in arg.split(",") if s.strip()))

    if arg and arg.upper() == arg and len(arg) <= 8 and arg not in categories:
        return [arg]

    return list(dict.fromkeys(
        r.get("symbol", "").strip().upper()
        for r in rows
        if r.get("category", "").strip() == arg
    ))


def get_json(path, params=None, timeout=60):
    q = dict(params or {})
    q["apikey"] = KEY
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Nwing-Bot/check-symbols",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_symbols(data, label):
    """Accept the normal {data:[...]} shape plus safe compatibility variants."""
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"Twelve Data {label} error: {data.get('message', data)}")

    if isinstance(data, dict):
        for key in ("data", "stocks", "etfs", "etf", "values"):
            if isinstance(data.get(key), list):
                data = data[key]
                break

    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Twelve Data {label} response: {type(data).__name__}")

    return {
        str(item.get("symbol", "")).strip().upper()
        for item in data
        if isinstance(item, dict) and item.get("symbol")
    }


def reference_catalogs():
    """Load stock and ETF catalogs once each; no per-symbol requests."""
    stocks = extract_symbols(get_json("/stocks"), "/stocks")

    etf_error = None
    for endpoint in ("/etfs", "/etf"):
        try:
            etfs = extract_symbols(get_json(endpoint), endpoint)
            return stocks, etfs, endpoint
        except Exception as exc:
            etf_error = exc

    raise RuntimeError(f"Could not load ETF reference catalog: {etf_error}")


def csv_kind_map(rows):
    """Map each CSV symbol to stock/ETF using the CSV's existing classification."""
    kinds = {}
    for row in rows:
        symbol = row.get("symbol", "").strip().upper()
        if not symbol:
            continue
        kind = "etf" if is_etf(row) else "stock"
        # Prefer ETF if the same ticker appears in multiple rows and any row marks it ETF.
        if symbol not in kinds or kind == "etf":
            kinds[symbol] = kind
    return kinds


def variants(symbol):
    """Return the original symbol plus common dot/dash variants."""
    result = [symbol]
    if "." in symbol:
        result.append(symbol.replace(".", "-"))
    elif "-" in symbol:
        result.append(symbol.replace("-", "."))
    return result


def find_in_catalog(symbol, catalog):
    for candidate in variants(symbol):
        if candidate in catalog:
            return candidate
    return None


def probe(symbol):
    data = get_json(
        "/time_series",
        {"symbol": symbol, "interval": "1h", "outputsize": 2},
        20,
    )
    if isinstance(data, dict) and data.get("values"):
        return True, ""
    if isinstance(data, dict):
        return False, str(data.get("message", data.get("status", "no data")))[:120]
    return False, "unexpected response"


def main():
    if not KEY:
        raise SystemExit("Set TWELVEDATA_KEY first.")
    if not os.path.exists(CSV_FILE):
        raise SystemExit(f"{CSV_FILE} not found.")

    rows = load_rows()
    todo = load_targets(rows)
    if not todo:
        raise SystemExit("No symbols found.")

    kinds = csv_kind_map(rows)

    print("==== CHECK SYMBOLS ====\n")
    print(f"CSV rows                 : {len(rows)}")
    print(f"Unique requested symbols : {len(todo)}")
    print("Validation               : Twelve Data reference catalogs")
    print("Stocks catalog           : /stocks")
    print("ETF catalog              : /etfs (fallback /etf)")
    print("Per-symbol time series   : DISABLED by default")
    print()

    stock_ref, etf_ref, etf_endpoint = reference_catalogs()

    available = []
    missing = []
    by_kind = Counter()

    for symbol in todo:
        kind = kinds.get(symbol)

        # Explicit symbols not present in CSV get checked against both catalogs.
        if kind == "etf":
            found = find_in_catalog(symbol, etf_ref)
        elif kind == "stock":
            found = find_in_catalog(symbol, stock_ref)
        else:
            stock_found = find_in_catalog(symbol, stock_ref)
            etf_found = find_in_catalog(symbol, etf_ref)
            if etf_found:
                kind = "etf"
                found = etf_found
            else:
                kind = "stock"
                found = stock_found

        if found:
            available.append((symbol, kind, found))
            by_kind[f"available_{kind}"] += 1
        else:
            missing.append((symbol, kind))
            by_kind[f"missing_{kind}"] += 1

    print("==== REFERENCE RESULT ====")
    print(f"Twelve Data stocks      : {len(stock_ref)}")
    print(f"Twelve Data ETFs        : {len(etf_ref)}")
    print(f"ETF endpoint used       : {etf_endpoint}")
    print(f"Available               : {len(available)}")
    print(f"Missing                 : {len(missing)}")
    print(f"  Stocks available      : {by_kind['available_stock']}")
    print(f"  ETFs available        : {by_kind['available_etf']}")
    print(f"  Stocks missing        : {by_kind['missing_stock']}")
    print(f"  ETFs missing          : {by_kind['missing_etf']}")

    if missing:
        print("\n==== MISSING ==== ")
        for symbol, kind in missing:
            print(f"  MISS {symbol} [{kind}]")

    print("\n==== AVAILABLE SAMPLE ====")
    for symbol, kind, found in available[:50]:
        suffix = f" -> {found}" if found != symbol else ""
        print(f"  OK   {symbol} [{kind}]{suffix}")
    if len(available) > 50:
        print(f"  ... and {len(available) - 50} more")

    probe_n = max(0, int(os.getenv("TWELVEDATA_PROBE_N", "0")))
    if probe_n:
        candidates = [x for x in available[:probe_n]]
        ok_count = bad_count = 0
        print(f"\n==== OPTIONAL REAL-DATA PROBE ({len(candidates)}) ====")
        print("Each probe costs 1 API credit.")
        for original, _kind, resolved in candidates:
            try:
                ok, msg = probe(resolved)
            except Exception as exc:
                ok, msg = False, str(exc)[:120]
            if ok:
                ok_count += 1
                print(f"  OK   {original}")
            else:
                bad_count += 1
                print(f"  MISS {original} ({msg})")
        print(f"Probe result: {ok_count} OK, {bad_count} failed")

    print("\n==== CSV SUMMARY ====")
    for label, counter in (
        ("Categories", Counter(r.get("category", "").strip() or "(blank)" for r in rows)),
        ("Types", Counter(r.get("type", "").strip() or "(blank)" for r in rows)),
    ):
        print(label + ":")
        for key, value in counter.most_common():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
