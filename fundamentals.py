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
            out["notes"].append(f"Debt/equity {float(de):.1f} > {cfg.MAX_DEBT_TO_EQUITY:.1f} limit")
        margin = r.get("netProfitMarginTTM")
        if margin is not None and float(margin) < -0.20:
            out["pass"] = False
            out["notes"].append(f"Net profit margin {float(margin):.0%} < -20% limit")
        time.sleep(0.3)
        prof = _fmp(f"profile?symbol={ticker}", f"profile/{ticker}")
        p = prof[0] if isinstance(prof, list) and prof else {}
        mc = p.get("mktCap") or p.get("marketCap")
        market_cap = float(mc) if mc is not None else None

        # Market cap is a risk tier, not a blanket quality verdict.
        # < $100M: microcap -> keep out of the main swing system.
        # $100M-$300M: small-cap -> allow if liquidity and the existing
        # debt/profitability checks are healthy.
        # >= $300M: normal universe.
        microcap_floor = float(getattr(cfg, "MICROCAP_MIN_MARKETCAP", 100e6))
        smallcap_floor = float(getattr(cfg, "SMALLCAP_MIN_MARKETCAP", 300e6))
        smallcap_dollar_volume = float(getattr(cfg, "SMALLCAP_MIN_DOLLAR_VOLUME", 2e6))

        if market_cap is not None and market_cap < microcap_floor:
            out["pass"] = False
            out["notes"].append(
                f"Microcap — market cap ${market_cap/1e6:.0f}M < ${microcap_floor/1e6:.0f}M floor"
            )
        elif market_cap is not None and market_cap < smallcap_floor:
            # Do NOT reject solely because the company is small.
            # Prefer practical tradability: average daily dollar volume.
            avg_volume = (
                p.get("volAvg")
                or p.get("avgVolume")
                or p.get("averageVolume")
                or p.get("volumeAvg")
            )
            price = p.get("price") or p.get("previousClose")
            dollar_volume = None
            try:
                if avg_volume is not None and price is not None:
                    dollar_volume = float(avg_volume) * float(price)
            except (TypeError, ValueError):
                dollar_volume = None

            if dollar_volume is not None and dollar_volume < smallcap_dollar_volume:
                out["pass"] = False
                out["notes"].append(
                    f"Small-cap but low liquidity — avg dollar volume "
                    f"${dollar_volume/1e6:.1f}M < ${smallcap_dollar_volume/1e6:.0f}M"
                )
            elif dollar_volume is not None:
                out["notes"].append(
                    f"Small-cap accepted — market cap ${market_cap/1e6:.0f}M; "
                    f"avg dollar volume ${dollar_volume/1e6:.1f}M"
                )
            else:
                # Do not silently reject a small cap because the profile lacks
                # volume. Existing debt/margin checks remain the quality gate.
                out["notes"].append(
                    f"Small-cap accepted for quality review — market cap "
                    f"${market_cap/1e6:.0f}M; liquidity data unavailable"
                )

        out["name"] = p.get("companyName") or ""
        out["sector"] = p.get("sector", "Unknown")
        out["industry"] = p.get("industry", "Unknown")
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
