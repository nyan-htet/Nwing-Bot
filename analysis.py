"""analysis.py — Multi-timeframe engine.

Daily  : trend direction + strength (EMA20/50, ADX, RS vs SPY, 52w-high scan)
4-hour : setup (pullback to EMA20 or range breakout with volume)
1-hour : sniper trigger (bullish confirmation bar)
Levels : TP at resistance, must be >= MIN_TP_PCT above entry. No stoploss —
         instead a 'thesis broken' monitor for open positions.
Long-only by design.
"""
import pandas as pd

import indicators as ind


def trend_duration(d) -> tuple[str, int]:
    """Current daily regime and how many WEEKS it has persisted.
    Uptrend = EMA20 > EMA50; downtrend = EMA20 < EMA50."""
    up = (d["ema_f"] > d["ema_s"]).values
    if len(up) == 0:
        return "unknown", 0
    cur = bool(up[-1])
    n = 0
    for v in up[::-1]:
        if bool(v) != cur:
            break
        n += 1
    return ("uptrend" if cur else "downtrend"), max(1, round(n / 5))


def daily_trend(daily, spy_daily, cfg) -> dict:
    d = daily.copy()
    d["ema_f"] = ind.ema(d["close"], cfg.EMA_FAST)
    d["ema_s"] = ind.ema(d["close"], cfg.EMA_SLOW)
    d["ema_200"] = ind.ema(d["close"], 200)
    d["adx"] = ind.adx(d, cfg.ADX_PERIOD)
    last = d.iloc[-1]
    regime, weeks = trend_duration(d)
    above_200 = bool(last.close > last.ema_200) if not pd.isna(last.ema_200) else None
    hi52 = d["high"].iloc[-252:].max() if len(d) >= 60 else d["high"].max()
    rs = ind.relative_strength(d["close"], spy_daily["close"], cfg.RS_LOOKBACK)
    uptrend = bool(last.ema_f > last.ema_s and last.close > last.ema_s)
    strong = bool(last.adx >= cfg.ADX_MIN)
    near_high = bool(last.close >= hi52 * (1 - cfg.HIGH_52W_PROXIMITY))
    higher_highs = bool(d["high"].iloc[-20:].max() > d["high"].iloc[-40:-20].max()) if len(d) >= 40 else False
    score = sum([uptrend, strong, rs > 0, near_high, higher_highs])
    return {"uptrend": uptrend, "strong": strong, "rs": round(rs, 3),
            "near_52w_high": near_high, "higher_highs": higher_highs,
            "adx": round(float(last.adx), 1), "score": score,
            "regime": regime, "regime_weeks": weeks,
            "ema20": round(float(last.ema_f), 2),
            "ema50": round(float(last.ema_s), 2),
            "ema200": (round(float(last.ema_200), 2) if not pd.isna(last.ema_200) else None),
            "above_ema200": above_200,
            "runner": uptrend and strong and rs > 0 and near_high}


def setup_4h(h4, cfg) -> str | None:
    d = h4.copy()
    d["ema_f"] = ind.ema(d["close"], cfg.EMA_FAST)
    d["vol_avg"] = d["volume"].rolling(20).mean()
    if len(d) < 25:
        return None
    last, prev = d.iloc[-1], d.iloc[-2]
    # Pullback: touched EMA20 recently, now closing back above it
    touched = (d["low"].iloc[-4:-1] <= d["ema_f"].iloc[-4:-1]).any()
    if touched and last.close > last.ema_f and last.close > last.open:
        return "pullback"
    # Breakout: closes above 20-bar range high on volume spike
    range_high = d["high"].iloc[-21:-1].max()
    if last.close > range_high and last.volume > cfg.VOL_SPIKE * (last.vol_avg or 1e9):
        return "breakout"
    return None


def trigger_1h(h1) -> bool:
    """Confirmation on 1h: last bar bullish and closes in upper half of range,
    above the prior bar's high (simple momentum ignition)."""
    if len(h1) < 3:
        return False
    last, prev = h1.iloc[-1], h1.iloc[-2]
    rng = last.high - last.low
    upper_half = rng > 0 and (last.close - last.low) / rng >= 0.5
    return bool(last.close > last.open and upper_half and last.close > prev.high)


def calc_tp(daily, h4, entry, cfg, trend=None) -> float | None:
    """Target inside [MIN_TP_PCT, MAX_TP_PCT], chosen in this order:

      1. Fibonacci extension of the last up-leg (1.272 / 1.618 / 2.0) that
         falls inside the window — a structural projection of how far this
         move is likely to travel.
      2. Swing-high resistance inside the window (nearest in weak trends,
         further up the ladder in strong ones).
      3. Blue-sky ATR measured move.

    Returns None (skip the trade) if nothing sensible sits inside the window,
    e.g. the only resistance is below +MIN_TP_PCT.
    """
    min_tp = entry * (1 + cfg.MIN_TP_PCT)
    max_tp = entry * (1 + cfg.MAX_TP_PCT)
    strong = bool(trend and trend.get("score", 0) >= 4 and trend.get("adx", 0) >= 30)

    # 1) Fibonacci extension targets inside the window
    if getattr(cfg, "USE_FIB_TARGETS", False):
        fibs = [f for f in ind.fib_extension_targets(daily, entry)
                if min_tp <= f <= max_tp]
        if fibs:
            return round(fibs[-1] if strong else fibs[0], 2)

    # 2) structural resistance inside the window
    cands = []
    highs = daily["high"].values
    lb = cfg.SWING_LOOKBACK
    for j in range(lb, len(highs) - lb):
        w = highs[j - lb: j + lb + 1]
        if highs[j] == w.max() and min_tp <= highs[j] <= max_tp:
            cands.append(float(highs[j]))
    if cands:
        cands.sort()
        pctl = cfg.TP_STRETCH["strong" if strong else "normal"]
        return round(cands[min(len(cands) - 1, int(round(pctl * (len(cands) - 1))))], 2)

    # 3) blue sky: ATR measured move, clamped to the window
    hi52 = float(daily["high"].iloc[-252:].max())
    atr = float(ind.atr(daily, cfg.ATR_PERIOD).iloc[-1])
    if entry >= hi52 * 0.97 or min_tp > hi52:
        mult = cfg.TP_BLUESKY_ATR * (1.5 if strong else 1.0)
        target = entry + mult * atr
        if target < min_tp:
            return None               # move too small to be worth it
        return round(min(target, max_tp), 2)
    return None


def thesis_broken(daily, cfg) -> bool:
    """For open positions: daily close below EMA50 AND lower-low structure."""
    d = daily.copy()
    d["ema_s"] = ind.ema(d["close"], cfg.THESIS_EMA)
    last = d.iloc[-1]
    lower_low = d["low"].iloc[-10:].min() < d["low"].iloc[-30:-10].min() if len(d) >= 30 else False
    return bool(last.close < last.ema_s and lower_low)


def quality_score(h1, h4, setup, cfg, octx=None, entry_hint=None) -> dict:
    """Weighted confirmation. Each factor votes in [-1, +1] and is adjusted by
    CONTEXT (how it got here), not just its level. Nothing is a hard trigger."""
    scores, notes = {}, {}

    # ---------- RSI: level + direction of approach + divergence ----------
    r, r_dir, r_lo, r_hi, r_reset = ind.rsi_context(h4["close"], cfg.RSI_PERIOD)
    if setup == "pullback":
        base = 1.0 if 40 <= r <= 60 else (0.4 if (30 <= r < 40 or 60 < r <= 68) else (-1.0 if r > 75 else -0.3))
    else:
        base = 1.0 if 55 <= r <= 70 else (0.3 if 50 <= r < 55 else (-1.0 if r > 80 else -0.3))
    adj = 0.0
    if r_dir == "rising":
        adj += 0.3                      # recovering into the zone = good
    elif r_dir == "falling":
        adj -= 0.4                      # decaying into the zone = caution
    if r_reset:
        adj += 0.3                      # dipped <40 then turned up = clean reset
    if r_hi and r_hi > 75 and r_dir == "falling":
        adj -= 0.3                      # coming down off overbought
    diverg = ind.rsi_divergence(h4, cfg.RSI_PERIOD)
    if diverg:
        adj -= 0.5
    scores["rsi"] = max(-1.0, min(1.0, base + adj))
    notes["rsi"] = (f"RSI {r:.1f} {r_dir}"
                    + (f", reset from {r_lo:.0f}" if r_reset else "")
                    + (f", peaked {r_hi:.0f}" if r_hi and r_hi > 75 else "")
                    + (", BEARISH DIVERGENCE" if diverg else ""))

    # ---------- Bollinger: position + squeeze/expansion + band walk ----------
    mid, up, lo_b, width, pos = ind.bollinger(h4["close"], cfg.BB_PERIOD)
    p = float(pos.iloc[-1]); w_now = float(width.iloc[-1])
    w_avg = float(width.iloc[-60:].mean()) if len(width.dropna()) >= 60 else w_now
    expanding = w_avg > 0 and w_now > 1.1 * w_avg
    contracting = w_avg > 0 and w_now < 0.9 * w_avg
    if setup == "pullback":
        b = 1.0 if 0.3 <= p <= 0.7 else (0.3 if p < 0.3 else (-0.6 if p > 0.95 else 0.0))
        if p > 0.9 and contracting:
            b -= 0.3                    # tagging upper band as bands shrink = exhaustion
    else:
        b = 1.0 if (p > 0.8 and expanding) else (0.2 if p > 0.8 else -0.4)
        if p > 0.9 and expanding:
            b = 1.0                     # healthy band walk
        if p > 0.9 and contracting:
            b = -0.5                    # snap-back risk
    scores["bollinger"] = max(-1.0, min(1.0, b))
    notes["bollinger"] = (f"band position {p:.0%}, "
                          + ("expanding" if expanding else "contracting" if contracting else "stable"))

    # ---------- VWAP: reclaim vs rejection + extension ----------
    vw = ind.vwap(h1)
    px = float(h1["close"].iloc[-1]); v = float(vw.iloc[-1])
    above = px > v
    recent_frac = float((h1["close"].iloc[-7:] > vw.iloc[-7:]).mean())
    dipped = bool((h1["low"].iloc[-7:] < vw.iloc[-7:]).any())
    atr_h1 = float(ind.atr(h1, cfg.ATR_PERIOD).iloc[-1]) or 1e-9
    ext = (px - v) / atr_h1
    if above and dipped and recent_frac >= 0.4:
        vs, vnote = 1.0, "reclaimed VWAP after dip (buyers defended)"
    elif above and recent_frac > 0.7:
        vs, vnote = 0.8, "holding above VWAP"
    elif above:
        vs, vnote = 0.3, "just above VWAP"
    else:
        vs, vnote = -0.7, "below VWAP (sellers in control)"
    if ext > 2.0:
        vs -= 0.5
        vnote += f", stretched {ext:.1f} ATR above"
    scores["vwap"] = max(-1.0, min(1.0, vs))
    notes["vwap"] = vnote

    # ---------- Volume: setup-aware (dry-up on pullbacks, surge on breakouts) ----------
    ratio, vtrend = ind.volume_context(h4)
    if setup == "pullback":
        if vtrend == "drying up":
            vol = 1.0                   # sellers exhausted = ideal pullback
        elif ratio >= cfg.VOL_SPIKE:
            vol = 0.2                   # heavy volume on a dip = distribution risk
        else:
            vol = 0.5
    else:
        vol = 1.0 if ratio >= cfg.VOL_SPIKE else (0.3 if ratio >= 1.0 else -0.6)
    scores["volume"] = vol
    notes["volume"] = f"{ratio:.1f}x average, {vtrend}"

    # ---------- NEW: extension from EMA20 (don't chase parabolic moves) ----------
    e20 = ind.ema(h4["close"], cfg.EMA_FAST)
    e50 = ind.ema(h4["close"], cfg.EMA_SLOW)
    e200 = ind.ema(h4["close"], 200)
    atr4 = ind.atr(h4, cfg.ATR_PERIOD)
    ex = ind.ema_extension(h4, e20, atr4)
    slope = ind.ema_slope(e20)
    stack = ind.ema_stack(e20, e50, e200)
    if ex <= 1.0:
        es = 1.0                        # near the line = clean entry
    elif ex <= 2.0:
        es = 0.4
    elif ex <= 3.0:
        es = -0.2
    else:
        es = -1.0                       # parabolic, correction likely
    if slope <= 0:
        es -= 0.4                       # flat/falling EMA = no real trend
    if stack.startswith("bullish"):
        es += 0.2
    scores["extension"] = max(-1.0, min(1.0, es))
    notes["extension"] = (f"{ex:+.1f} ATR from 20 EMA, slope {slope:+.1f}%, {stack}")

    # ---------- Options (unchanged) ----------
    options_note = None
    if "options" in cfg.QUALITY_WEIGHTS:   # only if re-enabled in config
        import options_context as oc
        entry = entry_hint or px
        v_, options_note = oc.score(octx, entry, entry * (1 + cfg.MIN_TP_PCT))
        scores["options"] = v_
        notes["options"] = options_note
    else:
        scores.pop("options", None)
        notes.pop("options", None)

    wts = cfg.QUALITY_WEIGHTS
    total = sum(wts.get(k, 0) * v for k, v in scores.items())
    detail = {k: round(v, 2) for k, v in scores.items()}
    return {"total": round(total, 2), "detail": detail, "notes": notes,
            "rsi_value": round(r, 1), "rsi_dir": r_dir, "divergence": diverg,
            "ema_extension_atr": round(ex, 2), "ema_slope_pct": round(slope, 2),
            "ema_stack": stack, "volume_trend": vtrend,
            "options_note": options_note}


def fee_check(entry, tp, shares, is_etf, cfg) -> tuple[bool, float]:
    fees = (cfg.FEE_PER_ETF_TRADE if is_etf else cfg.FEE_PER_STOCK_TRADE) * 2
    profit = (tp - entry) * shares
    if profit <= 0:
        return False, 1.0
    frac = fees / profit
    return frac <= cfg.MAX_FEE_PCT_OF_PROFIT, frac
