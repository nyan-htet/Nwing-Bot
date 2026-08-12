"""nightly.py — staged nightly watchlist pipeline.

Stages are deliberately small so GitHub Actions can run each trimming level
as a separate job with its own timeout. Intermediate state is passed through
JSON artifacts; the final watchlist.json is written only in stage 3.

  stage1: FMP bulk fundamentals / market-cap funnel
  stage2: Twelve Data daily technical ranking
  stage3: Twelve Data 4H setup trim (cost control before context)
  stage4: options + earnings context and final watchlist
  stage5: notification only (no API work)
"""

import datetime as dt
from collections import Counter
import json
import os
import sys

import config as cfg
import data
import analysis
import fundamentals as fnd
import notify
import alerts_ledger as al

STAGE1 = "nightly_stage1.json"
STAGE2 = "nightly_stage2.json"
STAGE3 = "nightly_stage3.json"
STAGE4 = "nightly_stage4.json"
DIAGNOSTICS = "nightly_diagnostics.json"

# Benchmark for relative-strength/trend comparisons. Hardcoded on purpose —
# fetched directly regardless of what's in tickers.csv/eligible, so a
# missing/misconfigured row in the universe CSV can never silently drop the
# benchmark fetch.
BENCHMARK = "SPY"


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)


def load(path):
    if not os.path.exists(path):
        raise SystemExit(f"FATAL: required state file missing: {path}")
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict) or not obj:
        raise SystemExit(f"FATAL: required state file is empty/invalid: {path}")
    return obj


def save_diagnostics(stage, input_n=0, passed=0, failed=0,
                     reasons=None, ticker_status=None,
                     api_errors=None, status="completed", error=None):
    """Persist stage-by-stage funnel diagnostics; never breaks the scanner."""
    try:
        existing = {}
        if os.path.exists(DIAGNOSTICS):
            with open(DIAGNOSTICS, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing.setdefault("run_started", dt.datetime.now(dt.timezone.utc).isoformat())
        existing["last_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
        existing.setdefault("stages", {})
        existing["stages"][stage] = {
            "status": status,
            "input": int(input_n or 0),
            "passed": int(passed or 0),
            "failed": int(failed or 0),
            "reasons": dict(reasons or {}),
            "api_errors": dict(api_errors or {}),
            "ticker_status": ticker_status or {},
            "error": error,
        }
        with open(DIAGNOSTICS, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, default=str)
    except Exception as exc:
        print(f"WARNING: diagnostics write failed: {exc}")


def print_stage_diagnostics(stage, input_n, passed, failed, reasons, status="completed"):
    print(f"\n==== DIAGNOSTICS — {stage.upper()} ====")
    print(f"Status : {status}")
    print(f"Input  : {input_n}")
    print(f"Passed : {passed}")
    print(f"Failed : {failed}")
    if reasons:
        print("Reasons:")
        for reason, count in sorted(reasons.items(), key=lambda x: (-x[1], x[0])):
            print(f"  - {reason}: {count}")
    print("======================================")


def stage1():
    """Trim the universe using the FMP company-screener (market cap + liquidity).

    Uses fnd.screener_context(), which makes a small, bounded number of
    bulk screener calls (not one call per ticker) and returns a stock
    universe already capped/tiered to cfg.DAILY_STOCK_CAP — this is the
    cost-control gate before stage 2 spends any Twelve Data calls.
    """
    import universe

    tickers, tmeta = universe.load()
    etf_set = {
        t for t in tickers
        if tmeta.get(t, {}).get("type") == "etf"
        or t in getattr(cfg, "ETF_TICKERS", [])
    }
    for t in etf_set:
        tmeta.setdefault(t, {})["type"] = "etf"
    stocks = [t for t in tickers if t not in etf_set]

    daily_cap = int(getattr(cfg, "DAILY_STOCK_CAP", 500) or 500)

    try:
        result = fnd.screener_context(tickers, tmeta, stock_cap=daily_cap)
    except Exception as exc:
        msg = f"FMP screener unavailable: {type(exc).__name__}: {str(exc)[:160]}"
        save_diagnostics(
            "stage1",
            input_n=len(stocks),
            failed=len(stocks),
            reasons={"FMP screener unavailable": len(stocks)},
            api_errors={"FMP screener": msg},
            status="failed",
            error=msg,
        )
        raise SystemExit("FATAL: " + msg)

    eligible_stocks = result["eligible_stocks"]
    meta = result["meta"]
    failed = result["failed"]
    tier_counts = result["tier_counts"]
    bulk_note = result["note"]

    # Never allow an upstream FMP outage/plan limitation to silently turn
    # every stock into a fundamental failure and publish an ETF-only list.
    if stocks and not eligible_stocks:
        msg = f"FMP screener produced zero eligible stocks. {bulk_note}"
        save_diagnostics(
            "stage1",
            input_n=len(stocks),
            failed=len(stocks),
            reasons={"FMP screener produced nothing": len(stocks)},
            api_errors={"FMP screener": bulk_note},
            status="failed",
            error=msg,
        )
        raise SystemExit("FATAL: " + msg)

    stage1_reasons = Counter()
    stage1_status = {}
    screen_failed = sorted(failed.keys())
    screen_failed_details = dict(failed)
    for t, notes in failed.items():
        stage1_status[t] = {"status": "FAIL", "reasons": notes}
        for reason in notes:
            stage1_reasons[reason] += 1
    for t in eligible_stocks:
        tier = meta.get(t, {}).get("screen", {}).get("tier", "UNKNOWN")
        stage1_status[t] = {"status": "PASS", "tier": tier}

    eligible = eligible_stocks + sorted(etf_set)

    try:
        prev = set(load(cfg.WATCHLIST_FILE).get("tickers", []))
    except Exception:
        prev = set()

    state = {
        "csv_n": len(tickers),
        "tickers": tickers,
        "tmeta": tmeta,
        "stocks": stocks,
        "etf_set": sorted(etf_set),
        "eligible_stocks": eligible_stocks,
        "eligible": eligible,
        "meta": meta,
        "tier_counts": tier_counts,
        "screen_failed": screen_failed,
        "screen_failed_details": screen_failed_details,
        "bulk_note": bulk_note,
        "prev": sorted(prev),
        "created": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save(STAGE1, state)
    save_diagnostics(
        "stage1",
        input_n=len(stocks),
        passed=len(eligible_stocks),
        failed=len(screen_failed),
        reasons=stage1_reasons,
        ticker_status=stage1_status,
    )
    print_stage_diagnostics("stage1", len(stocks), len(eligible_stocks),
                            len(screen_failed), stage1_reasons)

    print("==== NIGHTLY STAGE 1 — FMP SCREENER FUNNEL ====")
    print(f"CSV unique tickers : {len(tickers)}")
    print(f"Stocks             : {len(stocks)}")
    print(f"ETFs (unfiltered)  : {len(etf_set)}")
    print(f"FMP screener       : {bulk_note}")
    print(f"Tier counts         : {tier_counts}")
    print(f"Fundamental fails   : {len(screen_failed)}")
    print(f"Technical survivors : {len(eligible_stocks)} stocks + {len(etf_set)} ETFs")
    print("=================================================")


def stage2():
    """Fetch daily data only for stage-1 survivors and trim by trend ranking."""
    s1 = load(STAGE1)
    eligible = s1["eligible"]
    eligible_stocks = s1["eligible_stocks"]
    etf_set = set(s1["etf_set"])
    meta = s1["meta"]

    if not eligible:
        raise SystemExit("FATAL: Stage 1 produced no eligible tickers.")

    # SPY preflight: fetched directly here, independent of tickers.csv/eligible
    # — do NOT rely on SPY happening to be in the universe list. Checked FIRST,
    # before spending the daily-data budget on the other ~500 tickers, so a
    # dead API key/quota fails cheap and immediate instead of only surfacing
    # after the full 500-ticker fetch has already run.
    spy_preflight = data.fetch_daily([BENCHMARK])
    spy = spy_preflight.get(BENCHMARK)
    if spy is None or len(spy) < 60:
        save_diagnostics(
            "stage2",
            input_n=1,
            failed=1,
            reasons={"SPY benchmark unavailable (preflight)": 1},
            api_errors={"Twelve Data SPY preflight": "no/short SPY daily data"},
            status="failed",
            error="SPY benchmark unavailable on preflight check",
        )
        raise SystemExit(
            "FATAL: SPY benchmark unavailable from Twelve Data (preflight check, "
            "before spending the daily-data budget) — check TWELVEDATA_KEY and quota."
        )

    # Preflight passed — fetch the rest of the eligible list (SPY already have it,
    # and is excluded here purely to avoid a duplicate fetch, not because the
    # preflight depended on it being present).
    rest = [t for t in eligible if t != BENCHMARK]
    d = data.fetch_daily(rest) if rest else {}
    d[BENCHMARK] = spy

    scored, short_history, no_data = [], [], []
    stage2_reasons = Counter()
    stage2_status = {}

    for t in eligible_stocks:
        df = d.get(t)
        if df is None:
            no_data.append(t)
            stage2_status[t] = {"status": "FAIL", "reason": "No Twelve Data daily data"}
            stage2_reasons["No Twelve Data daily data"] += 1
            continue
        if len(df) < 120:
            short_history.append(t)
            stage2_status[t] = {"status": "FAIL", "reason": "Insufficient daily history"}
            stage2_reasons["Insufficient daily history"] += 1
            continue
        try:
            tr = analysis.daily_trend(df, spy, cfg)
            score = tr["score"] + tr["rs"]
            scored.append((score, t))
            stage2_status[t] = {
                "status": "RANKED",
                "score": tr.get("score"),
                "relative_strength": tr.get("rs"),
                "combined_score": score,
            }
        except Exception as exc:
            reason = f"Technical calculation error: {type(exc).__name__}"
            stage2_status[t] = {"status": "FAIL", "reason": reason}
            stage2_reasons[reason] += 1

    scored.sort(reverse=True)
    top_stocks = [t for _, t in scored[:cfg.WATCHLIST_SIZE]]
    top_etfs = [t for t in sorted(etf_set) if t in d and len(d[t]) >= 60]
    top = top_stocks + top_etfs

    if not top:
        raise SystemExit(
            "FATAL: Stage 2 produced an empty watchlist. "
            "No final watchlist will be published."
        )

    top_ranked = []
    for t, sc, rs in [
        (t, analysis.daily_trend(d[t], spy, cfg)["score"],
         analysis.daily_trend(d[t], spy, cfg)["rs"])
        for _, t in scored[:10] if t in d
    ]:
        top_ranked.append([t, sc, rs])

    spots = {
        t: float(d[t]["close"].iloc[-1])
        for t in top if t in d and len(d[t])
    }

    for t in top_stocks:
        stage2_status.setdefault(t, {})["final"] = "SURVIVOR"
    for t in top_etfs:
        stage2_status.setdefault(t, {})["final"] = "ETF_SURVIVOR"

    state = {
        **s1,
        "top_stocks": top_stocks,
        "top_etfs": top_etfs,
        "top": top,
        "no_data": no_data,
        "short_history": short_history,
        "top_ranked": top_ranked,
        "spots": spots,
        "technical_created": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save(STAGE2, state)
    stage2_failed = len(eligible_stocks) - len(scored)
    save_diagnostics(
        "stage2",
        input_n=len(eligible_stocks),
        passed=len(top_stocks),
        failed=stage2_failed,
        reasons=stage2_reasons,
        ticker_status=stage2_status,
    )
    print_stage_diagnostics("stage2", len(eligible_stocks), len(top_stocks),
                            stage2_failed, stage2_reasons)

    print("==== NIGHTLY STAGE 2 — TECHNICAL FUNNEL ====")
    print(f"Technical universe : {len(eligible_stocks)} stocks + {len(etf_set)} ETFs")
    print(f"No daily data      : {len(no_data)}")
    print(f"Short history      : {len(short_history)}")
    print(f"Stock spotlight    : {len(top_stocks)} / {len(eligible_stocks)}")
    print(f"ETFs retained      : {len(top_etfs)} / {len(etf_set)}")
    print(f"Stage-2 survivors  : {len(top)}")
    print("=============================================")


def stage3():
    """Twelve Data 4H setup trim.

    Fetches 4H candles only for stage-2 survivors and keeps stocks that show
    an active 4H setup (pullback-to-EMA20 or volume breakout), capped at
    cfg.STAGE3_4H_CAP. This is a *cost* trim only — it never judges signal
    quality; hourly-scan still owns the real entry decision on 1H. ETFs are
    a small fixed basket and are carried through unfiltered.
    """
    s2 = load(STAGE2)
    top = s2["top"]
    top_stocks = s2["top_stocks"]
    top_etfs = s2["top_etfs"]

    if not top:
        raise SystemExit("FATAL: Stage 2 produced no survivors to trim on 4H.")

    cap = int(getattr(cfg, "STAGE3_4H_CAP", 150) or 0)

    d4 = data.fetch_td(top, interval="4h", outputsize=200)

    setups = {}
    no_4h_data = []
    stage3_reasons = Counter()
    stage3_status = {}

    for t in top:
        h4 = d4.get(t)
        if h4 is None or len(h4) < 25:
            no_4h_data.append(t)
            stage3_status[t] = {"status": "FAIL", "reason": "No/short 4H data"}
            stage3_reasons["No/short 4H data"] += 1
            continue
        try:
            setup = analysis.setup_4h(h4, cfg)
        except Exception as exc:
            reason = f"4H setup error: {type(exc).__name__}"
            stage3_status[t] = {"status": "FAIL", "reason": reason}
            stage3_reasons[reason] += 1
            continue
        if setup:
            setups[t] = setup
            stage3_status[t] = {"status": "PASS", "setup": setup}
        else:
            stage3_status[t] = {"status": "FAIL", "reason": "No active 4H setup"}
            stage3_reasons["No active 4H setup"] += 1

    # Preserve stage-2 rank order, only keep stocks with an active setup, cap.
    setup_stocks = [t for t in top_stocks if t in setups]
    dropped_no_setup = [t for t in top_stocks if t not in setups]
    kept_stocks = setup_stocks[:cap]
    cut_by_cap = setup_stocks[cap:]
    for t in cut_by_cap:
        stage3_status[t]["status"] = "FAIL"
        stage3_status[t]["reason"] = "Cut by NIGHTLY_4H_STOCK_CAP (cost control)"
        stage3_reasons["Cut by NIGHTLY_4H_STOCK_CAP (cost control)"] += 1

    trimmed = kept_stocks + top_etfs
    if not trimmed:
        raise SystemExit("FATAL: Stage 3 (4H trim) produced an empty watchlist.")

    for t in kept_stocks:
        stage3_status.setdefault(t, {})["final"] = "4H_SURVIVOR"
    for t in top_etfs:
        stage3_status.setdefault(t, {})["final"] = "ETF_SURVIVOR"

    state = {
        **s2,
        "top": trimmed,
        "top_stocks": kept_stocks,
        "top_etfs": top_etfs,
        "setups": setups,
        "no_4h_data": no_4h_data,
        "dropped_no_setup": dropped_no_setup,
        "stage3_created": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save(STAGE3, state)
    save_diagnostics(
        "stage3",
        input_n=len(top),
        passed=len(trimmed),
        failed=len(top) - len(trimmed),
        reasons=stage3_reasons,
        ticker_status=stage3_status,
    )
    print_stage_diagnostics("stage3", len(top), len(trimmed),
                            len(top) - len(trimmed), stage3_reasons)

    print("==== NIGHTLY STAGE 3 — 4H SETUP TRIM ====")
    print(f"Daily survivors    : {len(top)}")
    print(f"No/short 4H data   : {len(no_4h_data)}")
    print(f"No active 4H setup : {len(dropped_no_setup)}")
    print(f"Cut by cap ({cap})   : {len(cut_by_cap)}")
    print(f"Stocks kept        : {len(kept_stocks)}")
    print(f"ETFs kept          : {len(top_etfs)}")
    print("==========================================")


def stage4():
    """Add options/earnings context and write the final watchlist."""
    s3 = load(STAGE3)
    top = s3["top"]
    meta = s3["meta"]
    spots = s3.get("spots", {})

    if not top:
        raise SystemExit("FATAL: Stage 3 state contains an empty top list.")

    import options_context as oc

    earn_set, earn_note = fnd.earnings_soon_set(days_ahead=7)
    stage4_reasons = Counter()
    stage4_status = {}

    for t in top:
        m = dict(meta.get(t, s3["tmeta"].get(t, {})))
        spot = float(spots.get(t, 0) or 0)
        try:
            m["options"] = oc.fetch_context(t, spot) if spot else {"liquid": False}
            stage4_status[t] = {"status": "PASS", "options_loaded": bool(spot)}
        except Exception as exc:
            reason = f"Options context error: {type(exc).__name__}"
            m["options"] = {"liquid": False, "error": reason}
            stage4_status[t] = {"status": "WARN", "reason": reason}
            stage4_reasons[reason] += 1
        if m.get("type", "stock") == "stock":
            m["earnings_soon"] = t.upper() in earn_set
        meta[t] = m

    final_tickers = sorted(set(top))
    with open(cfg.WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "tickers": final_tickers,
            "meta": meta,
            "built": dt.datetime.now(dt.timezone.utc).isoformat(),
        }, f, indent=2, default=str)

    ledger = al.load()
    earnings_soon = [t for t in top if meta.get(t, {}).get("earnings_soon")]
    info = {
        "when": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "watchlist_n": len(final_tickers),
        "csv_n": s3["csv_n"],
        "tracked": list(ledger.keys()),
        "thesis_warned": [t for t, e in ledger.items() if e.get("thesis_warned")],
        "earnings": earnings_soon,
        "screen_failed": s3["screen_failed"],
        "screen_failed_details": s3["screen_failed_details"],
        "new_entrants": (
            sorted(set(top) - set(s3.get("prev", [])))
            if s3.get("prev") else []
        ),
        "dropped": (
            sorted(set(s3.get("prev", [])) - set(top))
            if s3.get("prev") else []
        ),
        "top": s3["top_ranked"],
        "no_data": s3["no_data"],
        "short_history": s3["short_history"],
        "tier_counts": s3["tier_counts"],
        "bulk_note": s3["bulk_note"],
        "earnings_note": earn_note,
    }
    save(STAGE4, {"info": info, "watchlist": {"tickers": final_tickers, "meta": meta}})
    save_diagnostics(
        "stage4",
        input_n=len(top),
        passed=len(final_tickers),
        failed=0,
        reasons=stage4_reasons,
        ticker_status=stage4_status,
    )
    print_stage_diagnostics("stage4", len(top), len(final_tickers), 0,
                            stage4_reasons)

    print("==== NIGHTLY STAGE 4 — CONTEXT + FINALIZE ====")
    print(earn_note)
    print(f"Final watchlist    : {len(final_tickers)}")
    print("===============================================")


def stage5():
    """Send the final nightly notification. No expensive API calls."""
    s4 = load(STAGE4)
    info = s4["info"]
    note = notify.format_nightly(info)
    save_diagnostics(
        "stage5",
        input_n=info.get("watchlist_n", 0),
        passed=info.get("watchlist_n", 0),
        failed=0,
        status="completed",
    )
    print("\n" + note)
    notify.send_email(
        f"Nightly complete — watchlist {info['watchlist_n']} ({info['when']} UTC)",
        note,
        cfg,
    )
    notify.send_telegram(note, cfg)


def main():
    stage = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    if stage == "stage1":
        stage1()
    elif stage == "stage2":
        stage2()
    elif stage == "stage3":
        stage3()
    elif stage == "stage4":
        stage4()
    elif stage == "stage5":
        stage5()
    elif stage == "all":
        stage1(); stage2(); stage3(); stage4(); stage5()
    else:
        raise SystemExit(
            "Usage: python nightly.py stage1|stage2|stage3|stage4|stage5|all"
        )


if __name__ == "__main__":
    main()
