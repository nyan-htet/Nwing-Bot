"""nightly.py — staged nightly funnel.

Funnel:
  Stage 1: FMP Company Screener -> cap/liquidity funnel, <=500 stocks.
  Stage 2: Twelve Data daily regime -> <=250 stocks.
  Stage 3: Twelve Data 4H setup -> <=150 stocks.
  Stage 4: earnings/options enrichment + publish.
  Stage 5: notification only.

ETFs are never filtered by market-cap or stock technical gates.
Diagnostics are written after every stage to nightly_diagnostics.json.
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

DAILY_STOCK_CAP = int(os.getenv("NIGHTLY_DAILY_STOCK_CAP", "500"))
FOUR_H_STOCK_CAP = int(os.getenv("NIGHTLY_4H_STOCK_CAP", "150"))


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


def save_diag(stage, input_n=0, passed=0, failed=0, reasons=None,
              ticker_status=None, api_errors=None, status="completed",
              error=None):
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
        save(DIAGNOSTICS, existing)
    except Exception as exc:
        print(f"WARNING: diagnostics write failed: {exc}")


def print_diag(stage, input_n, passed, failed, reasons):
    print(f"\n==== DIAGNOSTICS — {stage.upper()} ====")
    print(f"Input  : {input_n}")
    print(f"Passed : {passed}")
    print(f"Failed : {failed}")
    for reason, count in sorted((reasons or {}).items(),
                                key=lambda x: (-x[1], x[0])):
        print(f"  - {reason}: {count}")
    print("======================================")


def _daily_rank(t, df, spy):
    tr = analysis.daily_trend(df, spy, cfg)
    return float(tr.get("score", 0) or 0), float(tr.get("rs", 0) or 0), tr


def _four_h_setup(t, df):
    """Evaluate only the 4H setup; 1H entry remains the hourly scanner's job."""
    if df is None or len(df) < 80:
        return None, "insufficient 4H history"
    try:
        setup = analysis.setup_4h(df, cfg)
        if not setup:
            return None, "4H setup unavailable / no qualifying setup"
        return setup, None
    except Exception as exc:
        return None, f"4H calculation error: {type(exc).__name__}"


def stage1():
    import universe

    tickers, tmeta = universe.load()
    etfs = [t for t in tickers if tmeta.get(t, {}).get("type") == "etf"
            or t in getattr(cfg, "ETF_TICKERS", [])]
    stocks = [t for t in tickers if t not in set(etfs)]

    try:
        result = fnd.screener_context(
            stocks,
            tmeta,
            stock_cap=DAILY_STOCK_CAP,
        )
    except Exception as exc:
        msg = f"FMP screener failure: {type(exc).__name__}: {exc}"
        save_diag("stage1", len(stocks), 0, len(stocks),
                  {"FMP screener failure": len(stocks)},
                  api_errors={"FMP": str(exc)}, status="failed", error=msg)
        raise SystemExit("FATAL: " + msg)

    eligible_stocks = result["eligible_stocks"]
    eligible_etfs = result["etfs"]
    meta = result["meta"]
    failed = result["failed"]
    reasons = Counter()
    ticker_status = {}

    for t in eligible_stocks:
        tier = meta.get(t, {}).get("screen", {}).get("tier", "UNKNOWN")
        ticker_status[t] = {"status": "PASS", "tier": tier}
    for t, rs in failed.items():
        ticker_status[t] = {
            "status": "FAIL",
            "tier": meta.get(t, {}).get("screen", {}).get("tier", "UNKNOWN"),
            "reasons": rs,
        }
        for reason in rs:
            reasons[reason] += 1

    eligible = eligible_stocks + sorted(eligible_etfs)
    try:
        prev = set(load(cfg.WATCHLIST_FILE).get("tickers", []))
    except Exception:
        prev = set()

    state = {
        "csv_n": len(tickers),
        "tickers": tickers,
        "tmeta": tmeta,
        "stocks": stocks,
        "etf_set": sorted(eligible_etfs),
        "eligible_stocks": eligible_stocks,
        "eligible": eligible,
        "meta": meta,
        "tier_counts": result["tier_counts"],
        "screen_failed": sorted(failed),
        "screen_failed_details": failed,
        "bulk_note": result["note"],
        "prev": sorted(prev),
        "created": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save(STAGE1, state)
    save_diag("stage1", len(stocks), len(eligible_stocks), len(stocks) - len(eligible_stocks),
              reasons, ticker_status)
    print_diag("stage1", len(stocks), len(eligible_stocks),
               len(stocks) - len(eligible_stocks), reasons)

    print("==== NIGHTLY STAGE 1 — FMP UNIVERSE FUNNEL ====")
    print(f"CSV unique tickers : {len(tickers)}")
    print(f"Stocks             : {len(stocks)}")
    print(f"ETFs (unfiltered)  : {len(eligible_etfs)}")
    print(f"FMP screener       : {result['note']}")
    print(f"Tier counts        : {result['tier_counts']}")
    print(f"Daily TD stock cap : {DAILY_STOCK_CAP}")
    print("==============================================")


def stage2():
    s1 = load(STAGE1)
    stocks = s1["eligible_stocks"]
    etfs = s1["etf_set"]
    meta = s1["meta"]

    if not stocks and not etfs:
        raise SystemExit("FATAL: Stage 1 produced no eligible symbols.")

    # Daily data is the first expensive TD layer.
    d = data.fetch_daily(stocks + etfs)
    spy = d.get("SPY")
    if spy is None:
        save_diag("stage2", len(stocks), 0, len(stocks),
                  {"SPY benchmark unavailable": len(stocks)},
                  status="failed", error="No SPY daily data")
        raise SystemExit("FATAL: no SPY daily data from Twelve Data.")

    scored = []
    reasons = Counter()
    status = {}

    for t in stocks:
        df = d.get(t)
        if df is None:
            reasons["No Twelve Data daily data"] += 1
            status[t] = {"status": "FAIL", "reason": "No daily data"}
            continue
        if len(df) < 120:
            reasons["Insufficient daily history"] += 1
            status[t] = {"status": "FAIL", "reason": "Insufficient daily history"}
            continue
        try:
            score, rs, trend = _daily_rank(t, df, spy)
            tier = meta.get(t, {}).get("screen", {}).get("tier", "A")

            # Stronger daily bar for smaller tiers.
            # We use the existing 0–5 trend score rather than confusing it
            # with the 0–1 entry-quality score used by hourly-scan.
            min_trend = 2.5 if tier == "B" else 3.0 if tier == "C" else 2.0
            if score < min_trend:
                reasons[f"Tier {tier}: daily trend score below {min_trend:.1f}"] += 1
                status[t] = {"status": "FAIL", "reason": "Daily trend threshold",
                             "score": score, "rs": rs, "tier": tier}
                continue

            combined = score + rs
            scored.append((combined, t, trend))
            status[t] = {"status": "RANKED", "score": score, "rs": rs,
                         "combined": combined, "tier": tier}
        except Exception as exc:
            reason = f"Daily technical error: {type(exc).__name__}"
            reasons[reason] += 1
            status[t] = {"status": "FAIL", "reason": reason}

    scored.sort(reverse=True, key=lambda x: x[0])
    top_stocks = [t for _, t, _ in scored[:DAILY_STOCK_CAP]]
    # ETFs are never filtered by stock ranking.
    top_etfs = [t for t in etfs if t in d and len(d[t]) >= 60]
    top = top_stocks + top_etfs

    for t in top_stocks:
        status.setdefault(t, {})["final"] = "DAILY_SURVIVOR"
    for t in top_etfs:
        status.setdefault(t, {})["final"] = "ETF_UNFILTERED"

    if not top:
        raise SystemExit("FATAL: Stage 2 produced no daily survivors.")

    spots = {t: float(d[t]["close"].iloc[-1]) for t in top if t in d and len(d[t])}

    state = {
        **s1,
        "top_stocks": top_stocks,
        "top_etfs": top_etfs,
        "top": top,
        "daily_status": status,
        "daily_reasons": dict(reasons),
        "spots": spots,
        "top_ranked": [[t, sc, tr.get("rs", 0)] for sc, t, tr in scored[:20]],
        "technical_created": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save(STAGE2, state)
    save_diag("stage2", len(stocks), len(top_stocks), len(stocks) - len(top_stocks),
              reasons, status)
    print_diag("stage2", len(stocks), len(top_stocks),
               len(stocks) - len(top_stocks), reasons)

    print("==== NIGHTLY STAGE 2 — DAILY TECHNICAL TRIM ====")
    print(f"Input stocks       : {len(stocks)}")
    print(f"Daily survivors    : {len(top_stocks)}")
    print(f"ETFs retained      : {len(top_etfs)}")
    print("================================================")


def stage3():
    s2 = load(STAGE2)
    stocks = s2["top_stocks"]
    etfs = s2["top_etfs"]
    meta = s2["meta"]

    four_h_status = {}
    reasons = Counter()
    scored = []

    # Deep technical only for the smaller stock set.
    for t in stocks:
        try:
            raw = data.fetch_td([t], interval="4h", outputsize=220)
            df = raw.get(t) if isinstance(raw, dict) else None
            setup, error = _four_h_setup(t, df)
            if error:
                reasons[error] += 1
                four_h_status[t] = {"status": "FAIL", "reason": error}
                continue

            tier = meta.get(t, {}).get("screen", {}).get("tier", "A")
            # A setup is required for the deep technical shortlist.
            # B/C are not rejected just because their setup is weaker;
            # they simply need a stronger daily gate already applied in stage 2.
            setup_text = str(setup)
            bonus = 0.10 if tier == "C" else 0.0
            score = bonus
            if "pullback" in setup_text.lower():
                score += 1.0
            if "breakout" in setup_text.lower():
                score += 1.0
            scored.append((score, t, setup))
            four_h_status[t] = {"status": "PASS", "setup": setup, "tier": tier}
        except Exception as exc:
            reason = f"4H data/calculation error: {type(exc).__name__}"
            reasons[reason] += 1
            four_h_status[t] = {"status": "FAIL", "reason": reason}

    # Prefer stocks with a recognizable setup, then preserve the strongest
    # daily-ranked candidates if fewer than the cap have setups.
    scored.sort(reverse=True, key=lambda x: x[0])
    top_stocks = [t for _, t, _ in scored[:FOUR_H_STOCK_CAP]]

    # ETFs remain untouched by stock 4H filtering.
    top = top_stocks + etfs
    if not top:
        raise SystemExit("FATAL: Stage 3 produced no final technical candidates.")

    spots = s2.get("spots", {})
    final_meta = dict(meta)
    for t in top_stocks:
        m = dict(final_meta.get(t, {}))
        m["nightly_4h"] = four_h_status.get(t, {})
        final_meta[t] = m

    state = {
        **s2,
        "top_stocks": top_stocks,
        "top_etfs": etfs,
        "top": top,
        "meta": final_meta,
        "four_h_status": four_h_status,
        "four_h_reasons": dict(reasons),
        "spots": spots,
        "technical_created": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save(STAGE3, state)
    save_diag("stage3", len(stocks), len(top_stocks), len(stocks) - len(top_stocks),
              reasons, four_h_status)
    print_diag("stage3", len(stocks), len(top_stocks),
               len(stocks) - len(top_stocks), reasons)

    print("==== NIGHTLY STAGE 3 — 4H SETUP TRIM ====")
    print(f"Input stocks       : {len(stocks)}")
    print(f"4H survivors       : {len(top_stocks)}")
    print(f"ETFs retained      : {len(etfs)}")
    print("===========================================")


def stage4():
    s3 = load(STAGE3)
    top = s3["top"]
    meta = s3["meta"]
    spots = s3.get("spots", {})

    if not top:
        raise SystemExit("FATAL: Stage 3 state contains no candidates.")

    import options_context as oc

    earn_set, earn_note = fnd.earnings_soon_set(days_ahead=7)
    context_status = {}
    reasons = Counter()

    for t in top:
        m = dict(meta.get(t, s3.get("tmeta", {}).get(t, {})))
        spot = float(spots.get(t, 0) or 0)
        try:
            m["options"] = oc.fetch_context(t, spot) if spot else {"liquid": False}
            context_status[t] = {"status": "PASS"}
        except Exception as exc:
            reason = f"Options context error: {type(exc).__name__}"
            m["options"] = {"liquid": False, "error": reason}
            context_status[t] = {"status": "WARN", "reason": reason}
            reasons[reason] += 1

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
    earnings = [t for t in top if meta.get(t, {}).get("earnings_soon")]

    info = {
        "when": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "watchlist_n": len(final_tickers),
        "csv_n": s3["csv_n"],
        "tracked": list(ledger.keys()),
        "thesis_warned": [t for t, e in ledger.items() if e.get("thesis_warned")],
        "earnings": earnings,
        "screen_failed": s3["screen_failed"],
        "screen_failed_details": s3["screen_failed_details"],
        "new_entrants": sorted(set(top) - set(s3.get("prev", []))) if s3.get("prev") else [],
        "dropped": sorted(set(s3.get("prev", [])) - set(top)) if s3.get("prev") else [],
        "top": s3.get("top_ranked", []),
        "daily_reasons": s3.get("daily_reasons", {}),
        "four_h_reasons": s3.get("four_h_reasons", {}),
        "tier_counts": s3["tier_counts"],
        "bulk_note": s3["bulk_note"],
        "earnings_note": earn_note,
    }
    save(STAGE4, {"info": info, "watchlist": {"tickers": final_tickers, "meta": meta}})
    save_diag("stage4", len(top), len(final_tickers), 0, reasons, context_status)

    print("==== NIGHTLY STAGE 4 — CONTEXT + FINALIZE ====")
    print(earn_note)
    print(f"Final watchlist    : {len(final_tickers)}")
    print("===============================================")


def stage5():
    s4 = load(STAGE4)
    info = s4["info"]
    note = notify.format_nightly(info)
    print("\n" + note)
    notify.send_email(
        f"Nightly complete — watchlist {info['watchlist_n']} ({info['when']} UTC)",
        note,
        cfg,
    )
    notify.send_telegram(note, cfg)
    save_diag("stage5", info.get("watchlist_n", 0),
              info.get("watchlist_n", 0), 0, status="completed")


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
