"""FMP bulk fundamentals / quality screen.
Nightly only. Uses FMP bulk endpoints instead of 2 requests per ticker.
Stocks are assigned to market-cap tiers; ETFs bypass stock fundamental filters.
"""
import datetime as dt
import json
import os
import urllib.request

FMP_KEY = os.getenv("FMP_KEY", "")
_BASE_STABLE = "https://financialmodelingprep.com/stable"
_BASE_V3 = "https://financialmodelingprep.com/api/v3"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=45).read())


def _fmp(path_stable, path_v3=None):
    if not FMP_KEY:
        return None
    for base, path in ((_BASE_STABLE, path_stable), (_BASE_V3, path_v3)):
        if not path:
            continue
        try:
            sep = "&" if "?" in path else "?"
            out = _get(f"{base}/{path}{sep}apikey={FMP_KEY}")
            if isinstance(out, dict) and out.get("Error Message"):
                continue
            return out
        except Exception as exc:
            print(f"FMP error {path[:60]}: {str(exc)[:120]}")
    return None


def earnings_soon_set(days_ahead=7):
    if not FMP_KEY:
        return set(), "FMP_KEY not set — earnings blocker inactive"
    today = dt.date.today(); to = today + dt.timedelta(days=days_ahead)
    data = _fmp(f"earnings-calendar?from={today}&to={to}",
                f"earning_calendar?from={today}&to={to}")
    if not isinstance(data, list):
        return set(), "earnings calendar unavailable on this FMP plan"
    syms = {str(row.get("symbol", "")).upper() for row in data}
    return syms, f"earnings calendar loaded ({len(syms)} symbols reporting)"


def bulk_context(tickers):
    """Load profiles + TTM ratios once each and index only requested tickers."""
    wanted = {str(x).upper() for x in tickers}
    if not FMP_KEY:
        return {}, {}, "FMP_KEY not set"
    profiles_raw = _fmp("profile-bulk?part=0")
    ratios_raw = _fmp("ratios-ttm-bulk")
    profiles = {}
    ratios = {}
    for row in profiles_raw if isinstance(profiles_raw, list) else []:
        s = str(row.get("symbol") or "").upper()
        if s in wanted:
            profiles[s] = row
    for row in ratios_raw if isinstance(ratios_raw, list) else []:
        s = str(row.get("symbol") or "").upper()
        if s in wanted:
            ratios[s] = row
    note = f"FMP bulk loaded: profiles {len(profiles)}/{len(wanted)}, ratios {len(ratios)}/{len(wanted)}"
    print(note)
    return profiles, ratios, note


def market_cap_tier(market_cap, is_etf=False):
    if is_etf:
        return "ETF"
    if market_cap is None:
        return "UNKNOWN"
    mc = float(market_cap)

    # Boundaries:
    # A: >= $10B
    # B: >= $1B and < $10B
    # C: >= $100M and < $1B
    # D: < $100M
    if mc >= 10e9:
        return "A"
    if mc >= 1e9:
        return "B"
    if mc >= 100e6:
        return "C"
    return "D"


def company_screen(ticker, cfg, profile=None, ratios=None, is_etf=False):
    """Return tier + quality gate using already-loaded bulk FMP rows."""
    out = {"pass": True, "notes": [], "tier": "ETF" if is_etf else "UNKNOWN"}
    if is_etf:
        out["notes"].append("ETF — market-cap/fundamental stock filter bypassed")
        return out
    p = profile or {}
    r = ratios or {}
    if not p:
        out["pass"] = False
        out["notes"].append("FMP bulk profile unavailable — stock skipped")
        return out
    mc = p.get("mktCap") or p.get("marketCap")
    try: mc = float(mc) if mc is not None else None
    except (TypeError, ValueError): mc = None
    tier = market_cap_tier(mc, False)
    out["tier"] = tier
    out["market_cap"] = mc
    out["name"] = p.get("companyName") or ""
    out["sector"] = p.get("sector", "Unknown")
    out["industry"] = p.get("industry", "Unknown")
    if tier == "UNKNOWN":
        out["pass"] = False
        out["notes"].append("market cap unavailable — stock skipped")
        return out
    if tier == "D":
        out["pass"] = False
        out["notes"].append(f"microcap: market cap ${mc/1e6:.0f}M < $100M")
        return out
    de = r.get("debtEquityRatioTTM") or r.get("debtToEquityTTM")
    margin = r.get("netProfitMarginTTM")
    de_f = float(de) if de is not None else None
    margin_f = float(margin) if margin is not None else None
    out["debt_to_equity"] = de_f
    out["net_margin"] = margin_f
    avg_vol = p.get("volAvg") or p.get("avgVolume") or p.get("averageVolume") or p.get("volumeAvg")
    price = p.get("price") or p.get("previousClose")
    try:
        dollar_vol = float(avg_vol) * float(price) if avg_vol is not None and price is not None else None
    except (TypeError, ValueError):
        dollar_vol = None
    out["avg_dollar_volume"] = dollar_vol

    # Tier C: strongest fundamental quality + liquidity + technical score.
    if tier == "C":
        if de_f is None or de_f > cfg.TIER_C_MAX_DEBT_TO_EQUITY:
            out["pass"] = False
            out["notes"].append("Tier C: debt/equity unavailable or above 1.50")
        if margin_f is None or margin_f < cfg.TIER_C_MIN_NET_MARGIN:
            out["pass"] = False
            out["notes"].append("Tier C: not profitable / net margin unavailable")
        if dollar_vol is None or dollar_vol < cfg.TIER_C_MIN_DOLLAR_VOLUME:
            out["pass"] = False
            out["notes"].append("Tier C: average dollar volume unavailable or below $5M")
        else:
            out["notes"].append(f"Tier C liquidity OK: avg dollar volume ${dollar_vol/1e6:.1f}M")
        out["notes"].append("Tier C: stronger technical score required")
    else:
        if de_f is not None and de_f > cfg.MAX_DEBT_TO_EQUITY:
            out["pass"] = False
            out["notes"].append(f"high debt/equity {de_f:.1f}")
        if margin_f is not None and margin_f < -0.20:
            out["pass"] = False
            out["notes"].append(f"deeply unprofitable {margin_f:.0%}")
        if tier == "A":
            out["notes"].append("Tier A: normal processing")
        else:
            out["notes"].append("Tier B: stronger technical score required")
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
