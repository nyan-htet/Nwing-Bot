"""Fast Twelve Data universe validation.

The old checker made one /time_series request per ticker and slept ~8 seconds.
This version uses Twelve Data's /stocks reference endpoint once, then compares
tickers.csv locally. Optional real-data probes are limited by TWELVEDATA_PROBE_N.
"""
import csv, json, os, sys, urllib.parse, urllib.request
from collections import Counter

KEY = os.getenv("TWELVEDATA_KEY", "").strip()
BASE = "https://api.twelvedata.com"
CSV_FILE = "tickers.csv"

def load_rows():
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def load_targets(rows):
    arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    categories = {r.get("category", "").strip() for r in rows}
    if arg in ("", "reference"):
        return list(dict.fromkeys(r.get("symbol","").strip().upper() for r in rows if r.get("symbol","").strip()))
    if "," in arg:
        return [s.strip().upper() for s in arg.split(",") if s.strip()]
    if arg and arg.upper() == arg and len(arg) <= 8 and arg not in categories:
        return [arg]
    return list(dict.fromkeys(r.get("symbol","").strip().upper() for r in rows if r.get("category","").strip() == arg))

def get_json(path, params=None, timeout=30):
    q = dict(params or {})
    q["apikey"] = KEY
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"Accept":"application/json","User-Agent":"Nwing-Bot/check-symbols"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def reference_symbols():
    data = get_json("/stocks")
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"Twelve Data /stocks error: {data.get('message', data)}")
    if isinstance(data, dict):
        for k in ("data","stocks","values"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected /stocks response: {type(data).__name__}")
    return {str(x.get("symbol","")).strip().upper() for x in data if isinstance(x, dict) and x.get("symbol")}

def probe(symbol):
    data = get_json("/time_series", {"symbol":symbol, "interval":"1h", "outputsize":2}, 20)
    if isinstance(data, dict) and data.get("values"):
        return True, ""
    return False, str(data.get("message", data.get("status","no data")))[:120] if isinstance(data,dict) else "unexpected response"

def main():
    if not KEY:
        raise SystemExit("Set TWELVEDATA_KEY first.")
    if not os.path.exists(CSV_FILE):
        raise SystemExit(f"{CSV_FILE} not found.")
    rows = load_rows()
    todo = load_targets(rows)
    if not todo:
        raise SystemExit("No symbols found.")

    print("==== CHECK SYMBOLS ====")
    print(f"CSV rows                 : {len(rows)}")
    print(f"Unique requested symbols : {len(todo)}")
    print("Validation               : Twelve Data /stocks reference list")
    print("Per-symbol time series   : DISABLED by default")
    print()

    ref = reference_symbols()
    available, missing = [], []
    for s in todo:
        if s in ref:
            available.append(s)
        else:
            alt = s.replace(".", "-") if "." in s else s.replace("-", ".")
            if alt in ref:
                available.append(f"{s} -> {alt}")
            else:
                missing.append(s)

    print("==== REFERENCE RESULT ====")
    print(f"Twelve Data instruments : {len(ref)}")
    print(f"Available               : {len(available)}")
    print(f"Missing                 : {len(missing)}")
    if missing:
        print("\n==== MISSING ====")
        for s in missing: print(f"  MISS {s}")

    print("\n==== AVAILABLE SAMPLE ====")
    for s in available[:50]: print(f"  OK   {s}")
    if len(available) > 50: print(f"  ... and {len(available)-50} more")

    probe_n = max(0, int(os.getenv("TWELVEDATA_PROBE_N","0")))
    if probe_n:
        candidates = [s for s in todo if s in ref][:probe_n]
        ok_count = bad_count = 0
        print(f"\n==== OPTIONAL REAL-DATA PROBE ({len(candidates)}) ====")
        print("Each probe costs 1 API credit.")
        for s in candidates:
            try:
                ok, msg = probe(s)
            except Exception as e:
                ok, msg = False, str(e)[:120]
            if ok:
                ok_count += 1; print(f"  OK   {s}")
            else:
                bad_count += 1; print(f"  MISS {s} ({msg})")
        print(f"Probe result: {ok_count} OK, {bad_count} failed")

    print("\n==== CSV SUMMARY ====")
    for label, counter in (
        ("Categories", Counter(r.get("category","").strip() or "(blank)" for r in rows)),
        ("Types", Counter(r.get("type","").strip() or "(blank)" for r in rows)),
    ):
        print(label + ":")
        for k,v in counter.most_common(): print(f"  {k}: {v}")

if __name__ == "__main__":
    main()
