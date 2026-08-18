"""explain.py — Why did (or didn't) a ticker produce a signal?

Walks each symbol through every gate in the live pipeline, prints the result
and writes docs/explain.json so docs/explain.html can render it.

Usage:
  TWELVEDATA_KEY=... python explain.py GLD
  TWELVEDATA_KEY=... python explain.py "GLD,SOXL,NVDA,SLV"
  TWELVEDATA_KEY=... python explain.py watchlist     # everything on the watchlist
  TWELVEDATA_KEY=... python explain.py alerted       # everything currently muted
"""
import datetime as dt
import json
import os
import sys

import analysis
import config as cfg
import data
import universe
import fundamentals as fnd

GATES = ["fundamental / quality", "daily trend", "4h setup", "1h trigger", "quality score",
         "target 9-20%", "fee check"]


def explain_one(sym, spy_daily, meta, ledger):
    """Explain the live pipeline without hiding downstream technical diagnostics.

    Fundamental quality is a hard signal gate in scan.py, but Explain continues
    through the technical timeframes so the user can see whether a stock is
    technically attractive even when fundamentals block the trade.
    """
    out = {"ticker": sym, "name": meta.get("name", ""),
           "type": meta.get("type", "stock"), "sector": meta.get("sector", ""),
           "gates": [], "stopped_at": None, "blocked_by": [], "muted": None, "price": None}
    is_etf = out["type"] == "etf"

    if sym in ledger:
        e = ledger[sym]
        out["muted"] = {"entry": e.get("entry"), "tp": e.get("tp"),
                        "alerted": str(e.get("alerted", ""))[:10],
                        "thesis_warned": bool(e.get("thesis_warned"))}

    # Reuse the exact cached nightly FMP screen when available.
    # IMPORTANT: a missing cached screen is NOT a pass. Older watchlists can
    # lack this field, so fall back to a live FMP screen rather than silently
    # telling the user that fundamentals passed.
    screen = meta.get("screen")
    screen_source = "cached nightly FMP"
    if isinstance(screen, dict) and "pass" in screen:
        screen_pass = bool(screen.get("pass"))
        screen_notes = list(screen.get("notes") or [])
    else:
        screen_source = "live FMP fallback"
        try:
            live_screen = fnd.company_screen(sym, cfg, is_etf=is_etf)
            screen_pass = bool(live_screen.get("pass", False))
            screen_notes = list(live_screen.get("notes") or [])
            if not meta.get("sector"):
                out["sector"] = live_screen.get("sector") or out["sector"]
        except Exception as exc:
            # Unknown is safer than a false PASS. Do not let a missing cache
            # turn into a fake fundamental approval.
            screen_pass = False
            screen_notes = [f"Unable to verify FMP quality screen: {type(exc).__name__}"]
            screen_source = "verification error"

    # Older watchlists only stored "microcap, below quality floor". Make the
    # explanation explicit without changing the actual gate.
    # Older watchlists only stored "microcap, below quality floor". Make the
    # explanation explicit without changing the actual gate.
    if any("microcap" in str(n).lower() for n in screen_notes):
        screen_notes = [
            f"Market cap below ${cfg.SMALLCAP_MIN_MARKETCAP/1e6:.0f}M quality floor"
            if "market cap" not in str(n).lower() else str(n)
            for n in screen_notes
        ]
    if screen_pass:
        fund_detail = f"Pass — verified by {screen_source}; no FMP quality rule is currently blocking this ticker."
    else:
        fund_detail = "Failed — " + "; ".join(screen_notes[:3])
    out["fundamental"] = {"pass": screen_pass, "notes": screen_notes}
    out["gates"].append({"gate": "fundamental / quality", "pass": screen_pass,
                         "detail": fund_detail})
    if not screen_pass:
        out["blocked_by"].append("fundamental / quality")

    h1 = data.fetch_intraday([sym]).get(sym)
    if h1 is None or len(h1) < 100:
        out["stopped_at"] = "no data"
        out["gates"].append({"gate": "data", "pass": False,
                             "detail": "no intraday data from Twelve Data"})
        out["blocked_by"].append("data")
        out["final"] = {"pass": False, "status": "NO DATA",
                         "reason": "No intraday data was available."}
        return out

    # Intended multi-timeframe architecture:
    # Daily = direction, 4H = setup, 1H = entry trigger.
    h4, daily = data.resample(h1, "4h"), data.resample(h1, "1D")
    price = float(h1["close"].iloc[-1])
    out["price"] = round(price, 2)

    t = analysis.daily_trend(daily, spy_daily, cfg)
    daily_ok = bool(t["uptrend"] and t["strong"])
    out["trend"] = t
    out["gates"].append({"gate": "daily trend", "pass": daily_ok, "detail":
        f"EMA20 ${t['ema20']} vs EMA50 ${t['ema50']} → uptrend {t['uptrend']}; "
        f"ADX {t['adx']} (needs ≥{cfg.ADX_MIN}); RS vs SPY {t['rs']:+.1%}; "
        f"score {t['score']}/5; {t['regime']} ~{t['regime_weeks']}w; "
        f"{'above' if t['above_ema200'] else 'below'} 200 EMA"})
    if not daily_ok:
        out["blocked_by"].append("daily trend")
    
    st = analysis.setup_4h(h4, cfg)
    out["setup_4h"] = st
    setup_ok = bool(st)
    out["gates"].append({"gate": "4h setup", "pass": setup_ok,
                         "detail": st or "No pullback-to-EMA20 or qualifying range breakout"})
    if not setup_ok:
        out["blocked_by"].append("4h setup")

    trig = analysis.trigger_1h(h1)
    last, prev = h1.iloc[-1], h1.iloc[-2]
    rng = float(last.high - last.low)
    pos = (float(last.close) - float(last.low)) / rng if rng else 0
    out["gates"].append({"gate": "1h trigger", "pass": bool(trig), "detail":
        f"last 1h bar closed {pos:.0%} up its range, "
        f"{'above' if float(last.close) > float(prev.high) else 'below'} the prior bar high"})
    if not trig:
        out["blocked_by"].append("1h trigger")

    # Only calculate downstream entry gates when their prerequisites exist.
    q = None
    if setup_ok and trig:
        q = analysis.quality_score(h1, h4, st, cfg, entry_hint=price)
        floor = cfg.SCORE_MIN_ETF if is_etf else cfg.SCORE_MIN_STOCK
        out["quality"] = {"total": q["total"], "floor": floor,
                          "detail": q["detail"], "notes": q["notes"]}
        q_ok = q["total"] >= floor
        out["gates"].append({"gate": "quality score", "pass": q_ok,
                             "detail": f"{q['total']} vs floor {floor} ({'ETF' if is_etf else 'stock'})"})
        if not q_ok:
            out["blocked_by"].append("quality score")

        tp = analysis.calc_tp(daily, h4, price, cfg, trend=t)
        tp_ok = bool(tp)
        out["gates"].append({"gate": "target 9-20%", "pass": tp_ok, "detail":
            (f"${tp:.2f} (+{(tp / price - 1) * 100:.1f}%)" if tp else
             "no Fibonacci extension or resistance level inside the 9-20% window")})
        if not tp_ok:
            out["blocked_by"].append("target 9-20%")

        if tp_ok and q_ok:
            value = max(cfg.ACCOUNT_SIZE * cfg.POSITION_PCT, cfg.MIN_TRADE_USD)
            shares = round(value / price, cfg.SHARE_DECIMALS)
            okf, frac = analysis.fee_check(price, tp, shares, is_etf, cfg)
            out["gates"].append({"gate": "fee check", "pass": okf,
                                 "detail": f"fees {frac:.1%} of expected profit"})
            if not okf:
                out["blocked_by"].append("fee check")
        else:
            out["gates"].append({"gate": "fee check", "pass": False,
                                 "detail": "Not evaluated — upstream quality/target gate failed"})
    else:
        out["gates"].append({"gate": "quality score", "pass": False,
                             "detail": "Not evaluated — 4H setup or 1H trigger failed"})
        out["gates"].append({"gate": "target 9-20%", "pass": False,
                             "detail": "Not evaluated — no confirmed entry setup"})
        out["gates"].append({"gate": "fee check", "pass": False,
                             "detail": "Not evaluated — no confirmed entry setup"})

    # Final signal requires ALL live gates. Fundamental is a hard blocker even
    # when the technical side is attractive.
    all_technical = daily_ok and setup_ok and bool(trig)
    if q is not None:
        all_technical = all_technical and q["total"] >= (cfg.SCORE_MIN_ETF if is_etf else cfg.SCORE_MIN_STOCK)
    final_pass = screen_pass and all_technical and not any(
        g["gate"] in {"target 9-20%", "fee check"} and not g["pass"] for g in out["gates"]
    )

    if final_pass:
        # Reuse the last computed target if available.
        tp = next((g for g in out["gates"] if g["gate"] == "target 9-20%" and g["pass"]), None)
        # Extract exact target from gate detail for dashboard only; calculate again
        # to keep the output numeric and authoritative.
        target = analysis.calc_tp(daily, h4, price, cfg, trend=t)
        value = max(cfg.ACCOUNT_SIZE * cfg.POSITION_PCT, cfg.MIN_TRADE_USD)
        shares = round(value / price, cfg.SHARE_DECIMALS)
        out["would_alert"] = {"entry": round(price, 2), "tp": round(target, 2),
                              "tp_pct": round((target / price - 1) * 100, 1),
                              "shares": shares, "value": round(shares * price, 2)}
        out["why"] = "All fundamental, daily trend, 4H setup, 1H trigger, quality, target and fee gates passed."
        out["final"] = {"pass": True, "status": "WOULD ALERT", "reason": out["why"]}
    else:
        # Keep the first blockers in pipeline order, with the fundamental reason
        # first when applicable.
        priority = ["fundamental / quality", "daily trend", "4h setup", "1h trigger",
                    "quality score", "target 9-20%", "fee check"]
        blockers = [x for x in priority if x in out["blocked_by"]]
        out["stopped_at"] = blockers[0] if blockers else "blocked"
        if not screen_pass:
            fund_reason = "; ".join(screen_notes[:2]) or "quality filter failed"
            tech_state = "Technical analysis is shown for diagnostics but does not override the fundamental block."
            out["why"] = f"🔴 Fundamental / quality filter failed: {fund_reason}. {tech_state}"
        else:
            out["why"] = "🔴 Blocked by: " + ", ".join(blockers) + "."
        out["final"] = {"pass": False, "status": "BLOCKED", "reason": out["why"],
                         "blocked_by": blockers}
    return out

def resolve_symbols(arg, ledger):
    arg = (arg or "GLD").strip()
    if arg.lower() == "alerted":
        return sorted(ledger.keys())
    if arg.lower() == "watchlist":
        try:
            return json.load(open(cfg.WATCHLIST_FILE))["tickers"]
        except Exception:
            return []
    return [s.strip().upper() for s in arg.split(",") if s.strip()]


def main():
    try:
        ledger = json.load(open("alerted.json"))
    except Exception:
        ledger = {}
    syms = resolve_symbols(sys.argv[1] if len(sys.argv) > 1 else "", ledger)
    if not syms:
        raise SystemExit("no symbols to explain")

    _, meta = universe.load()
    spy = data.fetch_intraday(["SPY"]).get("SPY")
    if spy is None:
        raise SystemExit("no SPY data — check TWELVEDATA_KEY / rate limits")
    spy_daily = data.resample(spy, "1D")

    results = []
    for s in syms:
        r = explain_one(s, spy_daily, meta.get(s, {}), ledger)
        results.append(r)
        head = f"{r['ticker']}" + (f" — {r['name']}" if r["name"] else "")
        print(f"\n{'=' * 60}\n{head}   ${r.get('price', '?')}\n{'=' * 60}")
        if r.get("muted"):
            m = r["muted"]
            print(f"  MUTED: alerted {m['alerted']} at ${m['entry']}, "
                  f"target ${m['tp']} — will not re-alert until price clears it")
        for g in r["gates"]:
            print(f"  [{'PASS' if g['pass'] else 'FAIL'}] {g['gate']:<14} {g['detail']}")
        print(f"  -> {r.get('why', '')}")
        if r.get("blocked_by"):
            print("  BLOCKED BY: " + ", ".join(r["blocked_by"]))

    os.makedirs("docs", exist_ok=True)
    with open("docs/explain.json", "w") as f:
        json.dump({"generated": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "results": results}, f, indent=2, default=str)
    print(f"\nSaved docs/explain.json ({len(results)} ticker(s))")

    from collections import Counter
    c = Counter(r.get("stopped_at") or "would alert" for r in results)
    print("\nWhere they stopped:")
    for k, v in c.most_common():
        print(f"  {k:<16} {v}")


if __name__ == "__main__":
    main()
