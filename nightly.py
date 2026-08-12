"""nightly.py — staged nightly watchlist pipeline.

Stages are deliberately small so GitHub Actions can run each trimming level
as a separate job with its own timeout. Intermediate state is passed through
JSON artifacts; the final watchlist.json is written only in stage 3.

  stage1: FMP bulk fundamentals / market-cap funnel
  stage2: Twelve Data daily technical ranking
  stage3: options + earnings context and final watchlist
  stage4: notification only (no API work)
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
DIAGNOSTICS = "nightly_diagnostics.json"


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
    """Trim the universe using FMP bulk profile/TTM data."""
    import universe

    tickers, tmeta = universe.load()
    etf_set = {
        t for t in tickers
        if tmeta.get(t, {}).get("type") == "etf"
        or t in getattr(cfg, "ETF_TICKERS", [])
    }
    stocks = [t for t in tickers if t not in etf_set]

    profiles, ratios, bulk_note = fnd.bulk_context(stocks)

    # Never allow an upstream FMP outage/plan limitation to silently turn
    # every stock into a "fundamental failure" and publish an ETF-only list.
    if stocks and (not profiles or not ratios):
        msg = (
            "FMP bulk fundamentals unavailable. "
            f"profiles={len(profiles)}/{len(stocks)}, "
            f"ratios={len(ratios)}/{len(stocks)}. "
            "Do not publish an ETF-only watchlist."
        )
        save_diagnostics(
            "stage1",
            input_n=len(stocks),
            failed=len(stocks),
            reasons={"FMP bulk data unavailable": len(stocks)},
            api_errors={"FMP bulk": msg},
            status="failed",
            error=msg,
        )
        raise SystemExit("FATAL: " + msg)

    eligible_stocks = []
    meta = {}
    tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "UNKNOWN": 0}
    screen_failed = []
    screen_failed_details = []

    screen_failed_details = {}
    stage1_reasons = Counter()
    stage1_status = {}

    for t in stocks:
        scr = fnd.company_screen(
            t, cfg, profiles.get(t), ratios.get(t), is_etf=False
        )
        tier = scr.get("tier", "UNKNOWN")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

        m = dict(tmeta.get(t, {}))
        m["name"] = m.get("name") or scr.get("name") or ""
        m["screen"] = {
            "pass": scr.get("pass", True),
            "notes": scr.get("notes", [])[:3],
            "sector": scr.get("sector"),
            "industry": scr.get("industry"),
            "tier": tier,
            "market_cap": scr.get("market_cap"),
            "debt_to_equity": scr.get("debt_to_equity"),
            "net_margin": scr.get("net_margin"),
            "avg_dollar_volume": scr.get("avg_dollar_volume"),
        }
        meta[t] = m

        if scr.get("pass", True):
            eligible_stocks.append(t)
            stage1_status[t] = {"status": "PASS", "tier": tier}
        else:
            screen_failed.append(t)
            notes = scr.get("notes", [])[:3] or ["Quality filter failed"]
            screen_failed_details[t] = notes
            stage1_status[t] = {"status": "FAIL", "tier": tier, "reasons": notes}
            for reason in notes:
                stage1_reasons[reason] += 1

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

    print("==== NIGHTLY STAGE 1 — FMP FUNDAMENTAL FUNNEL ====")
    print(f"CSV unique tickers : {len(tickers)}")
    print(f"Stocks             : {len(stocks)}")
    print(f"ETFs (unfiltered)  : {len(etf_set)}")
    print(f"FMP bulk            : {bulk_note}")
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

    d = data.fetch_daily(eligible)
    spy = d.get("SPY")
    if spy is None:
        raise SystemExit(
            "FATAL: no SPY data from Twelve Data — check TWELVEDATA_KEY and quota."
        )

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
    """Add options/earnings context and write the final watchlist."""
    s2 = load(STAGE2)
    top = s2["top"]
    meta = s2["meta"]
    spots = s2.get("spots", {})

    if not top:
        raise SystemExit("FATAL: Stage 2 state contains an empty top list.")

    import options_context as oc

    earn_set, earn_note = fnd.earnings_soon_set(days_ahead=7)
    stage3_reasons = Counter()
    stage3_status = {}

    for t in top:
        m = dict(meta.get(t, s2["tmeta"].get(t, {})))
        spot = float(spots.get(t, 0) or 0)
        try:
            m["options"] = oc.fetch_context(t, spot) if spot else {"liquid": False}
            stage3_status[t] = {"status": "PASS", "options_loaded": bool(spot)}
        except Exception as exc:
            reason = f"Options context error: {type(exc).__name__}"
            m["options"] = {"liquid": False, "error": reason}
            stage3_status[t] = {"status": "WARN", "reason": reason}
            stage3_reasons[reason] += 1
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
        "csv_n": s2["csv_n"],
        "tracked": list(ledger.keys()),
        "thesis_warned": [t for t, e in ledger.items() if e.get("thesis_warned")],
        "earnings": earnings_soon,
        "screen_failed": s2["screen_failed"],
        "screen_failed_details": s2["screen_failed_details"],
        "new_entrants": (
            sorted(set(top) - set(s2.get("prev", [])))
            if s2.get("prev") else []
        ),
        "dropped": (
            sorted(set(s2.get("prev", [])) - set(top))
            if s2.get("prev") else []
        ),
        "top": s2["top_ranked"],
        "no_data": s2["no_data"],
        "short_history": s2["short_history"],
        "tier_counts": s2["tier_counts"],
        "bulk_note": s2["bulk_note"],
        "earnings_note": earn_note,
    }
    save(STAGE3, {"info": info, "watchlist": {"tickers": final_tickers, "meta": meta}})
    save_diagnostics(
        "stage3",
        input_n=len(top),
        passed=len(final_tickers),
        failed=0,
        reasons=stage3_reasons,
        ticker_status=stage3_status,
    )
    print_stage_diagnostics("stage3", len(top), len(final_tickers), 0,
                            stage3_reasons)

    print("==== NIGHTLY STAGE 3 — CONTEXT + FINALIZE ====")
    print(earn_note)
    print(f"Final watchlist    : {len(final_tickers)}")
    print("===============================================")


def stage4():
    """Send the final nightly notification. No expensive API calls."""
    s3 = load(STAGE3)
    info = s3["info"]
    note = notify.format_nightly(info)
    save_diagnostics(
        "stage4",
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
    elif stage == "all":
        stage1(); stage2(); stage3(); stage4()
    else:
        raise SystemExit("Usage: python nightly.py stage1|stage2|stage3|stage4|all")


if __name__ == "__main__":
    main()
