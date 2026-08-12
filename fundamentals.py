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


def market_cap_tier(market_cap, is_etf=False):
    """Four stock tiers; ETFs bypass stock market-cap filtering."""
    if is_etf:
        return "ETF"
    if market_cap is None:
        return "UNKNOWN"
    mc = float(market_cap)
    if mc < 100e6:
        return "D"
    if mc >= 10e9:
        return "A"
    if mc >= 1e9:
        return "B"
    return "C"


def company_screen(ticker, cfg, profile=None, ratios=None, is_etf=False):
    """FMP quality gate using the requested A/B/C/D market-cap tiers.

    A: >= $10B — normal processing.
    B: $1B-$10B — normal fundamentals, stronger technical floor.
    C: $100M-$1B — must be profitable, have controlled debt, sufficient
       dollar liquidity, and later clear the strongest technical floor.
    D: < $100M — skip from the main swing universe.
    ETFs bypass stock market-cap/fundamental filtering.
    """
    out = {"pass": True, "notes": [], "tier": "ETF" if is_etf else "UNKNOWN"}
    if is_etf:
        out["notes"].append("ETF — stock market-cap/fundamental filter bypassed")
        return out
    if not FMP_KEY:
        out["notes"].append("no FMP key — screen neutral")
        return out
    try:
        ratios_data = ratios
        if ratios_data is None:
            ratios_data = _fmp(f"ratios-ttm?symbol={ticker}", f"ratios-ttm/{ticker}")
        r = ratios_data[0] if isinstance(ratios_data, list) and ratios_data else {}
        de = r.get("debtEquityRatioTTM") or r.get("debtToEquityTTM")
        margin = r.get("netProfitMarginTTM")

        if de is not None and float(de) > cfg.MAX_DEBT_TO_EQUITY:
            out["pass"] = False
            out["notes"].append(f"Debt/equity {float(de):.1f} > {cfg.MAX_DEBT_TO_EQUITY:.1f} limit")
        if margin is not None and float(margin) < -0.20:
            out["pass"] = False
            out["notes"].append(f"Net profit margin {float(margin):.0%} < -20% limit")

        p = profile
        if p is None:
            time.sleep(0.3)
            prof = _fmp(f"profile?symbol={ticker}", f"profile/{ticker}")
            p = prof[0] if isinstance(prof, list) and prof else {}
        if not p:
            out["pass"] = False
            out["notes"].append("FMP profile unavailable — stock skipped")
            return out

        mc = p.get("mktCap") or p.get("marketCap")
        market_cap = float(mc) if mc is not None else None
        tier = market_cap_tier(market_cap)
        out.update({"tier": tier, "market_cap": market_cap,
                    "name": p.get("companyName") or "",
                    "sector": p.get("sector", "Unknown"),
                    "industry": p.get("industry", "Unknown")})

        if market_cap is None:
            out["pass"] = False
            out["notes"].append("Market cap unavailable — stock skipped")
            return out

        if tier == "D":
            out["pass"] = False
            out["notes"].append(f"Microcap — market cap ${market_cap/1e6:.0f}M < $100M floor")
            return out

        if tier == "C":
            avg_volume = (p.get("volAvg") or p.get("avgVolume") or
                          p.get("averageVolume") or p.get("volumeAvg"))
            price = p.get("price") or p.get("previousClose")
            dollar_volume = None
            try:
                if avg_volume is not None and price is not None:
                    dollar_volume = float(avg_volume) * float(price)
            except (TypeError, ValueError):
                dollar_volume = None

            if margin is None or float(margin) < 0:
                out["pass"] = False
                out["notes"].append("Tier C requires positive net profit margin")
            c_de_limit = getattr(cfg, "TIER_C_MAX_DEBT_TO_EQUITY", 1.5)
            if de is not None and float(de) > c_de_limit:
                out["pass"] = False
                out["notes"].append(f"Tier C Debt/equity {float(de):.1f} > {c_de_limit:.1f} limit")
            if dollar_volume is None:
                out["pass"] = False
                out["notes"].append("Tier C liquidity unavailable")
            elif dollar_volume < getattr(cfg, "TIER_C_MIN_DOLLAR_VOLUME", 5e6):
                out["pass"] = False
                out["notes"].append(f"Tier C low liquidity — avg dollar volume ${dollar_volume/1e6:.1f}M < {getattr(cfg, 'TIER_C_MIN_DOLLAR_VOLUME', 5e6)/1e6:.0f}M")
        return out
    except Exception:
        out["notes"].append("FMP screen unavailable — neutral")
        return out


def macro_context(spy_daily=None):
    """Risk regime from SPY's own realized volatility (annualized 20d).
    No extra API calls; VIX isn't on TD's free tier. Rough mapping:
    realized vol > 22% ~ stressed, < 13% ~ calm."""
    ctx = {"risk": "neutral", "vol": None}
    try:
        if spy_daily is not None and len(spy_daily) > 25:
            r = spy_daily["close"].pct_change().dropna()
            vol = float(r.iloc[-20:].std() * (252 ** 0.5))
            ctx["vol"] = round(vol * 100, 1)
            ctx["risk"] = ("risk-off" if vol > 0.22
                           else "risk-on" if vol < 0.13 else "neutral")
    except Exception:
        pass
    return ctx
