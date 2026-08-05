"""backtest_v2.py — VARIANT: time-boxed exits.

Same engine as backtest_hist.py, one difference in how trades end:

  * price falls to the hard STOP LOSS            -> exit at the stop
  * target hit inside the estimated time window  -> take profit, move on
  * window expires without the target being hit  -> EXIT AT MARKET, whatever
    the price is (small profit, break-even, or loss)

Both the stop-loss percentage and which end of the time estimate to use
(min or max) are set from the workflow inputs.

The window is the upper bound of the ATR + moving-average estimate shown in
live alerts ("Time frame: 24-35 trading days"), so the simulation matches the
information you actually receive. Set PARAMS["eta_buffer"] > 1 to allow extra
time (e.g. 1.5 = give it 50% longer than estimated).

Outputs docs/backtest_v2.json + backtest_v2_trades.csv, leaving the normal
backtest results untouched.

Original engine below.

HONEST SCOPE
------------
Runs on DAILY bars (8y history). It reproduces:
  trend gate (EMA20/50, ADX, RS vs SPY, 52w-high runner), pullback & breakout
  setups, quality score (RSI / Bollinger / volume), TP >= MIN_TP_PCT at
  structure, NO stoploss, thesis-broken exit, eToro fee model, position
  sizing and portfolio caps.
It CANNOT reproduce: the 1h momentum trigger and session VWAP factor (no
multi-year intraday data). Those make the live bot slightly more selective,
so treat these results as an approximation of the strategy family, not a
promise about the live system.

Usage:
  TWELVEDATA_KEY=... python backtest_hist.py [years] [max_tickers]
Outputs: docs/backtest.json  (+ console summary)
"""
import datetime as dt
import json
import sys

import numpy as np
import pandas as pd

import config as cfg
import data
import indicators as ind
import universe

RUN_INFO = {}
_CACHE = {}          # reused across sweep variants (download + prep once)

# ---- portfolio rules (overridable per run: see PARAMS) ----
START_EQUITY = 10_000.0
MIN_TRADE = cfg.MIN_TRADE_USD
FEE_STOCK = cfg.FEE_PER_STOCK_TRADE

PARAMS = {
    "position_pct": cfg.POSITION_PCT,   # share of equity per trade
    "max_concurrent": 10,
    "max_hold_days": 2000,   # only forced exit
    "use_thesis_exit": False, # False = hold to target or 2000-day timeout only
    "time_exit": True,        # v2: exit at market when the ETA window expires
    "eta_buffer": 1.0,        # multiply the estimated window (1.0 = as estimated)
    "eta_bound": "low",       # "low" = min of the estimate, "high" = max
    "stop_loss_pct": 0.15,    # hard stop; 0 disables it
    "exit_mode": "tp",                  # tp | trail | tp_then_trail
    "trail_ema": 20,                    # trail: exit on close below this EMA
    "trail_pct": 0.12,                  # trail: or this far below running high
    "min_score": cfg.SCORE_MIN,
    "min_score_stock": getattr(cfg, "SCORE_MIN_STOCK", cfg.SCORE_MIN),
    "min_score_etf": getattr(cfg, "SCORE_MIN_ETF", cfg.SCORE_MIN),
    "require_rs": False,                # only take RS > 0
    "require_ema200": False,            # only take price > 200 EMA
}


def prep(df):
    d = df.copy()
    d["ema_f"] = ind.ema(d["close"], cfg.EMA_FAST)
    d["ema_s"] = ind.ema(d["close"], cfg.EMA_SLOW)
    d["adx"] = ind.adx(d, cfg.ADX_PERIOD)
    d["atr"] = ind.atr(d, cfg.ATR_PERIOD)
    d["rsi"] = ind.rsi(d["close"], cfg.RSI_PERIOD)
    mid, up, lo, width, pos = ind.bollinger(d["close"], cfg.BB_PERIOD)
    d["bb_pos"], d["bb_w"] = pos, width
    d["bb_w_avg"] = width.rolling(60).mean()
    d["vol_avg"] = d["volume"].rolling(20).mean()
    d["hi52"] = d["high"].rolling(252, min_periods=60).max()
    d["ret63"] = d["close"].pct_change(cfg.RS_LOOKBACK)
    d["ema_200"] = ind.ema(d["close"], 200)
    d["ema_trail"] = ind.ema(d["close"], PARAMS["trail_ema"])
    return d


def setup_at(d, i):
    """pullback | breakout | None — same shape as live setup_4h."""
    if i < 25:
        return None
    row, prev = d.iloc[i], d.iloc[i - 1]
    touched = (d["low"].iloc[i - 3:i] <= d["ema_f"].iloc[i - 3:i]).any()
    if touched and row.close > row.ema_f and row.close > row.open:
        return "pullback"
    range_high = d["high"].iloc[i - 21:i].max()
    if row.close > range_high and row.volume > cfg.VOL_SPIKE * (row.vol_avg or 1e18):
        return "breakout"
    return None


def quality_at(d, i, setup):
    """Live weights minus VWAP/options (renormalized over available factors)."""
    row = d.iloc[i]
    r = float(row.rsi)
    if setup == "pullback":
        s_rsi = 1.0 if 40 <= r <= 60 else (0.4 if (30 <= r < 40 or 60 < r <= 68) else (-1.0 if r > 75 else -0.3))
    else:
        s_rsi = 1.0 if 55 <= r <= 70 else (0.3 if 50 <= r < 55 else (-1.0 if r > 80 else -0.3))
    p, w_now, w_avg = float(row.bb_pos), float(row.bb_w), float(row.bb_w_avg or row.bb_w)
    if setup == "pullback":
        s_bb = 1.0 if 0.3 <= p <= 0.7 else (0.3 if p < 0.3 else (-0.6 if p > 0.95 else 0.0))
    else:
        s_bb = 1.0 if (p > 0.8 and w_avg and w_now > 1.1 * w_avg) else (0.2 if p > 0.8 else -0.4)
    ratio = float(row.volume) / float(row.vol_avg or 1e18)
    s_vol = 1.0 if ratio >= cfg.VOL_SPIKE else (0.3 if ratio >= 1.0 else -0.4)
    wts = {"rsi": 0.26, "bollinger": 0.18, "volume": 0.15}
    tot = sum(wts.values())
    return (wts["rsi"] * s_rsi + wts["bollinger"] * s_bb + wts["volume"] * s_vol) / tot * 0.6


def tp_at(d, i, entry, strong=False):
    """Trend-aware target ladder (mirrors analysis.calc_tp)."""
    min_tp, max_tp = entry * (1 + cfg.MIN_TP_PCT), entry * (1 + cfg.MAX_TP_PCT)
    lb = cfg.SWING_LOOKBACK
    highs = d["high"].values[max(0, i - 252):i]
    cands = []
    for j in range(lb, len(highs) - lb):
        w = highs[j - lb:j + lb + 1]
        if highs[j] == w.max() and min_tp <= highs[j] <= max_tp:
            cands.append(float(highs[j]))
    if getattr(cfg, "USE_FIB_TARGETS", False):
        sub = d.iloc[max(0, i - 120):i + 1]
        fibs = [f for f in ind.fib_extension_targets(sub, entry)
                if min_tp <= f <= max_tp]
        if fibs:
            return round(fibs[-1] if strong else fibs[0], 2)
    if cands:
        cands.sort()
        pctl = cfg.TP_STRETCH["strong" if strong else "normal"]
        return round(cands[min(len(cands) - 1, int(round(pctl * (len(cands) - 1))))], 2)
    hi52 = float(d["hi52"].iloc[i] or 0)
    atr = float(d["atr"].iloc[i] or 0)
    if hi52 and (entry >= hi52 * 0.97 or min_tp > hi52):
        mult = cfg.TP_BLUESKY_ATR * (1.5 if strong else 1.0)
        t = entry + mult * atr
        return round(min(t, max_tp), 2) if t >= min_tp else None
    return None


def thesis_broken_at(d, i):
    if i < 30:
        return False
    row = d.iloc[i]
    lower_low = d["low"].iloc[i - 10:i].min() < d["low"].iloc[i - 30:i - 10].min()
    return bool(row.close < row.ema_s and lower_low)


def stratified_sample(all_tickers, meta, n_stocks=30, n_etfs=10, n_baseline=10,
                      seed=42):
    """Balanced sample: n_stocks spread evenly across sectors, n_etfs spread
    across ETF exposures, plus a baseline block (SPY, GLD + random others).
    Deterministic for a given seed so runs are comparable."""
    import random
    from collections import defaultdict
    rng = random.Random(seed)

    stocks_by_sector = defaultdict(list)
    etfs_by_sector = defaultdict(list)
    for t in all_tickers:
        if t == "SPY":
            continue
        m = meta.get(t, {})
        (etfs_by_sector if m.get("type") == "etf" else stocks_by_sector)[
            m.get("sector", "Other")].append(t)

    def spread(groups, n):
        keys = sorted(groups)
        for k in keys:
            rng.shuffle(groups[k])
        picked, i = [], 0
        while len(picked) < n and any(groups[k] for k in keys):
            k = keys[i % len(keys)]
            if groups[k]:
                picked.append(groups[k].pop())
            i += 1
        return picked

    stocks = spread(stocks_by_sector, n_stocks)
    etfs = spread(etfs_by_sector, n_etfs)

    chosen = set(stocks + etfs)
    baseline = [t for t in ("GLD", "QQQ", "VOO") if t in all_tickers and t not in chosen]
    pool = [t for t in all_tickers if t not in chosen and t not in baseline and t != "SPY"]
    rng.shuffle(pool)
    baseline += pool[:max(0, n_baseline - len(baseline))]

    print(f"Stratified sample: {len(stocks)} stocks across "
          f"{len({meta.get(t, {}).get('sector') for t in stocks})} sectors, "
          f"{len(etfs)} ETFs across "
          f"{len({meta.get(t, {}).get('sector') for t in etfs})} exposures, "
          f"{len(baseline)} baseline/random")
    return stocks, etfs, baseline


def run(years=8, max_stocks=None, include_etfs=True, stratified=False,
        n_stocks=30, n_etfs=10, n_baseline=10):
    """max_stocks: how many STOCKS to test (0 = none, blank/None = all).
    include_etfs: test the ETF rows from tickers.csv too.
    SPY is always downloaded as the benchmark (never traded)."""
    all_tickers, meta = universe.load()
    if stratified:
        s_, e_, b_ = stratified_sample(all_tickers, meta, n_stocks, n_etfs, n_baseline)
        stocks, etfs = s_ + [t for t in b_ if meta.get(t, {}).get("type") != "etf"], \
                       e_ + [t for t in b_ if meta.get(t, {}).get("type") == "etf"]
        tickers = ["SPY"] + sorted(set(stocks + etfs))
        print(f"Universe: {len(stocks)} stocks + {len(etfs)} ETFs (+SPY benchmark) | stratified")
        return _run_core(tickers, meta, stocks, etfs, years, True)
    stocks = [t for t in all_tickers
              if meta.get(t, {}).get("type") != "etf" and t != "SPY"]
    etfs = [t for t in all_tickers
            if meta.get(t, {}).get("type") == "etf" and t != "SPY"]

    if max_stocks is not None:
        if max_stocks <= 0:
            stocks = []
        elif max_stocks < len(stocks):
            step = max(1, len(stocks) // max_stocks)
            stocks = stocks[::step][:max_stocks]
    if not include_etfs:
        etfs = []

    tickers = ["SPY"] + sorted(set(stocks + etfs))   # benchmark mandatory
    print(f"Universe: {len(stocks)} stocks + {len(etfs)} ETFs "
          f"(+SPY benchmark) | ETFs {'included' if include_etfs else 'excluded'}")
    return _run_core(tickers, meta, stocks, etfs, years, include_etfs)


def _run_core(tickers, meta, stocks, etfs, years, include_etfs):
    n_bars = int(years * 252) + 300

    cache_key = (tuple(tickers), n_bars)
    if _CACHE.get("key") == cache_key:
        raw = _CACHE["raw"]
        print(f"Reusing cached history for {len(raw)} tickers (no re-download)")
    else:
        print(f"Downloading daily history for {len(tickers)} tickers…")
        raw = data.fetch_td(tickers, interval="1day", outputsize=min(n_bars, 5000))
        _CACHE.update({"key": cache_key, "raw": raw})
    spy = raw.get("SPY")
    if spy is None:                           # retry benchmark alone
        print("SPY missing from batch — retrying on its own…")
        spy = data.fetch_td(["SPY"], interval="1day",
                            outputsize=min(n_bars, 5000)).get("SPY")
    if spy is None:
        raise SystemExit("No SPY data from Twelve Data — check TWELVEDATA_KEY "
                         "and that the plan allows daily history.")
    print(f"Downloaded {len(raw)} of {len(tickers)} tickers "
          f"(failed: {', '.join(t for t in tickers if t not in raw) or 'none'})")
    spy = spy.drop_duplicates(subset="time").sort_values("time").set_index("time")

    prep_key = (cache_key, PARAMS["trail_ema"])
    if _CACHE.get("prep_key") == prep_key:
        prepped = _CACHE["prepped"]
    else:
        prepped = {}
        for t, df in raw.items():
            if t == "SPY" or len(df) < 300:
                continue      # SPY = benchmark only, never traded
            d_ = df.drop_duplicates(subset="time").sort_values("time")
            prepped[t] = prep(d_).set_index("time")
        _CACHE.update({"prep_key": prep_key, "prepped": prepped})
    print(f"Usable tickers: {len(prepped)}")
    globals()["RUN_INFO"] = {"stocks": len(stocks), "etfs": len(etfs),
                             "tested": len(prepped), "years": years,
                             "include_etfs": include_etfs}

    # unified calendar
    all_days = sorted(set().union(*[set(d.index) for d in prepped.values()]))
    start = max(all_days[0], all_days[-1] - pd.Timedelta(days=int(years * 365.25)))
    days = [d for d in all_days if d >= start]

    equity = START_EQUITY
    open_pos, closed, curve = {}, [], []
    spy_ret = spy["close"].pct_change(cfg.RS_LOOKBACK)

    for day in days:
        # 1) manage open positions
        for t in list(open_pos):
            d = prepped[t]
            if day not in d.index:
                continue
            i = d.index.get_loc(day)
            if not isinstance(i, (int, np.integer)):
                i = int(np.atleast_1d(np.arange(len(d))[i])[-1])
            row, p = d.iloc[i], open_pos[t]
            p["bars"] = p.get("bars", 0) + 1
            p["peak"] = max(p.get("peak", p["entry"]), float(row.high))
            mode = PARAMS["exit_mode"]
            exit_px = reason = None

            # hard stop first — if both stop and target are touched in one bar,
            # assume the stop filled (conservative)
            if p.get("stop") and row.low <= p["stop"]:
                exit_px, reason = float(p["stop"]), "stop"

            hit_tp = exit_px is None and row.high >= p["tp"]

            if exit_px is None and mode == "tp" and hit_tp:
                exit_px, reason = p["tp"], "tp"
            elif exit_px is None and mode in ("trail", "tp_then_trail"):
                if mode == "tp_then_trail" and hit_tp and not p.get("trailing"):
                    p["trailing"] = True          # target reached -> now let it run
                    reason = None
                trail_on = (mode == "trail") or p.get("trailing")
                if trail_on:
                    below_ema = float(row.close) < float(row.ema_trail)
                    gave_back = float(row.close) <= p["peak"] * (1 - PARAMS["trail_pct"])
                    if below_ema or gave_back:
                        exit_px, reason = float(row.close), "trail"
            if (exit_px is None and PARAMS.get("use_thesis_exit")
                    and thesis_broken_at(d, i)):
                exit_px, reason = float(row.close), "thesis"
            # v2: ETA window expired -> exit at market, whatever the price
            eta_key = "eta_lo" if PARAMS.get("eta_bound", "low") == "low" else "eta_hi"
            eta_n = p.get(eta_key) or p.get("eta_hi")
            if (exit_px is None and PARAMS.get("time_exit") and eta_n
                    and p["bars"] >= eta_n * PARAMS.get("eta_buffer", 1.0)):
                exit_px, reason = float(row.close), "time-exit"
            if exit_px is None and (day - p["opened"]).days > PARAMS["max_hold_days"]:
                exit_px, reason = float(row.close), "timeout"
            if exit_px is None:
                continue
            fees = 2 * (FEE_STOCK if meta.get(t, {}).get("type") != "etf" else 0.0)
            pnl = (exit_px - p["entry"]) * p["shares"] - fees
            equity += pnl
            closed.append({"ticker": t, "entry": p["entry"], "exit": exit_px,
                           "shares": p["shares"], "opened": p["opened"],
                           "closed": day, "reason": reason, "pnl": pnl,
                           "ret": exit_px / p["entry"] - 1})
            del open_pos[t]

        # 2) look for entries
        if len(open_pos) < PARAMS["max_concurrent"]:
            cands = []
            for t, d in prepped.items():
                if t in open_pos or day not in d.index:
                    continue
                i = d.index.get_loc(day)
                if not isinstance(i, (int, np.integer)):
                    i = int(np.atleast_1d(np.arange(len(d))[i])[-1])
                if i < 260:
                    continue
                row = d.iloc[i]
                if not (row.ema_f > row.ema_s and row.close > row.ema_s):
                    continue
                if not (row.adx >= cfg.ADX_MIN):
                    continue
                b = spy_ret.get(day, np.nan)
                rs = (row.ret63 - b) if not np.isnan(b) else 0.0
                st = setup_at(d, i)
                if st is None:
                    continue
                q = quality_at(d, i, st)
                is_etf_t = meta.get(t, {}).get("type") == "etf"
                floor = PARAMS.get("min_score_etf" if is_etf_t else "min_score_stock",
                                   PARAMS["min_score"])
                if q < floor:
                    continue
                if PARAMS["require_rs"] and rs <= 0:
                    continue
                if PARAMS["require_ema200"] and not (row.close > row.ema_200):
                    continue
                entry = float(row.close)
                strong = bool(row.adx >= 30 and rs > 0 and row.close >= (row.hi52 or 1e18) * 0.85)
                tp = tp_at(d, i, entry, strong)
                eta_lo = eta_hi = None
                if tp:
                    eta_lo, eta_hi = ind.time_to_target(
                        d.iloc[max(0, i - 300):i + 1].reset_index(), entry, tp)
                if tp is None:
                    continue
                score = q + (0.2 if rs > 0 else -0.1) + (0.1 if row.close >= (row.hi52 or 1e18) * 0.85 else 0)
                cands.append((score, t, entry, tp, st))
            cands.sort(reverse=True)
            for score, t, entry, tp, st in cands:
                if len(open_pos) >= PARAMS["max_concurrent"]:
                    break
                budget = max(equity * PARAMS["position_pct"], MIN_TRADE)
                if budget > equity * 0.9:
                    break
                if getattr(cfg, "FRACTIONAL_SHARES", False):
                    shares = round(budget / entry, getattr(cfg, "SHARE_DECIMALS", 2))
                    if shares <= 0:
                        continue
                    if shares * entry < MIN_TRADE * 0.95:
                        shares = round(MIN_TRADE / entry, getattr(cfg, "SHARE_DECIMALS", 2))
                else:
                    shares = int(budget // entry)
                    if shares < 1 or shares * entry < MIN_TRADE:
                        continue
                is_etf = meta.get(t, {}).get("type") == "etf"
                fees = 2 * (FEE_STOCK if not is_etf else 0.0)
                if (tp - entry) * shares <= fees * 3:
                    continue
                open_pos[t] = {"entry": entry, "tp": tp, "shares": shares,
                               "opened": day, "setup": st, "bars": 0,
                               "eta_lo": eta_lo, "eta_hi": eta_hi,
                               "stop": entry * (1 - PARAMS["stop_loss_pct"])
                               if PARAMS.get("stop_loss_pct") else None}
        # 3) mark to market + utilisation
        mtm = equity
        deployed = 0.0
        for t, p in open_pos.items():
            d = prepped[t]
            px = float(d.loc[day, "close"]) if day in d.index else p["entry"]
            mtm += (px - p["entry"]) * p["shares"]
            deployed += px * p["shares"]
        curve.append((day, mtm, deployed, len(open_pos)))

    # ---- close any still-open positions (v2: at their best close) ----
    last_day = days[-1]
    for t, p in list(open_pos.items()):
        d = prepped[t]
        px = float(d.loc[last_day, "close"]) if last_day in d.index else p["entry"]
        fees = 2 * (FEE_STOCK if meta.get(t, {}).get("type") != "etf" else 0.0)
        pnl = (px - p["entry"]) * p["shares"] - fees
        equity += pnl
        closed.append({"ticker": t, "entry": p["entry"], "exit": px,
                       "shares": p["shares"], "opened": p["opened"],
                       "closed": last_day,
                       "reason": "end-of-test",
                       "pnl": pnl, "ret": px / p["entry"] - 1})
        del open_pos[t]

    bh = None
    try:
        w = spy.loc[(spy.index >= days[0]) & (spy.index <= days[-1]), "close"]
        bh = round((float(w.iloc[-1]) / float(w.iloc[0]) - 1) * 100, 2)
    except Exception:
        pass
    return summarize(curve, closed, days, bh)


def summarize(curve, closed, days, buy_hold_pct=None):
    ec = pd.DataFrame(curve, columns=["time", "equity", "deployed", "n_open"]).set_index("time")
    tr = pd.DataFrame(closed)
    final = float(ec["equity"].iloc[-1])
    dd = float((ec["equity"] / ec["equity"].cummax() - 1).min() * 100)

    yearly = {}
    for y, grp in ec.groupby(ec.index.year):
        first, last = float(grp["equity"].iloc[0]), float(grp["equity"].iloc[-1])
        yearly[int(y)] = round((last / first - 1) * 100, 2)

    monthly = {}
    m = ec["equity"].resample("ME").last()
    m_ret = m.pct_change().dropna() * 100
    for ts, v in m_ret.items():
        monthly.setdefault(int(ts.year), {})[int(ts.month)] = round(float(v), 2)

    wins = tr[tr.pnl > 0] if len(tr) else tr
    losses = tr[tr.pnl <= 0] if len(tr) else tr
    rets = ec["equity"].pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    n_years = max((ec.index[-1] - ec.index[0]).days / 365.25, 0.5)

    stats = {
        "start_equity": START_EQUITY,
        "final_equity": round(final, 2),
        "total_return_pct": round((final / START_EQUITY - 1) * 100, 2),
        "cagr_pct": round(((final / START_EQUITY) ** (1 / n_years) - 1) * 100, 2),
        "max_drawdown_pct": round(dd, 2),
        "sharpe": round(sharpe, 2),
        "n_trades": int(len(tr)),
        "win_rate_pct": round(len(wins) / len(tr) * 100, 1) if len(tr) else 0,
        "avg_win_pct": round(float(wins.ret.mean() * 100), 2) if len(wins) else 0,
        "avg_loss_pct": round(float(losses.ret.mean() * 100), 2) if len(losses) else 0,
        "avg_hold_days": round(float((tr.closed - tr.opened).dt.days.mean()), 1) if len(tr) else 0,
        "exits": tr.reason.value_counts().to_dict() if len(tr) else {},
        "period": f"{ec.index[0].date()} to {ec.index[-1].date()}",
        "buy_hold_spy_pct": buy_hold_pct,
        "vs_buy_hold_pct": (round((final / START_EQUITY - 1) * 100 - buy_hold_pct, 2)
                            if buy_hold_pct is not None else None),
        "params": dict(PARAMS),
    }
    # ---- return distribution (buckets of trade outcomes) ----
    buckets = [(-1.0, -0.20, "loss > 20%"), (-0.20, -0.15, "loss 15-20%"),
               (-0.15, -0.10, "loss 10-15%"), (-0.10, -0.05, "loss 5-10%"),
               (-0.05, 0.0, "loss 0-5%"), (0.0, 0.05, "win 0-5%"),
               (0.05, 0.10, "win 5-10%"), (0.10, 0.15, "win 10-15%"),
               (0.15, 0.20, "win 15-20%"), (0.20, 10.0, "win > 20%")]
    dist = []
    for lo_, hi_, label in buckets:
        if len(tr):
            sel = tr[(tr.ret > lo_) & (tr.ret <= hi_)]
            dist.append({"bucket": label, "count": int(len(sel)),
                         "pct_of_trades": round(len(sel) / len(tr) * 100, 1),
                         "total_pnl": round(float(sel.pnl.sum()), 2)})
        else:
            dist.append({"bucket": label, "count": 0, "pct_of_trades": 0, "total_pnl": 0})

    # ---- capital utilisation: daily -> YEARLY averages ----
    util_yearly = []
    util_monthly = []          # kept for the JSON, page shows yearly
    if "deployed" in ec.columns:
        y = ec.resample("YE").agg({"deployed": "mean", "n_open": "mean",
                                   "equity": "mean"})
        peak = ec.resample("YE").agg({"n_open": "max", "deployed": "max"})
        for ts, row in y.iterrows():
            util_yearly.append({
                "year": int(ts.year),
                "avg_deployed": round(float(row["deployed"]), 0),
                "avg_positions": round(float(row["n_open"]), 1),
                "max_positions": int(peak.loc[ts, "n_open"]),
                "peak_deployed": round(float(peak.loc[ts, "deployed"]), 0),
                "avg_equity": round(float(row["equity"]), 0),
                "avg_utilisation_pct": round(float(row["deployed"] / row["equity"] * 100), 1)
                if row["equity"] else 0})
        stats["avg_deployed_usd"] = round(float(ec["deployed"].mean()), 0)
        stats["avg_utilisation_pct"] = round(float((ec["deployed"] / ec["equity"]).mean() * 100), 1)
        stats["avg_open_positions"] = round(float(ec["n_open"].mean()), 1)
        stats["max_open_positions"] = int(ec["n_open"].max())
        stats["days_fully_idle_pct"] = round(float((ec["n_open"] == 0).mean() * 100), 1)

    payload = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(),
               "universe": RUN_INFO,
               "distribution": dist,
               "utilisation_yearly": util_yearly,
               "utilisation_monthly": util_monthly,
               "note": "Daily-bar approximation of the live strategy (no 1h trigger / VWAP).",
               "stats": stats, "yearly_pct": yearly, "monthly_pct": monthly,
               "equity_curve": [[str(t.date()), round(float(v), 2)] for t, v in
                                ec["equity"].resample("W").last().dropna().items()],
               "deployed_curve": [[str(t.date()), round(float(v), 2)] for t, v in
                                  ec["deployed"].resample("W").mean().dropna().items()],
               "top_trades": (tr.nlargest(10, "pnl")[["ticker", "opened", "closed", "ret", "pnl"]]
                              .assign(opened=lambda x: x.opened.astype(str),
                                      closed=lambda x: x.closed.astype(str))
                              .to_dict("records") if len(tr) else []),
               "worst_trades": (tr.nsmallest(10, "pnl")[["ticker", "opened", "closed", "ret", "pnl"]]
                                .assign(opened=lambda x: x.opened.astype(str),
                                        closed=lambda x: x.closed.astype(str))
                                .to_dict("records") if len(tr) else [])}
    import os
    os.makedirs("docs", exist_ok=True)
    with open("docs/backtest_v2.json", "w") as f:
        json.dump(payload, f, indent=2, default=str)
    if len(tr):
        tr.to_csv("backtest_v2_trades.csv", index=False)

    print("\n================ BACKTEST SUMMARY ================")
    for k, v in stats.items():
        print(f"{k:>20}: {v}")
    print("\nTrade outcome distribution:")
    for d_ in dist:
        if d_["count"]:
            print(f"  {d_['bucket']:<14} {d_['count']:>4} trades "
                  f"({d_['pct_of_trades']:>4.1f}%)  P/L ${d_['total_pnl']:>9,.0f}")
    if util_yearly:
        print(f"\nCapital utilisation: avg ${stats.get('avg_deployed_usd', 0):,.0f} deployed "
              f"({stats.get('avg_utilisation_pct', 0)}% of equity), "
              f"avg {stats.get('avg_open_positions', 0)} positions "
              f"(max {stats.get('max_open_positions', 0)}), "
              f"{stats.get('days_fully_idle_pct', 0)}% of days fully in cash")
        print("  year   avg deployed   avg pos   max pos   utilisation")
        for u in util_yearly:
            print(f"  {u['year']}   ${u['avg_deployed']:>10,.0f}   {u['avg_positions']:>7.1f}"
                  f"   {u['max_positions']:>7}   {u['avg_utilisation_pct']:>9.1f}%")

    print("\nYearly returns (%):")
    for y, v in sorted(yearly.items()):
        print(f"  {y}: {v:+.2f}%")
    print("\nSaved: docs/backtest_v2.json, backtest_v2_trades.csv")
    print("NOTE: v2 exits at market when the ATR/MA time estimate expires "
          "('time-exit'), so those trades can close at a profit, loss or flat.")
    return payload


if __name__ == "__main__":
    # args: [years] [max_stocks|"strat"] [include_etfs yes/no] [n_stocks] [n_etfs] [n_baseline]
    yrs = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else 8
    mx = None
    if (len(sys.argv) > 2 and str(sys.argv[2]).strip() != ""
            and not str(sys.argv[2]).strip().lower().startswith("strat")):
        mx = int(sys.argv[2])
    inc = True
    if len(sys.argv) > 3 and str(sys.argv[3]).strip() != "":
        inc = str(sys.argv[3]).strip().lower() in ("yes", "y", "true", "1")
    # args 7,8: stop-loss % (e.g. 15 or 0 to disable) and eta bound (low|high)
    if len(sys.argv) > 7 and str(sys.argv[7]).strip():
        PARAMS["stop_loss_pct"] = max(0.0, float(sys.argv[7]) / 100.0)
    if len(sys.argv) > 8 and str(sys.argv[8]).strip():
        PARAMS["eta_bound"] = "high" if str(sys.argv[8]).strip().lower().startswith("h") else "low"
    print(f"v2 settings: stop-loss {PARAMS['stop_loss_pct']:.0%} | "
          f"time exit at ETA {PARAMS['eta_bound']} bound")

    if len(sys.argv) > 2 and str(sys.argv[2]).strip().lower().startswith("strat"):
        ns = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].strip() else 30
        ne = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5].strip() else 10
        nb = int(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6].strip() else 10
        run(yrs, stratified=True, n_stocks=ns, n_etfs=ne, n_baseline=nb)
    else:
        run(yrs, mx, inc)
