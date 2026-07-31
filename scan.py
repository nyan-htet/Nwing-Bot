"""scan.py — The pipeline. Two modes:

  python scan.py nightly   -> build watchlist (prefilter + fundamentals + overrides)
  python scan.py hourly    -> scan watchlist on 1h/4h/daily, alert + publish
  python scan.py test      -> full offline test on synthetic data

Pipeline per ticker (hourly):
  daily trend gate -> 4h setup -> 1h trigger -> TP >= 8% -> fee check
  -> min $250 sizing -> alert (email + telegram) + docs/signals.json (dashboard)
Then: check open positions for TP-reached / thesis-broken.
"""
import datetime as dt
import json
import os
import sys

import config as cfg
import data
import analysis
import fundamentals as fnd
import cycles
import notify
import portfolio_files as pf


def build_signal(ticker, h1, spy_daily, is_etf, screen, octx=None, tmeta=None):
    h4 = data.resample(h1, "4h")
    daily = data.resample(h1, "1D")
    if len(daily) < 60 or len(h4) < 30:
        return None
    trend = analysis.daily_trend(daily, spy_daily, cfg)
    if not (trend["uptrend"] and trend["strong"]):
        return None
    setup = analysis.setup_4h(h4, cfg)
    if setup is None:
        return None
    if not analysis.trigger_1h(h1):
        return None
    q = analysis.quality_score(h1, h4, setup, cfg, octx=octx,
                               entry_hint=float(h1["close"].iloc[-1]))
    if q["total"] < cfg.SCORE_MIN:
        return None
    entry = float(h1["close"].iloc[-1])
    tp = analysis.calc_tp(daily, h4, entry, cfg)
    if tp is None:
        return None
    value = max(cfg.ACCOUNT_SIZE * cfg.POSITION_PCT, cfg.MIN_TRADE_USD)
    shares = int(value // entry)
    if shares < 1 or shares * entry < cfg.MIN_TRADE_USD:
        return None
    ok, fee_frac = analysis.fee_check(entry, tp, shares, is_etf, cfg)
    if not ok:
        return None
    reasons = [f"daily uptrend (score {trend['score']}/5)", f"4h {setup}",
               "1h momentum trigger"]
    if trend["runner"]:
        reasons.append("RUNNER: near 52w high + outperforming SPY")
    reasons.append(f"quality {q['total']:+.2f} (rsi {q['detail']['rsi']:+.1f}, "
                   f"bb {q['detail']['bollinger']:+.1f}, vwap {q['detail']['vwap']:+.1f}, "
                   f"vol {q['detail']['volume']:+.1f}, opt {q['detail'].get('options',0):+.1f}; "
                   f"RSI={q['rsi_value']})")
    if q.get("options_note"):
        reasons.append(q["options_note"])
    import options_context as oc
    is_opex, opex_date = oc.near_opex(days=cfg.OPEX_CAUTION_DAYS)
    if is_opex:
        reasons.append(f"⚠ monthly opex {opex_date} — pinning/chop risk")
    tmeta = tmeta or {}
    lev = int(tmeta.get("leverage", 1) or 1)
    if tmeta.get("inverse"):
        reasons.append(f"🔻 THIS IS AN INVERSE ETF ({lev}x BEAR) — buying it is a "
                       "bearish bet; it profits when the underlying FALLS")
    elif lev >= 2:
        reasons.append(f"⚡ THIS IS A {lev}x LEVERAGED ETF — volatility decay "
                       "erodes value on multi-week holds; extra caution with "
                       "no-stoploss style")
    if tmeta.get("note"):
        reasons.append(f"({tmeta['note']})")
    if screen.get("notes"):
        reasons.extend(screen["notes"][:1])
    return {"ticker": ticker, "setup": setup, "entry": round(entry, 2),
            "tp": round(tp, 2), "tp_pct": tp / entry - 1, "shares": shares,
            "value": shares * entry, "fee_pct": fee_frac,
            "trend_score": trend["score"], "adx": trend["adx"],
            "rs": trend["rs"], "reasons": reasons,
            "time": dt.datetime.now(dt.timezone.utc).isoformat()}


def check_positions(get_daily, macro):
    msgs = []
    for p in pf.open_positions(cfg.POSITIONS_FILE):
        daily = get_daily(p["ticker"])
        if daily is None or len(daily) < 60:
            continue
        px = float(daily["close"].iloc[-1])
        if px >= float(p["tp"]):
            msgs.append(f"🎯 {p['ticker']} hit your TP ${p['tp']} (now ${px:.2f}). Consider closing.")
        elif analysis.thesis_broken(daily, cfg):
            msgs.append(f"⚠️ {p['ticker']} thesis broken: daily close below EMA{cfg.THESIS_EMA} "
                        f"with structure break (now ${px:.2f}, entry ${p['entry']}). Your call.")
    return msgs


def publish(signals, position_msgs, macro, cyc):
    os.makedirs("docs", exist_ok=True)
    payload = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(),
               "macro": macro, "cycles": cyc, "signals": signals,
               "position_alerts": position_msgs}
    with open(cfg.SIGNALS_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def run_hourly(offline=False):
    # --- load watchlist ---
    try:
        with open(cfg.WATCHLIST_FILE) as f:
            watch = json.load(f)
    except FileNotFoundError:
        import universe
        _tk, _m = universe.load()
        watch = {"tickers": _tk, "meta": _m}
    tickers = pf.apply_overrides(watch["tickers"], watch.get("meta", {}),
                                 cfg.OVERRIDES_FILE)

    # --- data ---
    if offline:
        h1_map = {t: data.make_sample_1h(seed=i, trend=[0.6, -0.2, 0.3, 0.9][i % 4])
                  for i, t in enumerate(tickers)}
        spy_daily = data.resample(data.make_sample_1h(seed=99, trend=0.2), "1D")
        long_hist = data.make_sample_daily_long()
        macro = {"risk": "neutral", "vix": 17.0}
    else:
        h1_map = data.fetch_intraday(tickers)
        spy = data.fetch_intraday("SPY").get("SPY")
        if spy is None or not h1_map:
            raise SystemExit("FATAL: no intraday data. On GitHub, set the "
                             "TWELVEDATA_KEY secret (free key from "
                             "twelvedata.com). Yahoo/keyless sources are "
                             "blocked from GitHub runners.")
        spy_daily = data.resample(spy, "1D")
        failed = [t for t in tickers if t not in h1_map]
        if failed:
            print(f"Intraday data MISSING for {len(failed)} ticker(s): "
                  f"{', '.join(sorted(failed))} — scanned the rest.")
        else:
            print(f"Intraday data OK for all {len(h1_map)} tickers ✓")
        long_hist = None
        try:
            import yfinance as yf
            h = yf.Ticker(cfg.CYCLES_SYMBOL).history(period="max")[["Close"]]
            long_hist = h.rename(columns={"Close": "close"}).reset_index() \
                         .rename(columns={"Date": "time"})
        except Exception:
            pass
        macro = fnd.macro_context()

    cyc = cycles.context(long_hist) if long_hist is not None else {"line": "cycles n/a"}

    if not offline:
        wanted = [t for t in tickers if t != "SPY"]
        failed = [t for t in wanted if t not in h1_map]
        print("---- hourly coverage report ----")
        print(f"watchlist        : {len(wanted)}")
        print(f"no 1h data       : {len(failed)}" + (f" -> {', '.join(failed)}" if failed else ""))
        print("--------------------------------")

    # --- scan ---
    decided = pf.recently_decided(cfg.POSITIONS_FILE, cfg.ALERT_COOLDOWN_DAYS)
    skip_report = {"cooldown": [], "earnings": [], "screen": []}
    signals = []
    for t, h1 in h1_map.items():
        if t == "SPY":
            continue
        if t.upper() in decided:
            skip_report["cooldown"].append(t)
            continue
        tmeta = watch.get("meta", {}).get(t, {})
        is_etf = tmeta.get("type", "stock") == "etf"
        screen = tmeta.get("screen", {"pass": True, "notes": []})
        if not screen.get("pass", True):
            skip_report["screen"].append(t)
            continue  # failed nightly FMP quality screen
        if tmeta.get("earnings_soon"):
            skip_report["earnings"].append(t)
            continue  # earnings within 7 days — blocked per your rules
        octx = tmeta.get("options")
        sig = build_signal(t, h1, spy_daily, is_etf, screen, octx=octx, tmeta=tmeta)
        if sig:
            signals.append(sig)

    # --- open positions ---
    def get_daily(t):
        h1 = h1_map.get(t)
        return data.resample(h1, "1D") if h1 is not None else None
    pos_msgs = check_positions(get_daily, macro)

    # --- deliver ---
    for s in signals:
        body = notify.format_alert(s, macro, cyc)
        notify.send_email(f"BUY {s['ticker']} — {s['setup']} (+{s['tp_pct']:.0%} target)", body, cfg)
        notify.send_telegram(body, cfg)
    for m in pos_msgs:
        notify.send_email("Position alert", m, cfg)
        notify.send_telegram(m, cfg)
    publish(signals, pos_msgs, macro, cyc)
    for k, v in skip_report.items():
        if v:
            print(f"blocked ({k}): {', '.join(sorted(v))}")
    print(f"Scan done. {len(signals)} signal(s), {len(pos_msgs)} position alert(s). "
          f"Dashboard data -> {cfg.SIGNALS_FILE}")
    return signals, pos_msgs


def run_nightly():
    """Build watchlist from tickers.csv: keep names with valid data and an
    intact daily uptrend ranking; cache options context (best effort)."""
    import universe
    tickers, tmeta = universe.load()
    d = data.fetch_daily(tickers)
    spy = d.get("SPY")
    if spy is None:
        raise SystemExit("FATAL: no SPY data from Twelve Data — check "
                         "TWELVEDATA_KEY secret and quota, then re-run.")
    scored, meta = [], {}
    no_data = [t for t in tickers if t not in d]
    short_history = []
    for t, df in d.items():
        if t == "SPY":
            continue
        if len(df) < 120:
            short_history.append(t)
            continue
        tr = analysis.daily_trend(df, spy, cfg)
        # keep everything with data; rank by trend so hourly scans strongest first
        scored.append((tr["score"] + tr["rs"], t))
    scored.sort(reverse=True)
    top = [t for _, t in scored[:cfg.WATCHLIST_SIZE]]
    cut = [t for _, t in scored[cfg.WATCHLIST_SIZE:]]
    print("---- nightly coverage report ----")
    print(f"csv tickers      : {len(tickers)}")
    print(f"benchmark (excl) : SPY")
    print(f"no data from TD  : {len(no_data)}" + (f" -> {', '.join(no_data)}" if no_data else ""))
    print(f"history < 120d   : {len(short_history)}" + (f" -> {', '.join(short_history)}" if short_history else ""))
    if cut:
        print(f"over watchlist cap: {len(cut)} -> {', '.join(cut)}")
    print(f"in watchlist     : {len(top)}")
    print("---------------------------------")
    import options_context as oc
    earn_set, earn_note = fnd.earnings_soon_set(days_ahead=7)
    print(earn_note)
    for t in top:
        spot = float(d[t]["close"].iloc[-1]) if t in d else 0.0
        m = dict(tmeta.get(t, {}))
        m["options"] = oc.fetch_context(t, spot) if spot else {"liquid": False}
        if m.get("type", "stock") == "stock":
            m["earnings_soon"] = t.upper() in earn_set
            scr = fnd.company_screen(t, cfg)
            m["screen"] = {"pass": scr["pass"], "notes": scr["notes"][:2],
                           "sector": scr.get("sector"), "industry": scr.get("industry")}
        meta[t] = m
    with open(cfg.WATCHLIST_FILE, "w") as f:
        json.dump({"tickers": sorted(set(top)), "meta": meta,
                   "built": dt.datetime.now(dt.timezone.utc).isoformat()}, f, indent=2)
    print(f"Watchlist built from tickers.csv: {len(set(top))} of {len(tickers)} tickers")
    # ---- accounting: name every ticker that is NOT in the watchlist and why ----
    in_watch = set(top)
    no_data = [t for t in tickers if t not in d and t != "SPY"]
    too_short = [t for t in d if t != "SPY" and len(d[t]) < 120 and t not in in_watch]
    capped = [t for t in d if t != "SPY" and t not in in_watch
              and t not in no_data and t not in too_short]
    print(f"Excluded — benchmark (by design): SPY")
    if no_data:
        print(f"Excluded — NO DATA from Twelve Data: {', '.join(sorted(no_data))}")
    if too_short:
        print(f"Excluded — insufficient history (<120 daily bars): {', '.join(sorted(too_short))}")
    if capped:
        print(f"Excluded — below watchlist size cap ({cfg.WATCHLIST_SIZE}): {', '.join(sorted(capped))}")
    if not (no_data or too_short or capped):
        print("All non-benchmark tickers made the watchlist ✓")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    if mode == "nightly":
        run_nightly()
    elif mode == "hourly":
        run_hourly(offline=False)
    else:
        run_hourly(offline=True)
