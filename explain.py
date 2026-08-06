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

GATES = ["daily trend", "4h setup", "1h trigger", "quality score",
         "target 9-20%", "fee check"]


def explain_one(sym, spy_daily, meta, ledger):
    out = {"ticker": sym, "name": meta.get("name", ""),
           "type": meta.get("type", "stock"), "sector": meta.get("sector", ""),
           "gates": [], "stopped_at": None, "muted": None, "price": None}

    if sym in ledger:
        e = ledger[sym]
        out["muted"] = {"entry": e.get("entry"), "tp": e.get("tp"),
                        "alerted": str(e.get("alerted", ""))[:10],
                        "thesis_warned": bool(e.get("thesis_warned"))}

    h1 = data.fetch_intraday([sym]).get(sym)
    if h1 is None or len(h1) < 100:
        out["stopped_at"] = "no data"
        out["gates"].append({"gate": "data", "pass": False,
                             "detail": "no intraday data from Twelve Data"})
        return out

    h4, daily = data.resample(h1, "4h"), data.resample(h1, "1D")
    price = float(h1["close"].iloc[-1])
    out["price"] = round(price, 2)

    t = analysis.daily_trend(daily, spy_daily, cfg)
    ok = bool(t["uptrend"] and t["strong"])
    out["trend"] = t
    out["gates"].append({"gate": "daily trend", "pass": ok, "detail":
        f"EMA20 ${t['ema20']} vs EMA50 ${t['ema50']} → uptrend {t['uptrend']}; "
        f"ADX {t['adx']} (needs ≥{cfg.ADX_MIN}); RS vs SPY {t['rs']:+.1%}; "
        f"score {t['score']}/5; {t['regime']} ~{t['regime_weeks']}w; "
        f"{'above' if t['above_ema200'] else 'below'} 200 EMA"})
    if not ok:
        out["stopped_at"] = "daily trend"
        out["why"] = ("No established daily uptrend. After a sharp reversal the "
                      "20 EMA needs weeks to cross back above the 50 EMA.")
        return out

    st = analysis.setup_4h(h4, cfg)
    out["gates"].append({"gate": "4h setup", "pass": bool(st),
                         "detail": st or "neither a pullback to the 20 EMA nor a "
                                         "20-bar range breakout on 1.5x volume"})
    if not st:
        out["stopped_at"] = "4h setup"
        out["why"] = ("Price is trending but offers no entry pattern — it is not "
                      "pulling back to the 20 EMA and not breaking a range. "
                      "A vertical move gets caught on its first pullback, not mid-run.")
        return out

    trig = analysis.trigger_1h(h1)
    last, prev = h1.iloc[-1], h1.iloc[-2]
    rng = float(last.high - last.low)
    pos = (float(last.close) - float(last.low)) / rng if rng else 0
    out["gates"].append({"gate": "1h trigger", "pass": bool(trig), "detail":
        f"last 1h bar closed {pos:.0%} up its range, "
        f"{'above' if float(last.close) > float(prev.high) else 'below'} the prior bar high"})
    if not trig:
        out["stopped_at"] = "1h trigger"
        out["why"] = ("Timing gate only — the setup is valid but the current hourly "
                      "candle is not bullish enough. It may trigger on a later scan.")
        return out

    q = analysis.quality_score(h1, h4, st, cfg, entry_hint=price)
    is_etf = meta.get("type") == "etf"
    floor = cfg.SCORE_MIN_ETF if is_etf else cfg.SCORE_MIN_STOCK
    out["quality"] = {"total": q["total"], "floor": floor,
                      "detail": q["detail"], "notes": q["notes"]}
    out["gates"].append({"gate": "quality score", "pass": q["total"] >= floor,
                         "detail": f"{q['total']} vs floor {floor} "
                                   f"({'ETF' if is_etf else 'stock'})"})
    if q["total"] < floor:
        out["stopped_at"] = "quality score"
        weakest = min(q["detail"], key=q["detail"].get)
        out["why"] = (f"Score below the floor. Weakest factor: {weakest} "
                      f"({q['detail'][weakest]:+.2f}) — {q['notes'].get(weakest, '')}")
        return out

    tp = analysis.calc_tp(daily, h4, price, cfg, trend=t)
    out["gates"].append({"gate": "target 9-20%", "pass": bool(tp), "detail":
        (f"${tp:.2f} (+{(tp / price - 1) * 100:.1f}%)" if tp else
         "no Fibonacci extension or resistance level inside the 9-20% window")})
    if not tp:
        out["stopped_at"] = "target 9-20%"
        out["why"] = ("No realistic target between +9% and +20%: resistance is "
                      "either too close to be worth the fees or beyond the cap.")
        return out

    value = max(cfg.ACCOUNT_SIZE * cfg.POSITION_PCT, cfg.MIN_TRADE_USD)
    shares = round(value / price, cfg.SHARE_DECIMALS)
    okf, frac = analysis.fee_check(price, tp, shares, is_etf, cfg)
    out["gates"].append({"gate": "fee check", "pass": okf,
                         "detail": f"fees {frac:.1%} of expected profit"})
    if not okf:
        out["stopped_at"] = "fee check"
        out["why"] = "Fees would eat too much of the expected profit."
        return out

    out["would_alert"] = {"entry": round(price, 2), "tp": round(tp, 2),
                          "tp_pct": round((tp / price - 1) * 100, 1),
                          "shares": shares, "value": round(shares * price, 2)}
    out["why"] = "All gates passed — this would alert (if not currently muted)."
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
