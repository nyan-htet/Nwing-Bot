"""fundamentals.py — FMP-based fundamentals (free key, datacenter-friendly).

Design for the 250 req/day free tier:
- ONE earnings-calendar call (date range) covers ALL tickers -> nightly
- company screen: 2 calls per stock, run nightly, cached into watchlist.json
- 4-hourly scans consume the cache: zero FMP requests intraday

Everything degrades gracefully: no key / call fails -> neutral pass with a
note, never a crash, never a silent block of all trades.
"""
import datetime as dt
import json
import os
import time
import urllib.request

FMP_KEY = os.getenv("FMP_KEY", "")
_BASE_STABLE = "https://financialmodelingprep.com/stable"
_BASE_V3 = "https://financialmodelingprep.com/api/v3"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def _fmp(path_stable, path_v3):
    """Try the new 'stable' API first, fall back to legacy v3."""
    for base, path in ((_BASE_STABLE, path_stable), (_BASE_V3, path_v3)):
        if not path:
            continue
        try:
            sep = "&" if "?" in path else "?"
            out = _get(f"{base}/{path}{sep}apikey={FMP_KEY}")
            if isinstance(out, dict) and out.get("Error Message"):
                continue
            return out
        except Exception:
            continue
    return None


def earnings_soon_set(days_ahead=7):
    """One call: all symbols with earnings in the next `days_ahead` days."""
    if not FMP_KEY:
        return set(), "FMP_KEY not set — earnings blocker inactive"
    today = dt.date.today()
    to = today + dt.timedelta(days=days_ahead)
    q = f"earnings-calendar?from={today}&to={to}"
    data = _fmp(q, f"earning_calendar?from={today}&to={to}")
    if not isinstance(data, list):
        return set(), "earnings calendar unavailable on this FMP plan"
    syms = {str(row.get("symbol", "")).upper() for row in data}
    return syms, f"earnings calendar loaded ({len(syms)} symbols reporting)"


def company_screen(ticker, cfg):
    """Quality gate from FMP ratios + profile. 2 requests per ticker."""
    out = {"pass": True, "notes": []}
    if not FMP_KEY:
        out["notes"].append("no FMP key — screen neutral")
        return out
    try:
        ratios = _fmp(f"ratios-ttm?symbol={ticker}", f"ratios-ttm/{ticker}")
        r = ratios[0] if isinstance(ratios, list) and ratios else {}
        de = r.get("debtEquityRatioTTM") or r.get("debtToEquityTTM")
        if de is not None and float(de) > cfg.MAX_DEBT_TO_EQUITY:
            out["pass"] = False
            out["notes"].append(f"high debt/equity {float(de):.1f}")
        margin = r.get("netProfitMarginTTM")
        if margin is not None and float(margin) < -0.20:
            out["pass"] = False
            out["notes"].append(f"deeply unprofitable {float(margin):.0%}")
        time.sleep(0.3)
        prof = _fmp(f"profile?symbol={ticker}", f"profile/{ticker}")
        p = prof[0] if isinstance(prof, list) and prof else {}
        mc = p.get("mktCap") or p.get("marketCap")
        if mc is not None and float(mc) < cfg.SMALLCAP_MIN_MARKETCAP:
            out["pass"] = False
            out["notes"].append("microcap, below quality floor")
        out["sector"] = p.get("sector", "Unknown")
        out["industry"] = p.get("industry", "Unknown")
    except Exception:
        out["notes"].append("FMP screen unavailable — neutral")
    return out


def macro_context():
    """Risk regime without extra dependencies: VIX proxy via Twelve Data if
    available; neutral otherwise."""
    ctx = {"risk": "neutral", "vix": None}
    try:
        import data as _d
        vix = _d.fetch_td(["VIX"], interval="1day", outputsize=5).get("VIX")
        if vix is not None and len(vix):
            v = float(vix["close"].iloc[-1])
            ctx["vix"] = round(v, 1)
            ctx["risk"] = "risk-off" if v > 25 else ("risk-on" if v < 16 else "neutral")
    except Exception:
        pass
    return ctx
