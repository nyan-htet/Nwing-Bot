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


def _tier_of(t, meta):
    return meta.get(t, {}).get("screen", {}).get("tier")


def build_universe_report(s3, final_tickers, meta):
    """Full stock/ETF disposition across stage1-3, tier by tier, for the
    nightly universe report email. Purely a summary — makes no API calls
    and doesn't affect the watchlist itself.
    """
    etf_set = set(s3.get("etf_set", []))
    top_etfs = set(s3.get("top_etfs", []))
    final_set = set(final_tickers)

    # ---- Watchlist (final survivors) ----
    watch_stocks_by_tier = {"A": [], "B": [], "C": []}
    watch_etfs = sorted(t for t in final_set if t in etf_set)
    for t in sorted(final_set - etf_set):
        tier = _tier_of(t, meta) or "UNKNOWN"
        watch_stocks_by_tier.setdefault(tier, []).append(t)

    # ---- Every rejection reason across all 3 stages, merged by ticker ----
    all_failures = {}
    for t, notes in (s3.get("screen_failed_details") or {}).items():
        all_failures.setdefault(t, []).extend(notes)
    for t in s3.get("no_data", []):
        all_failures.setdefault(t, []).append("No Twelve Data daily data")
    for t in s3.get("short_history", []):
        all_failures.setdefault(t, []).append("Insufficient daily history")
    for t in s3.get("tier_floor_failed", []):
        all_failures.setdefault(t, []).append("Below tier daily score floor")
    for t in s3.get("no_4h_data", []):
        all_failures.setdefault(t, []).append("No/short 4H data")
    for t in s3.get("dropped_no_setup", []):
        all_failures.setdefault(t, []).append("No active 4H setup")
    for t in s3.get("cut_by_cap", []):
        all_failures.setdefault(t, []).append("Cut by 4H processing cap")

    # ---- Unknown/no-data: never classified at all (no FMP match ever) ----
    unknown_stocks = sorted(
        t for t, notes in all_failures.items()
        if t not in etf_set and any("FMP screener data unavailable" in n for n in notes)
    )
    unknown_etfs = sorted(
        t for t, notes in all_failures.items()
        if t in etf_set and any("FMP screener data unavailable" in n for n in notes)
    )

    # ---- Rejected stocks, tier-classified (everything else that failed) ----
    rejected_stocks_by_tier = {"A": [], "B": [], "C": [], "D": []}
    extra_unknown = []
    for t, notes in all_failures.items():
        if t in etf_set or t in final_set:
            continue
        if any("FMP screener data unavailable" in n for n in notes):
            continue  # already in unknown_stocks
        tier = _tier_of(t, meta)
        reason = notes[0] if notes else "Rejected"
        if tier in rejected_stocks_by_tier:
            rejected_stocks_by_tier[tier].append((t, reason))
        else:
            extra_unknown.append(t)  # no tier on record for some other reason
    unknown_stocks = sorted(set(unknown_stocks) | set(extra_unknown))

    for tier in rejected_stocks_by_tier:
        rejected_stocks_by_tier[tier].sort()

    # ---- Rejected ETFs (flat — ETFs bypass the tier system entirely) ----
    rejected_etf_tickers = sorted(etf_set - top_etfs - set(unknown_etfs))
    rejected_etfs = [(t, "No/insufficient daily data") for t in rejected_etf_tickers]

    return {
        "watch_stocks_by_tier": watch_stocks_by_tier,
        "watch_etfs": watch_etfs,
        "rejected_stocks_by_tier": rejected_stocks_by_tier,
        "rejected_etfs": rejected_etfs,
        "unknown_stocks": unknown_stocks,
        "unknown_etfs": unknown_etfs,
    }


def stage1():
    """Trim the universe using the FMP company-screener (market cap + liquidity).

    Uses fnd.screener_context(), which makes a small, bounded number of
    bulk screener calls (not one call per ticker) and returns a stock
    universe already capped/tiered to cfg.DAILY_STOCK_CAP — this is the
    cost-control gate before stage 2 spends any Twelve Data calls.
    """
    import universe

    run_started = dt.datetime.now(dt.timezone.utc).isoformat()
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
        "run_started": run_started,
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

    unknown_tickers = sorted(
        t for t, notes in screen_failed_details.items()
        if any("FMP screener data unavailable" in n for n in notes)
    )
    if unknown_tickers:
        print(f"Unavailable stocks ({len(unknown_tickers)}): "
              f"{', '.join(unknown_tickers)}")

    # Liquidity sanity check: if the numbers driving these rejections are
    # implausible for large caps (e.g. lots of $0 dollar volume), that's a
    # signal the FMP screener's price/volume fields are stale, not that the
    # universe is genuinely illiquid. Spot-checked, not aggregated by note
    # text, so it doesn't blow up the reasons summary above.
    liq_failed = [
        t for t, notes in screen_failed_details.items()
        if any("dollar liquidity" in n for n in notes)
    ]
    if liq_failed:
        dvs = [meta.get(t, {}).get("screen", {}).get("dollar_volume") for t in liq_failed]
        dvs = [d for d in dvs if d is not None]
        if dvs:
            dvs_sorted = sorted(dvs)
            zero_n = sum(1 for d in dvs if d == 0)
            print(f"Liquidity-failed dollar volume — n={len(dvs)}, "
                  f"min=${dvs_sorted[0]:,.0f}, median=${dvs_sorted[len(dvs)//2]:,.0f}, "
                  f"max=${dvs_sorted[-1]:,.0f}, exactly $0: {zero_n}")
        sample = sorted(
            liq_failed,
            key=lambda t: meta.get(t, {}).get("screen", {}).get("market_cap") or 0,
            reverse=True,
        )[:10]
        print("Largest-market-cap liquidity rejects (ticker: tier, price, "
              "volume, dollar_volume):")
        for t in sample:
            scr = meta.get(t, {}).get("screen", {})
            print(f"  {t}: tier={scr.get('tier')}, price={scr.get('price')}, "
                  f"volume={scr.get('volume')}, "
                  f"dollar_volume={scr.get('dollar_volume')}")

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

    # Macro reading, computed here (not deferred to a later stage) because
    # this is the one place SPY data is *guaranteed* good — the preflight
    # above already verified it. Small, JSON-safe dict; carried through
    # stage3's state spread into stage4's notification.
    macro = fnd.macro_context(spy)

    scored, short_history, no_data, tier_floor_failed = [], [], [], []
    stage2_reasons = Counter()
    stage2_status = {}

    # Tier-specific daily trend floor (README §12): smaller companies must
    # clear a higher daily-score bar before staying on the watchlist at all.
    # ETFs are not tier-gated (they have no "screen"/tier — dict.get is None).
    tier_score_floor = {
        "A": getattr(cfg, "TIER_A_DAILY_SCORE_MIN", None),
        "B": getattr(cfg, "TIER_B_DAILY_SCORE_MIN", None),
        "C": getattr(cfg, "TIER_C_DAILY_SCORE_MIN", None),
    }

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
            tier = meta.get(t, {}).get("screen", {}).get("tier", "UNKNOWN")
            floor = tier_score_floor.get(tier)
            if floor is not None and tr["score"] < floor:
                tier_floor_failed.append(t)
                reason = f"Daily trend score {tr['score']} below Tier {tier} floor {floor}"
                stage2_status[t] = {
                    "status": "FAIL", "reason": reason, "tier": tier,
                    "score": tr.get("score"), "relative_strength": tr.get("rs"),
                }
                stage2_reasons[f"Below Tier {tier} daily score floor ({floor})"] += 1
                continue
            score = tr["score"] + tr["rs"]
            scored.append((score, t))
            stage2_status[t] = {
                "status": "RANKED",
                "tier": tier,
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
        "tier_floor_failed": tier_floor_failed,
        "top_ranked": top_ranked,
        "spots": spots,
        "macro": macro,
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
    print(f"Below tier score floor : {len(tier_floor_failed)}")
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
        "cut_by_cap": cut_by_cap,
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
        "macro_risk": (s3.get("macro") or {}).get("risk", "?"),
        "macro_vol": (s3.get("macro") or {}).get("vol", "?"),
    }

    # Runtime: from stage1's very first line (before any API calls) through
    # now — spans the whole GH Actions workflow, including inter-job setup,
    # which is what "how long did nightly take tonight" actually means.
    run_started = s3.get("run_started")
    runtime_str = "unknown"
    if run_started:
        try:
            started = dt.datetime.fromisoformat(str(run_started).replace("Z", "+00:00"))
            elapsed_s = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds())
            h, rem = divmod(elapsed_s, 3600)
            m, s_ = divmod(rem, 60)
            runtime_str = (f"{h}h {m}m {s_}s" if h else
                          f"{m}m {s_}s")
        except Exception:
            pass

    universe_report = build_universe_report(s3, final_tickers, meta)
    universe_report["runtime"] = runtime_str
    universe_report["run_started"] = run_started

    save(STAGE4, {
        "info": info,
        "watchlist": {"tickers": final_tickers, "meta": meta},
        "universe_report": universe_report,
    })
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
    """Send the final nightly notification. No expensive API calls.

    Notify failures (email/telegram) are logged but never raise — this
    stage must exit 0 so the workflow's git-publish step still runs even
    if a delivery channel is down or misbehaving.
    """
    s4 = load(STAGE4)
    info = s4["info"]
    note_email = notify.format_nightly(info)
    note_telegram = notify.format_nightly(info, for_telegram=True)
    save_diagnostics(
        "stage5",
        input_n=info.get("watchlist_n", 0),
        passed=info.get("watchlist_n", 0),
        failed=0,
        status="completed",
    )
    print("\n" + note_email)
    notify.send_email(
        f"Nightly complete — watchlist {info['watchlist_n']} ({info['when']} UTC)",
        note_email,
        cfg,
    )
    notify.send_telegram(note_telegram, cfg)

    # Separate universe report — full stock/ETF disposition tier by tier.
    # Email only, deliberately: it's a diagnostic dump, not a Telegram-sized
    # message. Never fatal if missing (older STAGE4 files won't have it).
    universe_report = s4.get("universe_report")
    if universe_report:
        ur_body = notify.format_universe_report(universe_report, info["when"])
        notify.send_email(
            f"Nightly universe report — {info['when']} UTC", ur_body, cfg
        )


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
