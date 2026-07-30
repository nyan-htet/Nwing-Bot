"""options_context.py — Options-derived context. Weighted input, never a trigger.

Runs in the NIGHTLY job (OI updates once daily; chains are heavy). Caches per
ticker into the watchlist file:
  call_wall      : strike above price with largest call OI (resistance magnet)
  put_wall       : strike below price with largest put OI (support magnet)
  pcr            : put/call volume ratio (sentiment; <0.7 bullish, >1.3 bearish)
  exp_move_pct   : IV-implied expected % move to the ~30d expiry
  liquid         : whether the chain is liquid enough to trust any of this
Also provides opex proximity (3rd Friday) for the alert caution note.

All functions degrade gracefully: no data -> {'liquid': False} and the
scorer treats the factor as neutral (0 vote).
"""
import datetime as dt


def third_friday(year, month):
    d = dt.date(year, month, 15)
    while d.weekday() != 4:
        d += dt.timedelta(days=1)
    return d


def near_opex(today=None, days=1):
    """True within `days` trading days of monthly opex (pinning/chop risk)."""
    today = today or dt.date.today()
    opex = third_friday(today.year, today.month)
    if today > opex:
        m = today.month % 12 + 1
        y = today.year + (1 if m == 1 else 0)
        opex = third_friday(y, m)
    delta = len([1 for i in range((opex - today).days)
                 if (today + dt.timedelta(days=i)).weekday() < 5])
    return delta <= days, opex


MIN_CHAIN_OI = 2000       # total OI below this -> chain too thin to trust
MIN_CHAIN_VOL = 200


def fetch_context(ticker: str, spot: float) -> dict:
    """Pull chain for the expiry nearest ~30 days out; compute walls/pcr/IV move."""
    out = {"liquid": False}
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        expiries = tk.options
        if not expiries:
            return out
        today = dt.date.today()
        target = min(expiries, key=lambda e: abs(
            (dt.date.fromisoformat(e) - today).days - 30))
        ch = tk.option_chain(target)
        calls, puts = ch.calls, ch.puts
        total_oi = float(calls.openInterest.sum() + puts.openInterest.sum())
        total_vol = float(calls.volume.fillna(0).sum() + puts.volume.fillna(0).sum())
        if total_oi < MIN_CHAIN_OI or total_vol < MIN_CHAIN_VOL:
            return out  # illiquid chain (common on R2000) -> ignore options

        above = calls[calls.strike > spot]
        below = puts[puts.strike < spot]
        call_wall = float(above.loc[above.openInterest.idxmax(), "strike"]) if len(above) else None
        put_wall = float(below.loc[below.openInterest.idxmax(), "strike"]) if len(below) else None
        pcr = float(puts.volume.fillna(0).sum() / max(calls.volume.fillna(0).sum(), 1))

        # IV expected move to expiry: ATM IV * sqrt(T)
        atm = calls.iloc[(calls.strike - spot).abs().argsort()[:1]]
        iv = float(atm.impliedVolatility.iloc[0]) if len(atm) else None
        days = max((dt.date.fromisoformat(target) - today).days, 1)
        exp_move = iv * (days / 365) ** 0.5 if iv else None

        out.update({"liquid": True, "expiry": target, "call_wall": call_wall,
                    "put_wall": put_wall, "pcr": round(pcr, 2),
                    "exp_move_pct": round(exp_move, 4) if exp_move else None})
    except Exception:
        pass
    return out


def score(octx: dict, entry: float, tp: float) -> tuple[float, str]:
    """Vote in [-1, +1] for the quality score + human-readable note.
    Neutral (0) when chain illiquid/missing."""
    if not octx or not octx.get("liquid"):
        return 0.0, "options: n/a"
    votes, notes = [], []

    pcr = octx.get("pcr")
    if pcr is not None:
        if pcr < 0.7:
            votes.append(0.6); notes.append(f"bullish flow (P/C {pcr})")
        elif pcr > 1.3:
            votes.append(-0.6); notes.append(f"bearish flow (P/C {pcr})")
        else:
            votes.append(0.0)

    cw = octx.get("call_wall")
    if cw:
        if tp <= cw * 1.005:
            votes.append(0.4); notes.append(f"TP inside call wall {cw}")
        else:
            votes.append(-0.5); notes.append(f"TP BEYOND call wall {cw} — likely stall")

    em = octx.get("exp_move_pct")
    if em:
        need = tp / entry - 1
        if need <= em * 1.2:
            votes.append(0.5); notes.append(f"TP within IV expected move ±{em:.0%}")
        else:
            votes.append(-0.5); notes.append(f"TP needs {need:.0%} vs IV ±{em:.0%} — ambitious")

    v = max(-1.0, min(1.0, sum(votes) / max(len(votes), 1) * 1.5))
    return round(v, 2), "; ".join(notes) if notes else "options: neutral"
