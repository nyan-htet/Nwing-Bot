"""analysis.py — Multi-timeframe engine.

Daily  : trend direction + strength (EMA20/50, ADX, RS vs SPY, 52w-high scan)
4-hour : setup (pullback to EMA20 or range breakout with volume)
1-hour : sniper trigger (bullish confirmation bar)
Levels : TP at resistance, must be >= MIN_TP_PCT above entry. No stoploss —
         instead a 'thesis broken' monitor for open positions.
Long-only by design.
"""
import indicators as ind


def daily_trend(daily, spy_daily, cfg) -> dict:
    d = daily.copy()
    d["ema_f"] = ind.ema(d["close"], cfg.EMA_FAST)
    d["ema_s"] = ind.ema(d["close"], cfg.EMA_SLOW)
    d["adx"] = ind.adx(d, cfg.ADX_PERIOD)
    last = d.iloc[-1]
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


def calc_tp(daily, h4, entry, cfg) -> float | None:
    """TP at nearest meaningful resistance ABOVE min threshold; if price is at
    52w highs (no resistance overhead), use a measured move. None = skip."""
    min_tp = entry * (1 + cfg.MIN_TP_PCT)
    max_tp = entry * (1 + cfg.MAX_TP_PCT)
    # candidate resistances from daily swings above min_tp
    cands = []
    highs = daily["high"].values
    lb = cfg.SWING_LOOKBACK
    for j in range(lb, len(highs) - lb):
        w = highs[j - lb: j + lb + 1]
        if highs[j] == w.max() and min_tp <= highs[j] <= max_tp:
            cands.append(float(highs[j]))
    if cands:
        return min(cands)          # nearest realistic target beyond 8%
    hi52 = daily["high"].iloc[-252:].max()
    if entry >= hi52 * 0.97:       # blue sky: measured move = 12% default
        return round(entry * 1.12, 2)
    return None                    # resistance closer than 8% -> skip trade


def thesis_broken(daily, cfg) -> bool:
    """For open positions: daily close below EMA50 AND lower-low structure."""
    d = daily.copy()
    d["ema_s"] = ind.ema(d["close"], cfg.THESIS_EMA)
    last = d.iloc[-1]
    lower_low = d["low"].iloc[-10:].min() < d["low"].iloc[-30:-10].min() if len(d) >= 30 else False
    return bool(last.close < last.ema_s and lower_low)


def quality_score(h1, h4, setup, cfg, octx=None, entry_hint=None) -> dict:
    """Weighted confirmation from RSI / Bollinger / VWAP / volume.
    Each factor votes in [-1, +1]; nothing here is a standalone trigger."""
    scores = {}
    r = float(ind.rsi(h4["close"], cfg.RSI_PERIOD).iloc[-1])
    # RSI: for pullbacks, recovering-from-dip is best; overbought is penalized
    if setup == "pullback":
        scores["rsi"] = 1.0 if 40 <= r <= 60 else (0.4 if 30 <= r < 40 or 60 < r <= 68 else -1.0 if r > 75 else -0.3)
    else:  # breakout: strength fine, exhaustion penalized
        scores["rsi"] = 1.0 if 55 <= r <= 70 else (0.3 if 50 <= r < 55 else -1.0 if r > 80 else -0.3)

    mid, up, lo, width, pos = ind.bollinger(h4["close"], cfg.BB_PERIOD)
    p = float(pos.iloc[-1]); w_now = float(width.iloc[-1])
    w_avg = float(width.iloc[-60:].mean()) if len(width.dropna()) >= 60 else w_now
    if setup == "pullback":   # best: bouncing from mid/lower half, not hugging upper band
        scores["bollinger"] = 1.0 if 0.3 <= p <= 0.7 else (0.3 if p < 0.3 else -0.6 if p > 0.95 else 0.0)
    else:                     # breakout: expansion from a squeeze is the prize
        squeeze_expand = w_avg > 0 and w_now > 1.1 * w_avg
        scores["bollinger"] = 1.0 if (p > 0.8 and squeeze_expand) else (0.2 if p > 0.8 else -0.4)

    vw = ind.vwap(h1)
    above = float(h1["close"].iloc[-1]) > float(vw.iloc[-1])
    recent = (h1["close"].iloc[-7:] > vw.iloc[-7:]).mean()  # fraction of day above VWAP
    scores["vwap"] = 1.0 if above and recent > 0.7 else (0.3 if above else -0.7)

    vol_avg = float(h4["volume"].rolling(20).mean().iloc[-1]) or 1e9
    ratio = float(h4["volume"].iloc[-1]) / vol_avg
    scores["volume"] = 1.0 if ratio >= 1.5 else (0.3 if ratio >= 1.0 else -0.4)

    options_note = None
    if "options" in cfg.QUALITY_WEIGHTS:
        import options_context as oc
        entry = entry_hint or float(h1["close"].iloc[-1])
        tp_guess = entry * (1 + cfg.MIN_TP_PCT)   # conservative: score vs min TP
        v, options_note = oc.score(octx, entry, tp_guess)
        scores["options"] = v

    total = sum(cfg.QUALITY_WEIGHTS[k] * v for k, v in scores.items())
    detail = {k: round(v, 2) for k, v in scores.items()}
    return {"total": round(total, 2), "detail": detail, "rsi_value": round(r, 1),
            "options_note": options_note}


def fee_check(entry, tp, shares, is_etf, cfg) -> tuple[bool, float]:
    fees = (cfg.FEE_PER_ETF_TRADE if is_etf else cfg.FEE_PER_STOCK_TRADE) * 2
    profit = (tp - entry) * shares
    if profit <= 0:
        return False, 1.0
    frac = fees / profit
    return frac <= cfg.MAX_FEE_PCT_OF_PROFIT, frac
