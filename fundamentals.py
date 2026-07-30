"""fundamentals.py — Company screen, earnings blocker, macro context.

All sources are free tiers. Every function degrades gracefully offline:
if data can't be fetched, the gate PASSES with a 'no-data' note rather than
silently blocking everything (you'll see data coverage in the dashboard).
"""
import datetime as dt


def company_screen(ticker: str, cfg) -> dict:
    """Quality gate via yfinance fundamentals."""
    out = {"pass": True, "notes": []}
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        de = info.get("debtToEquity")
        if de is not None and de / 100.0 > cfg.MAX_DEBT_TO_EQUITY:
            out["pass"] = False
            out["notes"].append(f"high debt/equity {de/100:.1f}")
        rg = info.get("revenueGrowth")
        if rg is not None and rg < cfg.MIN_REV_GROWTH:
            out["pass"] = False
            out["notes"].append(f"revenue shrinking {rg:.0%}")
        mc = info.get("marketCap")
        if mc is not None and mc < cfg.SMALLCAP_MIN_MARKETCAP:
            out["pass"] = False
            out["notes"].append("microcap, below quality floor")
        margins = info.get("profitMargins")
        if margins is not None and margins < -0.20:
            out["pass"] = False
            out["notes"].append(f"deeply unprofitable {margins:.0%}")
        out["pe"] = info.get("trailingPE")
        out["sector"] = info.get("sector", "Unknown")
        out["industry"] = info.get("industry", "Unknown")
    except Exception:
        out["notes"].append("fundamentals unavailable (offline?) - not blocked")
    return out


def earnings_blocked(ticker: str, cfg) -> bool:
    """True if earnings within EARNINGS_BLOCK_DAYS trading days from today."""
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return False
        nxt = min(d for d in dates if d >= dt.date.today())
        bdays = len([1 for i in range((nxt - dt.date.today()).days)
                     if (dt.date.today() + dt.timedelta(days=i)).weekday() < 5])
        return bdays <= cfg.EARNINGS_BLOCK_DAYS
    except Exception:
        return False  # no data -> don't block, but flagged in dashboard


def macro_context() -> dict:
    """Risk-on/off context from free sources. Simple + robust:
    VIX level via yfinance; extend with FRED (10y yield, DXY) later."""
    ctx = {"risk": "neutral", "vix": None}
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX").history(period="5d")["Close"].iloc[-1]
        ctx["vix"] = round(float(vix), 1)
        ctx["risk"] = "risk-off" if vix > 25 else ("risk-on" if vix < 16 else "neutral")
    except Exception:
        pass
    return ctx
