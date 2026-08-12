"""FMP bulk fundamentals / quality screen.
Nightly only.

Uses:
- company-screener for market cap/profile/liquidity
- income-statement-bulk for profitability
- balance-sheet-statement-bulk for debt/equity

ETFs bypass stock fundamental filters.
"""

import datetime as dt
import json
import os
import urllib.request
from urllib.parse import urlencode

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
    today = dt.date.today()
    to = today + dt.timedelta(days=days_ahead)
    data = _fmp(
        f"earnings-calendar?from={today}&to={to}",
        f"earning_calendar?from={today}&to={to}",
    )
    if not isinstance(data, list):
        return set(), "earnings calendar unavailable on this FMP plan"
    syms = {str(row.get("symbol", "")).upper() for row in data}
    return syms, f"earnings calendar loaded ({len(syms)} symbols reporting)"


def _fmp_get(path, params=None):
    if not FMP_KEY:
        return None, "FMP_KEY not set"
    params = dict(params or {})
    params["apikey"] = FMP_KEY
    url = f"{_BASE_STABLE}/{path}?{urlencode(params)}"
    try:
        return _get(url), None
    except Exception as exc:
        return None, str(exc)


def _bulk_rows(path, years_periods):
    """Try bulk statement snapshots, newest first."""
    last_err = None
    for year, period in years_periods:
        raw, err = _fmp_get(path, {"year": year, "period": period})
        if err:
            last_err = err
            continue
        if isinstance(raw, list) and raw:
            return raw, None, (year, period)
        if raw is not None:
            last_err = f"unexpected response for {path} {year} {period}"
    return [], last_err or f"no data returned by {path}", None


def _row_symbol(row):
    return str(row.get("symbol") or "").upper().strip()


def _first_number(row, *keys):
    for key in keys:
        value = row.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def bulk_context(tickers):
    """Load fundamentals without key-metrics-ttm-bulk.

    Market cap/liquidity:
      company-screener

    Profitability:
      income-statement-bulk. We use the latest available annual/quarterly
      snapshot returned by FMP and calculate net margin from revenue/net income.

    Debt/equity:
      balance-sheet-statement-bulk. We calculate debt/equity from the latest
      available balance-sheet snapshot.

    No per-ticker FMP loop is used.
    """
    wanted = {str(x).upper().strip() for x in tickers if str(x).strip()}
    if not wanted:
        return {}, {}, "No stock tickers requested"
    if not FMP_KEY:
        return {}, {}, "FMP_KEY not set"

    # ------------------------------------------------------------
    # 1) Company screener: market cap + liquidity/profile
    # ------------------------------------------------------------
    screen_rows = []
    page = 0
    page_size = 1000

    while page < 10:
        raw, err = _fmp_get(
            "company-screener",
            {
                "country": "US",
                "isEtf": "false",
                "isFund": "false",
                "isActivelyTrading": "true",
                "limit": page_size,
                "page": page,
            },
        )
        if err:
            print(f"FMP screener error page={page}: {err[:160]}")
            break
        if not isinstance(raw, list):
            break

        screen_rows.extend(raw)
        if len(raw) < page_size:
            break
        page += 1

    profiles = {}
    for row in screen_rows:
        s = _row_symbol(row)
        if s in wanted:
            profiles[s] = {
                "symbol": s,
                "companyName": row.get("companyName"),
                "sector": row.get("sector"),
                "industry": row.get("industry"),
                "marketCap": row.get("marketCap"),
                "price": row.get("price"),
                "volume": row.get("volume"),
                "volAvg": row.get("volume") or row.get("volAvg"),
            }

    # ------------------------------------------------------------
    # 2) Bulk income statement: profitability
    # ------------------------------------------------------------
    # Prefer the most recent annual statement, then current/recent quarters.
    # This is deliberately bulk: no 3,000+ individual API calls.
    today = dt.date.today()
    current_year = today.year
    periods = [
        (current_year, "Q2"),
        (current_year, "Q1"),
        (current_year - 1, "Q4"),
        (current_year - 1, "Q3"),
        (current_year - 1, "Q2"),
        (current_year - 1, "FY"),
    ]

    income_rows, income_err, income_period = _bulk_rows(
        "income-statement-bulk", periods
    )

    # Keep the newest statement available for each symbol.
    income_by_symbol = {}
    for row in income_rows:
        s = _row_symbol(row)
        if s in wanted and s not in income_by_symbol:
            income_by_symbol[s] = row

    # ------------------------------------------------------------
    # 3) Bulk balance sheet: debt/equity
    # ------------------------------------------------------------
    balance_rows, balance_err, balance_period = _bulk_rows(
        "balance-sheet-statement-bulk", periods
    )

    balance_by_symbol = {}
    for row in balance_rows:
        s = _row_symbol(row)
        if s in wanted and s not in balance_by_symbol:
            balance_by_symbol[s] = row

    # ------------------------------------------------------------
    # 4) Normalize into the old "ratios" shape expected by nightly.py
    # ------------------------------------------------------------
    ratios = {}

    for s in wanted:
        inc = income_by_symbol.get(s, {})
        bal = balance_by_symbol.get(s, {})

        revenue = _first_number(
            inc,
            "revenue",
            "revenueTTM",
            "totalRevenue",
        )
        net_income = _first_number(
            inc,
            "netIncome",
            "netIncomeTTM",
            "netIncomeCommonStockholders",
        )

        margin = None
        if revenue not in (None, 0) and net_income is not None:
            margin = net_income / revenue

        total_debt = _first_number(
            bal,
            "totalDebt",
            "totalDebtAndCapital",
            "totalDebtTTM",
        )
        total_equity = _first_number(
            bal,
            "totalStockholdersEquity",
            "stockholdersEquity",
            "totalEquity",
            "totalEquityGrossMinorityInterest",
        )

        de = None
        if total_equity not in (None, 0) and total_debt is not None:
            de = total_debt / total_equity

        # Preserve useful raw fields for downstream code/debugging.
        ratios[s] = {
            **inc,
            "marketCap": (
                _first_number(inc, "marketCap", "marketCapTTM")
                or profiles.get(s, {}).get("marketCap")
            ),
            "debtToEquityTTM": de,
            "debtEquityRatioTTM": de,
            "netProfitMarginTTM": margin,
            "revenueBulk": revenue,
            "netIncomeBulk": net_income,
            "totalDebtBulk": total_debt,
            "totalEquityBulk": total_equity,
            "incomePeriod": income_period,
            "balancePeriod": balance_period,
        }

    note = (
        f"FMP bulk source: company-screener {len(profiles)}/{len(wanted)}, "
        f"income-statement-bulk {len(income_by_symbol)}/{len(wanted)}, "
        f"balance-sheet-statement-bulk {len(balance_by_symbol)}/{len(wanted)}"
    )
    print(note)

    if len(profiles) == 0:
        raise RuntimeError(
            "FMP company-screener returned no requested stocks. "
            "Check FMP API access/plan and API key."
        )

    if len(income_by_symbol) == 0 or len(balance_by_symbol) == 0:
        detail = income_err or balance_err or "bulk financial statements unavailable"
        raise RuntimeError(
            "FMP bulk financial statements unavailable: " + detail[:300]
        )

    return profiles, ratios, note


def market_cap_tier(market_cap, is_etf=False):
    if is_etf:
        return "ETF"
    if market_cap is None:
        return "UNKNOWN"

    mc = float(market_cap)

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
    out = {
        "pass": True,
        "notes": [],
        "tier": "ETF" if is_etf else "UNKNOWN",
    }

    if is_etf:
        out["notes"].append("ETF — market-cap/fundamental stock filter bypassed")
        return out

    p = profile or {}
    r = ratios or {}

    if not p:
        out["pass"] = False
        out["notes"].append("FMP bulk profile unavailable — stock skipped")
        return out

    mc = (
        p.get("mktCap")
        or p.get("marketCap")
        or r.get("marketCap")
        or r.get("marketCapTTM")
    )
    try:
        mc = float(mc) if mc is not None else None
    except (TypeError, ValueError):
        mc = None

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

    # D is intentionally silent in nightly notification formatting.
    if tier == "D":
        out["pass"] = False
        out["notes"].append("microcap: market cap < $100M")
        return out

    de = (
        r.get("debtEquityRatioTTM")
        or r.get("debtToEquityTTM")
        or r.get("debtToEquityRatioTTM")
    )
    margin = r.get("netProfitMarginTTM")

    try:
        de_f = float(de) if de is not None else None
    except (TypeError, ValueError):
        de_f = None

    try:
        margin_f = float(margin) if margin is not None else None
    except (TypeError, ValueError):
        margin_f = None

    out["debt_to_equity"] = de_f
    out["net_margin"] = margin_f

    avg_vol = (
        p.get("volAvg")
        or p.get("avgVolume")
        or p.get("averageVolume")
        or p.get("volumeAvg")
        or p.get("volume")
    )
    price = p.get("price") or p.get("previousClose")

    try:
        dollar_vol = (
            float(avg_vol) * float(price)
            if avg_vol is not None and price is not None
            else None
        )
    except (TypeError, ValueError):
        dollar_vol = None

    out["avg_dollar_volume"] = dollar_vol

    # Tier C: liquidity + profitable/debt quality.
    if tier == "C":
        if de_f is None or de_f > cfg.TIER_C_MAX_DEBT_TO_EQUITY:
            out["pass"] = False
            out["notes"].append(
                "Tier C: debt/equity unavailable or above 1.50"
            )

        if margin_f is None or margin_f < cfg.TIER_C_MIN_NET_MARGIN:
            out["pass"] = False
            out["notes"].append(
                "Tier C: not profitable / net margin unavailable"
            )

        if dollar_vol is None or dollar_vol < cfg.TIER_C_MIN_DOLLAR_VOLUME:
            out["pass"] = False
            out["notes"].append(
                "Tier C: average dollar volume unavailable or below $5M"
            )
        else:
            out["notes"].append(
                f"Tier C liquidity OK: avg dollar volume ${dollar_vol/1e6:.1f}M"
            )

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
    """Risk regime from SPY's own realized volatility."""
    ctx = {"risk": "neutral", "vol": None}
    try:
        if spy_daily is not None and len(spy_daily) > 25:
            r = spy_daily["close"].pct_change().dropna()
            vol = float(r.iloc[-20:].std() * (252 ** 0.5))
            ctx["vol"] = round(vol * 100, 1)
            ctx["risk"] = (
                "risk-off"
                if vol > 0.22
                else "risk-on"
                if vol < 0.13
                else "neutral"
            )
    except Exception:
        pass
    return ctx
