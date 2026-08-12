"""Lightweight FMP universe funnel.

Nightly uses only the FMP Company Screener here.
No paid bulk financial-statement endpoints are required.

Market-cap tiers:
A >= $10B
B >= $1B and < $10B
C >= $100M and < $1B
D < $100M

ETFs bypass stock filtering.
"""

import datetime as dt
import json
import os
import urllib.request
from urllib.parse import urlencode

FMP_KEY = os.getenv("FMP_KEY", "")
BASE = "https://financialmodelingprep.com/stable"


def _get(path, params=None):
    if not FMP_KEY:
        raise RuntimeError("FMP_KEY not set")
    q = dict(params or {})
    q["apikey"] = FMP_KEY
    url = f"{BASE}/{path}?{urlencode(q)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Nwing-Bot/nightly"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=45).read())


def earnings_soon_set(days_ahead=7):
    if not FMP_KEY:
        return set(), "FMP_KEY not set — earnings blocker inactive"
    today = dt.date.today()
    to = today + dt.timedelta(days=days_ahead)
    try:
        data = _get(
            "earnings-calendar",
            {"from": str(today), "to": str(to)},
        )
    except Exception as exc:
        return set(), f"earnings calendar unavailable: {str(exc)[:120]}"
    if not isinstance(data, list):
        return set(), "earnings calendar unavailable on this FMP plan"
    syms = {str(row.get("symbol", "")).upper() for row in data}
    return syms, f"earnings calendar loaded ({len(syms)} symbols reporting)"


def market_cap_tier(market_cap, is_etf=False):
    if is_etf:
        return "ETF"
    try:
        mc = float(market_cap)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if mc >= 10e9:
        return "A"
    if mc >= 1e9:
        return "B"
    if mc >= 100e6:
        return "C"
    return "D"


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def screener_context(tickers, meta, stock_cap=500):
    """Return a first-pass universe using FMP screener only.

    The screener supplies market cap, price and volume. We rank within each
    tier by estimated dollar liquidity and keep a bounded stock universe.
    This deliberately does NOT request financial statements.

    Tier quotas preserve exposure across the cap tiers:
      A: up to 200
      B: up to 200
      C: up to 100
    ETFs are untouched and returned separately.
    """
    wanted = {str(t).upper() for t in tickers}
    stocks = [t for t in tickers if meta.get(t, {}).get("type") != "etf"]
    etfs = [t for t in tickers if meta.get(t, {}).get("type") == "etf"]

    if not stocks:
        return {
            "eligible_stocks": [],
            "etfs": etfs,
            "meta": meta,
            "failed": {},
            "tier_counts": {},
            "note": "No stocks in universe",
        }

    # Use the screener's own market-cap floor so < $100M symbols are excluded
    # before any Twelve Data time-series calls.
    rows = _get(
        "company-screener",
        {
            "country": "US",
            "isEtf": "false",
            "isFund": "false",
            "isActivelyTrading": "true",
            "marketCapMoreThan": 100000000,
            "limit": 1000,
        },
    )

    if not isinstance(rows, list):
        raise RuntimeError("FMP company-screener returned an unexpected response")

    by_symbol = {}
    for row in rows:
        s = str(row.get("symbol") or "").upper().strip()
        if s in wanted:
            by_symbol[s] = row

    # If the first page is capped, request additional pages using the same
    # endpoint. This remains a small number of bulk/screener calls, not one
    # call per ticker.
    page = 1
    while len(by_symbol) < len(stocks) and page < 10:
        more = _get(
            "company-screener",
            {
                "country": "US",
                "isEtf": "false",
                "isFund": "false",
                "isActivelyTrading": "true",
                "marketCapMoreThan": 100000000,
                "limit": 1000,
                "page": page,
            },
        )
        if not isinstance(more, list) or not more:
            break
        for row in more:
            s = str(row.get("symbol") or "").upper().strip()
            if s in wanted:
                by_symbol[s] = row
        if len(more) < 1000:
            break
        page += 1

    tier_buckets = {"A": [], "B": [], "C": []}
    failed = {}
    tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "UNKNOWN": 0}

    for t in stocks:
        row = by_symbol.get(t)
        if not row:
            tier_counts["UNKNOWN"] += 1
            failed[t] = ["FMP screener data unavailable"]
            continue

        mc = _num(row.get("marketCap"))
        tier = market_cap_tier(mc)
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

        name = row.get("companyName") or meta.get(t, {}).get("name") or t
        price = _num(row.get("price"))
        volume = _num(row.get("volume"))
        dollar_volume = (price * volume) if price is not None and volume is not None else None

        m = dict(meta.get(t, {}))
        m["name"] = name
        m["sector"] = row.get("sector") or m.get("sector") or "Other"
        m["industry"] = row.get("industry") or m.get("industry") or "Other"
        m["screen"] = {
            "pass": tier in ("A", "B", "C"),
            "tier": tier,
            "market_cap": mc,
            "price": price,
            "volume": volume,
            "dollar_volume": dollar_volume,
            "notes": [],
        }

        if tier == "D":
            m["screen"]["notes"].append("market cap below $100M")
            failed[t] = m["screen"]["notes"]
            meta[t] = m
            continue

        if dollar_volume is None:
            m["screen"]["notes"].append("current dollar volume unavailable")
            failed[t] = m["screen"]["notes"]
            meta[t] = m
            continue

        # Minimum liquidity gate for stocks; C gets a stronger floor.
        if tier == "C" and dollar_volume < 5e6:
            m["screen"]["notes"].append("Tier C dollar liquidity below $5M")
            failed[t] = m["screen"]["notes"]
            meta[t] = m
            continue

        if dollar_volume < 2e6:
            m["screen"]["notes"].append("dollar liquidity below $2M")
            failed[t] = m["screen"]["notes"]
            meta[t] = m
            continue

        m["screen"]["notes"].append("passed market-cap/liquidity funnel")
        meta[t] = m
        tier_buckets[tier].append((dollar_volume, t))

    # Preserve the large-cap universe but put a hard ceiling on TD work.
    # C gets a meaningful allocation so smaller companies are not crowded out.
    quotas = {"A": 200, "B": 200, "C": 100}
    if stock_cap != 500:
        # Scale quotas proportionally while preserving C.
        total = sum(quotas.values())
        quotas = {
            k: max(1, round(stock_cap * v / total))
            for k, v in quotas.items()
        }

    eligible = []
    for tier in ("A", "B", "C"):
        bucket = sorted(tier_buckets[tier], reverse=True)
        eligible.extend(t for _, t in bucket[:quotas[tier]])
        for _, t in bucket[quotas[tier]:]:
            failed[t] = [f"Tier {tier} liquidity rank below nightly cutoff"]

    note = (
        f"FMP screener matched {len(by_symbol)}/{len(stocks)} stocks; "
        f"stock TD cap={len(eligible)}; ETFs untouched={len(etfs)}"
    )
    return {
        "eligible_stocks": eligible,
        "etfs": etfs,
        "meta": meta,
        "failed": failed,
        "tier_counts": tier_counts,
        "note": note,
    }


def company_screen(ticker: str, cfg, is_etf: bool = False) -> dict:
    """Live single-symbol screen (one FMP 'profile' call). Used only where
    a small, on-demand ticker count is expected — the hourly path's
    legacy/dead nightly branch and explain.py's cache-miss fallback — never
    for bulk universe scanning (that's screener_context's job).

    Returns the same shape nightly's bulk screener_context() meta['screen']
    carries: pass / notes / tier / market_cap / sector / industry / name.
    """
    if is_etf:
        return {"pass": True, "notes": [], "tier": "ETF", "market_cap": None,
                "sector": None, "industry": None, "name": ticker}
    try:
        rows = _get("profile", {"symbol": ticker})
    except Exception as exc:
        return {"pass": False, "notes": [f"FMP profile unavailable: {type(exc).__name__}"],
                "tier": "UNKNOWN", "market_cap": None, "sector": None,
                "industry": None, "name": ticker}
    if not isinstance(rows, list) or not rows:
        return {"pass": False, "notes": ["FMP profile: no data"], "tier": "UNKNOWN",
                "market_cap": None, "sector": None, "industry": None, "name": ticker}
    row = rows[0]
    mc = _num(row.get("marketCap"))
    tier = market_cap_tier(mc)
    price = _num(row.get("price"))
    volume = _num(row.get("volume"))
    dollar_volume = (price * volume) if price is not None and volume is not None else None

    notes = []
    passed = True
    if tier == "D":
        notes.append("market cap below $100M")
        passed = False
    elif tier == "C" and (dollar_volume or 0) < 5e6:
        notes.append("Tier C dollar liquidity below $5M")
        passed = False
    elif dollar_volume is not None and dollar_volume < 2e6:
        notes.append("dollar liquidity below $2M")
        passed = False
    if not notes:
        notes.append("passed market-cap/liquidity funnel")

    return {
        "pass": passed, "notes": notes, "tier": tier, "market_cap": mc,
        "sector": row.get("sector"), "industry": row.get("industry"),
        "name": row.get("companyName") or ticker,
    }


def macro_context(spy_daily) -> dict:
    """Lightweight macro read off SPY's own daily price action — no paid
    macro data source required. Purely informational (a line in alerts);
    it never gates a trade.

    risk : 'risk-on' | 'risk-off' | 'neutral' | 'unknown'
    vol  : SPY 20-day realized volatility, annualized, in percent
    """
    try:
        closes = spy_daily["close"].astype(float)
        if len(closes) < 25:
            return {"risk": "unknown", "vol": None}
        rets = closes.pct_change().dropna()
        vol20 = float(rets.tail(20).std() * (252 ** 0.5) * 100)
        ma20 = float(closes.tail(20).mean())
        ma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else ma20
        last = float(closes.iloc[-1])
        if last > ma20 > ma50 and vol20 < 20:
            risk = "risk-on"
        elif last < ma20 < ma50 or vol20 > 30:
            risk = "risk-off"
        else:
            risk = "neutral"
        return {"risk": risk, "vol": round(vol20, 1)}
    except Exception:
        return {"risk": "unknown", "vol": None}
