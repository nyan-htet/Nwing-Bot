"""fmp_check.py — Standalone FMP data-availability diagnostic.

Completely isolated from the live pipeline: read-only, touches no
production state (never writes watchlist.json, alerted.json, tickers.csv,
or any nightly/hourly file). Only ever writes docs/fmp_check.json.

Reuses fundamentals._get() so the checks exercise the EXACT same HTTP
call the live pipeline makes — not a reimplementation that could behave
differently and mislead the diagnosis.

Usage:
  python fmp_check.py AAPL,GLD,SOXL
"""
import datetime as dt
import json
import sys

import config as cfg
import fundamentals as fnd
import universe


def check_ticker(ticker, uni_tickers, uni_meta):
    t = ticker.strip().upper()
    steps = []

    # Step 1 — is the API key even configured?
    key_ok = bool(fnd.FMP_KEY)
    steps.append({
        "step": "FMP_KEY present",
        "status": "PASS" if key_ok else "FAIL",
        "detail": "Key is set" if key_ok else
                  "FMP_KEY secret is empty — nothing below can work.",
    })

    # Step 2 — is this ticker even in your universe, and what type?
    in_universe = t in uni_tickers
    ticker_type = uni_meta.get(t, {}).get("type", "stock") if in_universe else None
    steps.append({
        "step": "In tickers.csv?",
        "status": "PASS" if in_universe else "WARN",
        "detail": (f"Yes — classified as '{ticker_type}'" if in_universe else
                  "Not found in tickers.csv — this ticker isn't part of your "
                  "universe at all, so nightly/hourly would never touch it "
                  "regardless of FMP."),
    })

    profile_ok, profile_detail, profile_raw = None, "", None
    quote_ok, quote_detail, quote_raw = None, "", None

    if key_ok:
        # Step 3 — the exact call company_screen()'s live fallback makes
        # (this is what explain.py's cache-miss path uses).
        try:
            rows = fnd._get("profile", {"symbol": t})
            if isinstance(rows, list) and rows:
                profile_ok = True
                profile_detail = (f"marketCap={rows[0].get('marketCap')}, "
                                  f"price={rows[0].get('price')}, "
                                  f"sector={rows[0].get('sector')}")
                profile_raw = rows[0]
            else:
                profile_ok = False
                profile_detail = f"Empty response: {str(rows)[:150]}"
        except Exception as exc:
            profile_ok = False
            profile_detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        steps.append({"step": "profile endpoint (/stable/profile)",
                      "status": "PASS" if profile_ok else "FAIL",
                      "detail": profile_detail})

        # Step 4 — quote endpoint, cross-check. Some symbols (notably ETFs)
        # have live quote data but no "company profile" — profile failing
        # while quote succeeds is a strong signal this is a normal ETF
        # limitation, not a real outage.
        try:
            rows = fnd._get("quote", {"symbol": t})
            if isinstance(rows, list) and rows:
                quote_ok = True
                quote_detail = (f"price={rows[0].get('price')}, "
                                f"volume={rows[0].get('volume')}")
                quote_raw = rows[0]
            else:
                quote_ok = False
                quote_detail = f"Empty response: {str(rows)[:150]}"
        except Exception as exc:
            quote_ok = False
            quote_detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        steps.append({"step": "quote endpoint (/stable/quote)",
                      "status": "PASS" if quote_ok else "FAIL",
                      "detail": quote_detail})

    # Diagnosis — plain-language verdict, not just raw PASS/FAIL rows.
    if not key_ok:
        diagnosis = "FMP_KEY isn't set for this run — fix that first."
    elif profile_ok:
        diagnosis = "profile endpoint works fine for this ticker right now."
    elif ticker_type == "etf" and not profile_ok and quote_ok:
        diagnosis = (
            "This is classified as an ETF, its 'profile' call failed, but "
            "'quote' succeeded. This is normal — FMP doesn't always carry a "
            "company-profile record for ETFs (they aren't operating "
            "companies). If you're seeing 'FMP profile unavailable' for "
            "this ticker via explain.py, that's explain.py calling "
            "company_screen() without is_etf=True (a real bug there, not "
            "an FMP outage) — production's own screener_context() never "
            "hits this path for ETFs at all, they bypass it entirely."
        )
    elif not profile_ok and not quote_ok:
        diagnosis = (
            "Both profile and quote failed — this looks like a genuine "
            "FMP-side issue for this symbol (wrong symbol, delisted, plan "
            "doesn't cover it, or a real outage), not an ETF-bypass quirk."
        )
    else:
        diagnosis = "profile failed but quote succeeded — see details above."

    return {
        "ticker": t,
        "in_universe": in_universe,
        "type": ticker_type,
        "steps": steps,
        "diagnosis": diagnosis,
        "profile_raw": profile_raw,
        "quote_raw": quote_raw,
    }


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("Usage: python fmp_check.py TICKER1,TICKER2,...")
        raise SystemExit(1)

    tickers = [t.strip().upper() for t in sys.argv[1].split(",") if t.strip()]
    if not tickers:
        print("No tickers given.")
        raise SystemExit(1)

    uni_tickers, uni_meta = universe.load()
    uni_tickers = set(uni_tickers)

    print(f"Checking {len(tickers)} ticker(s): {', '.join(tickers)}")
    results = []
    for t in tickers:
        print(f"\n--- {t} ---")
        r = check_ticker(t, uni_tickers, uni_meta)
        for s in r["steps"]:
            print(f"  [{s['status']}] {s['step']}: {s['detail']}")
        print(f"  DIAGNOSIS: {r['diagnosis']}")
        results.append(r)

    out = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tickers_checked": tickers,
        "results": results,
    }
    with open("docs/fmp_check.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved docs/fmp_check.json ({len(results)} ticker(s))")


if __name__ == "__main__":
    main()
