"""Signal intelligence follow-up.

Combines the latest hourly-scan technical signal with FMP fundamentals,
earnings, analyst estimates/ratings, news, sector/industry, and macro data.
The LLM interprets supplied numbers; it does not calculate or create a new
trading signal.

Inputs
------
- alerted.json: tells us which tickers were recently alerted and the trade
  entry/eToro TP value already produced by the scanner.
- docs/signals.json: the full technical analysis produced by hourly-scan.

Manual testing
--------------
    python news_llm.py AAPL,NVDA
    python news_llm.py NVDA --provider anthropic

Environment
-----------
FMP_KEY
ANTHROPIC_API_KEY / OPENAI_API_KEY
LLM_PROVIDER = auto | anthropic | openai | both
ANTHROPIC_MODEL / OPENAI_MODEL / LLM_MODEL (optional)
NEWS_LOOKBACK_MIN = 90
NEWS_MAX_TICKERS = 12
NEWS_MAX_AGE_HOURS = 48
NEWS_ENABLED = 1
DRY_RUN = 0
"""

import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import config as cfg
import notify


BASE = "https://financialmodelingprep.com/stable"
FMP_KEY = os.getenv("FMP_KEY", "").strip()
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "1") != "0"
LOOKBACK_MIN = int(os.getenv("NEWS_LOOKBACK_MIN", "90"))
MAX_TICKERS = int(os.getenv("NEWS_MAX_TICKERS", "12"))
NEWS_LIMIT = int(os.getenv("NEWS_HEADLINES_PER_TICKER", "8"))
NEWS_MAX_AGE_HOURS = int(os.getenv("NEWS_MAX_AGE_HOURS", "48"))
FMP_TIMEOUT = int(os.getenv("FMP_TIMEOUT", "25"))


PROMPT = """You are the interpretation layer for a technical trading scanner.
The scanner has ALREADY generated the technical signal. Do NOT create a new
buy/sell/entry/target. Do NOT invent missing numbers.

Your job is to explain whether the technical setup is supported or weakened by
recent fundamentals, earnings, analyst expectations, news, sector/industry,
and macro conditions.

Ticker: {ticker}
Company/ETF: {name}

TRADE CONTEXT FROM HOURLY-SCAN
{trade_context}

TECHNICAL ANALYSIS FROM HOURLY-SCAN
{technical}

LAST 3 FINANCIAL RESULTS / EARNINGS
{financials}

NEXT EARNINGS / FORWARD EXPECTATIONS
{forward}

ANALYST / RATINGS DATA
{analysts}

RECENT NEWS
{news}

SECTOR / INDUSTRY
{sector}

MACRO
{macro}

Return ONLY valid JSON with exactly these keys:
{{
  "technical_lookalike": "one concise setup characterization, e.g. bullish continuation, momentum breakout, pullback continuation, range breakout, trend recovery, extended uptrend, or mixed/conflicting",
  "technical_interpretation": "2 short sentences explaining what the existing technical signal resembles and why",
  "financial_assessment": "positive|mixed|negative|not_applicable",
  "financial_interpretation": "2 short sentences interpreting the last three results; mention acceleration/deceleration, margins, cash flow or surprises only when supported by the supplied data",
  "earnings_assessment": "positive|mixed|negative|unknown",
  "earnings_interpretation": "1-2 short sentences about the next earnings date, estimate direction and event risk",
  "analyst_assessment": "positive|mixed|negative|unknown",
  "analyst_interpretation": "1 short sentence about ratings and estimate direction",
  "news_assessment": "positive|mixed|negative|no_major_risk|no_recent_news|error",
  "news_interpretation": "1-2 short sentences summarizing meaningful company-specific news risk; do not overreact to routine analyst notes",
  "sector_assessment": "supportive|neutral|headwind|unknown",
  "sector_interpretation": "1 short sentence comparing the stock/ETF setup with its sector/industry context",
  "macro_assessment": "supportive|neutral|headwind|unknown",
  "macro_interpretation": "1 short sentence about macro relevance to this setup",
  "overall_assessment": "supportive|mixed|caution|insufficient_data",
  "overall_summary": "2-3 short sentences synthesizing the technical signal with the fundamental/catalyst context",
  "key_positive": "one sentence",
  "key_risk": "one sentence"
}}"""


# ---------------------------------------------------------------------------
# Generic HTTP / JSON

def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_dt(value):
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:19], fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return None


def fnum(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def pct_change(new, old):
    a, b = fnum(new), fnum(old)
    if a is None or b in (None, 0):
        return None
    return (a / b) - 1


def fmt_money(v, digits=2):
    n = fnum(v)
    if n is None:
        return "n/a"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1e12:
        return f"{sign}${n/1e12:.{digits}f}T"
    if n >= 1e9:
        return f"{sign}${n/1e9:.{digits}f}B"
    if n >= 1e6:
        return f"{sign}${n/1e6:.{digits}f}M"
    return f"{sign}${n:,.{digits}f}"


def fmt_pct(v, digits=1):
    n = fnum(v)
    return "n/a" if n is None else f"{n:+.{digits}%}"


def clean_json_text(text):
    if not text:
        return ""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def http_json(url, headers=None, timeout=FMP_TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Nwing-Bot/2.0",
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            status = getattr(r, "status", 200)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {raw[:900].replace(chr(10), ' ')}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError("request timed out") from e

    if not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status}: {raw[:900]}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON: {raw[:500]}") from e


def fmp(endpoint, params=None, label="FMP"):
    if not FMP_KEY:
        raise RuntimeError("FMP_KEY is not set")
    params = dict(params or {})
    params["apikey"] = FMP_KEY
    url = f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        data = http_json(url, headers={"apikey": FMP_KEY})
    except RuntimeError as e:
        raise RuntimeError(f"{label}: {e}") from e
    if isinstance(data, dict):
        err = data.get("Error Message") or data.get("error") or data.get("message")
        if err:
            raise RuntimeError(f"{label}: {err}")
    return data


# ---------------------------------------------------------------------------
# Hourly-scan inputs

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Could not read {path}: {e}")
        return default


def recent_alerts():
    ledger = load_json("alerted.json", {})
    now = utc_now()
    out = []
    for ticker, entry in ledger.items():
        d = parse_dt(entry.get("alerted")) if isinstance(entry, dict) else None
        if not d:
            continue
        age = (now - d).total_seconds()
        if 0 <= age <= LOOKBACK_MIN * 60:
            out.append((ticker.upper(), entry))
    out.sort(key=lambda x: x[1].get("alerted", ""), reverse=True)
    return out[:MAX_TICKERS]


def load_signals():
    doc = load_json("docs/signals.json", {})
    return {str(s.get("ticker", "")).upper(): s for s in doc.get("signals", [])}


def technical_text(sig):
    if not sig:
        return "No matching hourly-scan signal record was found."
    qd = sig.get("q_detail") or {}
    qn = sig.get("q_notes") or {}
    lines = [
        f"Setup: {sig.get('setup', 'n/a')}",
        f"Daily regime: {sig.get('regime', 'n/a')} for ~{sig.get('regime_weeks', 'n/a')} weeks",
        f"Trend score: {sig.get('trend_score', 'n/a')}/5",
        f"ADX: {sig.get('adx', 'n/a')}",
        f"Relative strength vs SPY (3m): {fmt_pct(sig.get('rs'))}",
        f"RSI (4h): {sig.get('rsi', 'n/a')}",
        f"EMA20 / EMA50 / EMA200: {sig.get('ema20', 'n/a')} / {sig.get('ema50', 'n/a')} / {sig.get('ema200', 'n/a')}",
        f"Above EMA200: {sig.get('above_ema200', 'n/a')}",
        f"Quality: {sig.get('quality', 'n/a')}",
        f"Runner: {sig.get('runner', False)}",
        f"Bollinger: {qd.get('bollinger', 'n/a')} — {qn.get('bollinger', '')}",
        f"VWAP: {qd.get('vwap', 'n/a')} — {qn.get('vwap', '')}",
        f"Volume: {qd.get('volume', 'n/a')} — {qn.get('volume', '')}",
        f"Extension: {qd.get('extension', 'n/a')} — {qn.get('extension', '')}",
        f"Options context: {sig.get('options_note') or 'none'}",
        f"Reasons: {'; '.join(sig.get('reasons') or []) or 'n/a'}",
        f"Warnings: {'; '.join(sig.get('warnings') or []) or 'none'}",
    ]
    return "\n".join(lines)


def trade_text(sig, alert):
    entry = fnum((sig or {}).get("entry")) or fnum(alert.get("entry"))
    etoro_tp = fnum((sig or {}).get("pl_amount"))
    if etoro_tp is None:
        # Keep compatibility with the ledger if a future scanner writes it there.
        etoro_tp = fnum(alert.get("pl_amount"))
    tp_pct = fnum((sig or {}).get("tp_pct"))
    eta_lo = (sig or {}).get("eta_days_low")
    eta_hi = (sig or {}).get("eta_days_high")
    return "\n".join([
        f"Entry: ${entry:.2f}" if entry is not None else "Entry: n/a",
        f"eToro TP value: ${etoro_tp:.2f}" if etoro_tp is not None else "eToro TP value: n/a",
        f"Scanner potential: {fmt_pct(tp_pct)}" if tp_pct is not None else "Scanner potential: n/a",
        f"Estimated scanner timeframe: {eta_lo}-{eta_hi} trading days" if eta_lo and eta_hi else "Estimated scanner timeframe: n/a",
        f"Scanner time: {(sig or {}).get('time', alert.get('alerted', 'n/a'))}",
    ])


# ---------------------------------------------------------------------------
# FMP collection

def profile(ticker):
    try:
        rows = fmp("profile", {"symbol": ticker}, "profile")
        return rows[0] if isinstance(rows, list) and rows else {}
    except Exception as e:
        print(f"  {ticker} profile unavailable: {e}")
        return {}


def earnings_history(ticker, limit=8):
    try:
        rows = fmp("earnings", {"symbol": ticker, "limit": limit}, "earnings")
        return rows if isinstance(rows, list) else []
    except Exception as e:
        print(f"  {ticker} earnings unavailable: {e}")
        return []


def income_statements(ticker, limit=5):
    try:
        rows = fmp("income-statement", {"symbol": ticker, "period": "quarter", "limit": limit}, "income statement")
        return rows if isinstance(rows, list) else []
    except Exception as e:
        print(f"  {ticker} income statements unavailable: {e}")
        return []


def balance_sheet(ticker):
    try:
        rows = fmp("balance-sheet-statement", {"symbol": ticker, "period": "quarter", "limit": 1}, "balance sheet")
        return rows[0] if isinstance(rows, list) and rows else {}
    except Exception as e:
        print(f"  {ticker} balance sheet unavailable: {e}")
        return {}


def cash_flow(ticker):
    try:
        rows = fmp("cash-flow-statement", {"symbol": ticker, "period": "quarter", "limit": 1}, "cash flow")
        return rows[0] if isinstance(rows, list) and rows else {}
    except Exception as e:
        print(f"  {ticker} cash flow unavailable: {e}")
        return {}


def analyst_estimates(ticker):
    try:
        rows = fmp("analyst-estimates", {"symbol": ticker, "period": "annual", "page": 0, "limit": 4}, "analyst estimates")
        return rows if isinstance(rows, list) else []
    except Exception as e:
        print(f"  {ticker} analyst estimates unavailable: {e}")
        return []


def analyst_grades(ticker):
    try:
        rows = fmp("grades-consensus", {"symbol": ticker}, "grades consensus")
        if isinstance(rows, list):
            return rows[0] if rows else {}
        return rows if isinstance(rows, dict) else {}
    except Exception as e:
        print(f"  {ticker} grades unavailable: {e}")
        return {}


def sector_snapshot(date):
    try:
        rows = fmp("sector-performance-snapshot", {"date": date}, "sector snapshot")
        return rows if isinstance(rows, list) else []
    except Exception as e:
        print(f"  sector snapshot unavailable: {e}")
        return []


def industry_snapshot(date):
    try:
        rows = fmp("industry-performance-snapshot", {"date": date}, "industry snapshot")
        return rows if isinstance(rows, list) else []
    except Exception as e:
        print(f"  industry snapshot unavailable: {e}")
        return []


def news(ticker):
    try:
        rows = fmp("news/stock", {"symbols": ticker, "limit": NEWS_LIMIT}, "stock news")
    except Exception as e:
        return [], {"status": "FMP_ERROR", "message": str(e)}
    if not isinstance(rows, list):
        return [], {"status": "NO_NEWS", "message": "unexpected empty news response"}

    now = utc_now()
    out = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip()
        if not title:
            continue
        d = parse_dt(a.get("publishedDate") or a.get("publishedAt") or a.get("date"))
        if d:
            age = (now - d).total_seconds() / 3600
            if age < -2 or age > NEWS_MAX_AGE_HOURS:
                continue
        out.append({
            "date": d.strftime("%Y-%m-%d %H:%M UTC") if d else str(a.get("publishedDate") or a.get("date") or ""),
            "title": title,
            "site": str(a.get("site") or a.get("publisher") or ""),
            "url": str(a.get("url") or ""),
            "text": str(a.get("text") or a.get("content") or "")[:1000],
        })
        if len(out) >= NEWS_LIMIT:
            break
    return out, {"status": "OK" if out else "NO_NEWS", "message": f"{len(out)} usable article(s)"}


def earnings_calendar_all(tickers):
    """One call for the next 120 days, then filter locally."""
    today = dt.date.today()
    end = today + dt.timedelta(days=120)
    try:
        rows = fmp("earnings-calendar", {"from": today.isoformat(), "to": end.isoformat()}, "earnings calendar")
    except Exception as e:
        print(f"  earnings calendar unavailable: {e}")
        return {t: [] for t in tickers}
    out = {t: [] for t in tickers}
    if not isinstance(rows, list):
        return out
    wanted = set(tickers)
    for row in rows:
        t = str(row.get("symbol") or "").upper()
        if t in wanted:
            out[t].append(row)
    for t in out:
        out[t].sort(key=lambda r: str(r.get("date") or r.get("earningsDate") or ""))
    return out


def macro_data():
    out = {"treasury": {}, "indicators": {}, "calendar": []}
    try:
        rows = fmp("treasury-rates", {}, "treasury rates")
        if isinstance(rows, list) and rows:
            out["treasury"] = rows[0]
    except Exception as e:
        print(f"  treasury data unavailable: {e}")

    # These names are deliberately best-effort because FMP can vary the exact
    # indicator catalog available to a plan. Missing indicators are omitted.
    for name in ("GDP", "CPI", "unemploymentRate"):
        try:
            rows = fmp("economic-indicators", {"name": name, "limit": 2}, f"economic indicator {name}")
            if isinstance(rows, list):
                out["indicators"][name] = rows[:2]
        except Exception as e:
            print(f"  {name} unavailable: {e}")

    today = dt.date.today()
    end = today + dt.timedelta(days=14)
    try:
        rows = fmp("economic-calendar", {"from": today.isoformat(), "to": end.isoformat()}, "economic calendar")
        if isinstance(rows, list):
            # Keep only relatively market-relevant items and avoid flooding the LLM.
            rows = sorted(rows, key=lambda r: str(r.get("date") or ""))
            out["calendar"] = rows[:12]
    except Exception as e:
        print(f"  economic calendar unavailable: {e}")
    return out


def build_financials(ticker, earnings, income, balance, cash):
    # ETFs/funds generally have no corporate quarterly statements. We detect
    # this via profile type later, but also gracefully return no applicable data.
    results = []
    # Earnings endpoint is the best source for actual vs estimate and surprise.
    hist = []
    now = utc_now()
    for e in earnings:
        d = parse_dt(e.get("date"))
        if d and d <= now:
            hist.append(e)
    hist.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    hist = hist[:3]

    for e in hist:
        date = str(e.get("date") or "n/a")[:10]
        actual = fnum(e.get("epsActual") if e.get("epsActual") is not None else e.get("eps"))
        est = fnum(e.get("epsEstimated") if e.get("epsEstimated") is not None else e.get("epsEstimate"))
        surprise = fnum(e.get("epsSurprise"))
        if surprise is None and actual is not None and est not in (None, 0):
            surprise = actual - est
        results.append({
            "date": date,
            "fiscal_period": e.get("fiscalDateEnding") or e.get("fiscalYear") or e.get("period") or "",
            "eps_actual": actual,
            "eps_estimate": est,
            "eps_surprise": surprise,
            "eps_surprise_pct": (surprise / abs(est)) if surprise is not None and est not in (None, 0) else None,
            "revenue": None,
            "net_income": None,
            "operating_margin": None,
            "gross_margin": None,
            "revenue_yoy": None,
            "net_income_yoy": None,
        })

    # Match income statements by fiscal period/date where possible.
    inc = sorted(income, key=lambda r: str(r.get("date") or ""), reverse=True)
    for r in results:
        best = None
        for row in inc:
            row_date = str(row.get("date") or "")[:10]
            fiscal = str(row.get("fiscalDateEnding") or "")[:10]
            if row_date == r["date"] or fiscal == str(r.get("fiscal_period"))[:10]:
                best = row
                break
        if best is None:
            # fallback to nearest date within 45 days
            rd = parse_dt(r["date"])
            candidates = []
            for row in inc:
                dd = parse_dt(row.get("date"))
                if rd and dd:
                    days = abs((rd - dd).days)
                    if days <= 45:
                        candidates.append((days, row))
            if candidates:
                best = sorted(candidates, key=lambda x: x[0])[0][1]
        if best:
            r["revenue"] = fnum(best.get("revenue"))
            r["net_income"] = fnum(best.get("netIncome"))
            op = fnum(best.get("operatingIncome"))
            gp = fnum(best.get("grossProfit"))
            r["operating_margin"] = op / r["revenue"] if op is not None and r["revenue"] else None
            r["gross_margin"] = gp / r["revenue"] if gp is not None and r["revenue"] else None

    # Sequential growth for the 3 displayed quarters.
    for i, r in enumerate(results):
        if i + 1 < len(results):
            prev = results[i + 1]
            r["revenue_yoy"] = pct_change(r.get("revenue"), prev.get("revenue"))
            r["net_income_yoy"] = pct_change(r.get("net_income"), prev.get("net_income"))

    health = {
        "cash": fnum(balance.get("cashAndCashEquivalents") or balance.get("cashAndShortTermInvestments")),
        "total_debt": fnum(balance.get("totalDebt")),
        "total_liabilities": fnum(balance.get("totalLiabilities")),
        "equity": fnum(balance.get("totalStockholdersEquity") or balance.get("totalEquity")),
        "operating_cash_flow": fnum(cash.get("operatingCashFlow")),
        "capital_expenditure": fnum(cash.get("capitalExpenditure")),
        "free_cash_flow": fnum(cash.get("freeCashFlow")),
    }
    if health["free_cash_flow"] is None and health["operating_cash_flow"] is not None and health["capital_expenditure"] is not None:
        health["free_cash_flow"] = health["operating_cash_flow"] + health["capital_expenditure"]
    return results, health


def forward_data(earnings_calendar, estimates):
    future = []
    today = dt.date.today()
    for r in earnings_calendar:
        d = parse_dt(r.get("date") or r.get("earningsDate"))
        if d and d.date() >= today:
            future.append(r)
    next_earn = future[0] if future else {}

    ests = []
    for e in estimates:
        ests.append({
            "date": e.get("date"),
            "period": e.get("period"),
            "revenue_avg": fnum(e.get("revenueAvg") or e.get("revenueAverage")),
            "eps_avg": fnum(e.get("epsAvg") or e.get("epsAverage")),
            "revenue_low": fnum(e.get("revenueLow")),
            "revenue_high": fnum(e.get("revenueHigh")),
            "eps_low": fnum(e.get("epsLow")),
            "eps_high": fnum(e.get("epsHigh")),
            "num_analysts_revenue": e.get("numAnalystsRevenue"),
            "num_analysts_eps": e.get("numAnalystsEps"),
        })
    return {"next_earnings": next_earn, "estimates": ests[:4]}


def analyst_data(grades, estimates):
    # Keep the raw grade counts because field names can differ slightly by FMP
    # response version. The LLM sees the actual supplied values.
    out = {"grades_consensus": grades, "estimate_direction": {}}
    if len(estimates) >= 2:
        a, b = estimates[0], estimates[1]
        for key in ("revenueAvg", "epsAvg", "revenueAverage", "epsAverage"):
            if key in a and key in b:
                out["estimate_direction"][key] = {
                    "latest": a.get(key),
                    "prior": b.get(key),
                    "change": pct_change(a.get(key), b.get(key)),
                }
    return out


def sector_data(profile_row, sectors, industries):
    sector = str(profile_row.get("sector") or "Unknown")
    industry = str(profile_row.get("industry") or "Unknown")
    srow = next((r for r in sectors if str(r.get("sector") or "").lower() == sector.lower()), {})
    irow = next((r for r in industries if str(r.get("industry") or "").lower() == industry.lower()), {})
    return {
        "sector": sector,
        "industry": industry,
        "sector_snapshot": srow,
        "industry_snapshot": irow,
    }


# ---------------------------------------------------------------------------
# Text for LLM

def compact_json(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)[:12000]


def financial_text(results, health):
    if not results:
        return "No quarterly financial results were returned by FMP (may be an ETF/fund or the data may not be available on the plan)."
    lines = []
    for r in results:
        lines.append(
            f"{r['date']} | EPS {r['eps_actual']} vs est {r['eps_estimate']} | surprise {fmt_pct(r['eps_surprise_pct'])} | "
            f"revenue {fmt_money(r['revenue'])} | revenue growth vs prior displayed period {fmt_pct(r['revenue_yoy'])} | "
            f"net income {fmt_money(r['net_income'])} | net income growth {fmt_pct(r['net_income_yoy'])} | "
            f"gross margin {fmt_pct(r['gross_margin'])} | operating margin {fmt_pct(r['operating_margin'])}"
        )
    lines.append(
        "Latest financial health: "
        + ", ".join([
            f"cash {fmt_money(health.get('cash'))}",
            f"debt {fmt_money(health.get('total_debt'))}",
            f"operating cash flow {fmt_money(health.get('operating_cash_flow'))}",
            f"free cash flow {fmt_money(health.get('free_cash_flow'))}",
        ])
    )
    return "\n".join(lines)


def forward_text(data):
    ne = data.get("next_earnings") or {}
    lines = []
    d = parse_dt(ne.get("date") or ne.get("earningsDate"))
    if d:
        days = (d.date() - dt.date.today()).days
        lines.append(f"Next earnings: {d.date().isoformat()} ({days} days from today)")
        if ne.get("epsEstimated") is not None:
            lines.append(f"Next EPS estimate: {ne.get('epsEstimated')}")
        if ne.get("revenueEstimated") is not None:
            lines.append(f"Next revenue estimate: {fmt_money(ne.get('revenueEstimated'))}")
        if ne.get("time"):
            lines.append(f"Report timing: {ne.get('time')}")
    else:
        lines.append("Next earnings date: unavailable")
    if data.get("estimates"):
        lines.append("Forward analyst estimates:")
        for e in data["estimates"]:
            lines.append(compact_json(e))
    return "\n".join(lines)


def analysts_text(data):
    g = data.get("grades_consensus") or {}
    if not g:
        return "Analyst ratings unavailable."
    return compact_json(g) + "\nEstimate direction:\n" + compact_json(data.get("estimate_direction") or {})


def news_text(rows, diagnostics):
    if diagnostics.get("status") == "FMP_ERROR":
        return f"FMP news error: {diagnostics.get('message')}"
    if not rows:
        return "No recent usable company-specific news was returned by FMP."
    lines = []
    for r in rows:
        lines.append(f"- {r['date']} — {r['title']} [{r['site']}]")
        if r.get("text"):
            lines.append(f"  Context: {r['text'][:500]}")
    return "\n".join(lines)


def sector_text(data):
    return compact_json(data)


def macro_text(data):
    return compact_json(data)


# ---------------------------------------------------------------------------
# LLM

def provider():
    wanted = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    has_a = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    has_o = bool(os.getenv("OPENAI_API_KEY", "").strip())
    if wanted == "both" and has_a and has_o:
        return "both"
    if wanted == "anthropic" and has_a:
        return "anthropic"
    if wanted == "openai" and has_o:
        return "openai"
    if wanted == "both":
        return "anthropic" if has_a else ("openai" if has_o else None)
    return "anthropic" if has_a else ("openai" if has_o else None)


def post_json(url, payload, headers):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=50) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {raw[:1000].replace(chr(10), ' ')}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from e
    return json.loads(raw)


def call_llm(which, prompt):
    try:
        common = os.getenv("LLM_MODEL", "").strip()
        if which == "anthropic":
            model = os.getenv("ANTHROPIC_MODEL", "").strip() or common or "claude-sonnet-4-6"
            resp = post_json(
                "https://api.anthropic.com/v1/messages",
                {
                    "model": model,
                    "max_tokens": 900,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                },
                {
                    "x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
                    "anthropic-version": "2023-06-01",
                },
            )
            text = "".join(x.get("text", "") for x in resp.get("content", []) if x.get("type") == "text")
        else:
            model = os.getenv("OPENAI_MODEL", "").strip() or common or "gpt-4o-mini"
            resp = post_json(
                "https://api.openai.com/v1/chat/completions",
                {
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 900,
                    "messages": [{"role": "user", "content": prompt}],
                },
                {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}"},
            )
            text = resp["choices"][0]["message"]["content"]
        print(f"  {which}: received {len(text)} chars")
        return text
    except Exception as e:
        print(f"  {which.upper()} LLM ERROR: {str(e)[:900]}")
        return None


def parse_llm(text):
    if not text:
        return None
    cleaned = clean_json_text(text)
    try:
        start = cleaned.index("{")
        end = cleaned.rindex("}") + 1
        obj = json.loads(cleaned[start:end])
    except Exception as e:
        print(f"  LLM JSON parse error: {e}")
        print(f"  LLM raw response (first 1200 chars): {cleaned[:1200]}")
        return None
    expected = [
        "technical_lookalike", "technical_interpretation", "financial_assessment",
        "financial_interpretation", "earnings_assessment", "earnings_interpretation",
        "analyst_assessment", "analyst_interpretation", "news_assessment",
        "news_interpretation", "sector_assessment", "sector_interpretation",
        "macro_assessment", "macro_interpretation", "overall_assessment",
        "overall_summary", "key_positive", "key_risk",
    ]
    for k in expected:
        obj[k] = str(obj.get(k, "")).strip()
    return obj


def ask(prompt, which):
    if which == "both":
        a = parse_llm(call_llm("anthropic", prompt))
        o = parse_llm(call_llm("openai", prompt))
        if not a and not o:
            return None, "both-failed"
        if a and o:
            # Prefer the more cautious overall assessment if the providers disagree.
            rank = {"supportive": 0, "mixed": 1, "caution": 2, "insufficient_data": 3}
            chosen = max((a, "anthropic"), (o, "openai"), key=lambda x: rank.get(x[0].get("overall_assessment"), 3))
            result = dict(chosen[0])
            if a.get("overall_assessment") != o.get("overall_assessment"):
                result["overall_summary"] += f" Provider check: Anthropic={a.get('overall_assessment')}; OpenAI={o.get('overall_assessment')}."
            return result, "both"
        return (a, "anthropic") if a else (o, "openai")
    result = parse_llm(call_llm(which, prompt))
    return result, which


# ---------------------------------------------------------------------------
# Report formatting

def icon_assessment(value):
    return {
        "positive": "🟢", "supportive": "🟢", "no_major_risk": "🟢",
        "mixed": "🟡", "neutral": "🟡", "unknown": "⚪", "no_recent_news": "⚪",
        "negative": "🔴", "headwind": "🔴", "caution": "🟠", "error": "⚠️",
        "not_applicable": "—", "insufficient_data": "⚪",
    }.get(str(value).lower(), "⚪")


def format_financial_lines(results):
    lines = []
    for r in results:
        surprise = r.get("eps_surprise_pct")
        label = "strong/positive" if surprise is not None and surprise >= 0.05 else "weak/negative" if surprise is not None and surprise <= -0.05 else "mixed/neutral"
        details = [
            f"EPS {r.get('eps_actual') if r.get('eps_actual') is not None else 'n/a'} vs est {r.get('eps_estimate') if r.get('eps_estimate') is not None else 'n/a'}",
            f"surprise {fmt_pct(surprise)}",
            f"revenue {fmt_money(r.get('revenue'))}",
            f"rev growth {fmt_pct(r.get('revenue_yoy'))}",
            f"operating margin {fmt_pct(r.get('operating_margin'))}",
        ]
        lines.append(f"- {r.get('date')} — {label} — " + ", ".join(details))
    return lines


def format_report(row):
    sig = row.get("signal") or {}
    alert = row.get("alert") or {}
    llm = row.get("llm") or {}
    prof = row.get("profile") or {}
    results = row.get("financials") or []
    forward = row.get("forward") or {}
    next_e = forward.get("next_earnings") or {}
    analysts = row.get("analysts") or {}
    sector = row.get("sector") or {}
    news_rows = row.get("news") or []

    ticker = row["ticker"]
    name = prof.get("companyName") or sig.get("name") or ticker
    entry = fnum(sig.get("entry")) or fnum(alert.get("entry"))
    etoro = fnum(sig.get("pl_amount"))
    if etoro is None:
        etoro = fnum(alert.get("pl_amount"))
    potential = fnum(sig.get("tp_pct"))

    lines = [
        f"🟢 {ticker} — {name}",
        f"Entry: ${entry:.2f}" if entry is not None else "Entry: n/a",
        f"eToro TP value: ${etoro:.2f}" if etoro is not None else "eToro TP value: n/a",
        f"Scanner potential: {fmt_pct(potential)}" if potential is not None else "Scanner potential: n/a",
        "",
        "═══ TECHNICAL SETUP ═══",
        f"Setup: {sig.get('setup', 'n/a')} | Regime: {sig.get('regime', 'n/a')} | Trend: {sig.get('trend_score', 'n/a')}/5 | ADX: {sig.get('adx', 'n/a')}",
        f"RS vs SPY: {fmt_pct(sig.get('rs'))} | RSI: {sig.get('rsi', 'n/a')} | Quality: {sig.get('quality', 'n/a')}",
        f"EMA20/50/200: {sig.get('ema20', 'n/a')} / {sig.get('ema50', 'n/a')} / {sig.get('ema200', 'n/a')}",
        f"Scanner timeframe: {sig.get('eta_days_low', 'n/a')}-{sig.get('eta_days_high', 'n/a')} trading days",
        f"Technical look-alike: {llm.get('technical_lookalike') or 'not available'}",
        llm.get("technical_interpretation") or "Technical interpretation unavailable.",
        "",
        "═══ LAST 3 FINANCIAL RESULTS ═══",
    ]
    if results:
        lines.extend(format_financial_lines(results))
        if llm.get("financial_interpretation"):
            lines += ["", f"🧠 {llm['financial_interpretation']}"]
    else:
        lines.append("- Not applicable / unavailable (common for ETFs or unavailable FMP statements).")
    lines += [
        "",
        "═══ NEXT FINANCIAL REPORT ═══",
    ]
    d = parse_dt(next_e.get("date") or next_e.get("earningsDate"))
    if d:
        days = (d.date() - dt.date.today()).days
        lines.append(f"Expected: {d.date().isoformat()} ({days:+d} days)")
        if next_e.get("epsEstimated") is not None:
            lines.append(f"EPS estimate: {next_e.get('epsEstimated')}")
        if next_e.get("revenueEstimated") is not None:
            lines.append(f"Revenue estimate: {fmt_money(next_e.get('revenueEstimated'))}")
    else:
        lines.append("Expected date: unavailable")
    if llm.get("earnings_interpretation"):
        lines.append(f"🧠 {llm['earnings_interpretation']}")

    lines += [
        "",
        "═══ ANALYST VIEW ═══",
        f"Assessment: {icon_assessment(llm.get('analyst_assessment'))} {llm.get('analyst_assessment') or 'unknown'}",
    ]
    g = analysts.get("grades_consensus") or {}
    for key in ("strongBuy", "strongBuyCount", "buy", "buyCount", "hold", "holdCount", "sell", "sellCount", "strongSell", "strongSellCount"):
        if key in g:
            lines.append(f"{key}: {g[key]}")
    if llm.get("analyst_interpretation"):
        lines.append(f"🧠 {llm['analyst_interpretation']}")

    lines += ["", "═══ RECENT NEWS ═══"]
    nd = row.get("news_diag") or {}
    if nd.get("status") == "FMP_ERROR":
        lines.append(f"⚠️ FMP news error: {nd.get('message')}")
    elif news_rows:
        for n in news_rows[:NEWS_LIMIT]:
            lines.append(f"- {n.get('date')} — {n.get('title')} [{n.get('site')}]" + (f"\n  {n.get('url')}" if n.get('url') else ""))
    else:
        lines.append("- No recent usable company-specific news returned by FMP.")
    lines.append(f"🧠 {icon_assessment(llm.get('news_assessment'))} {llm.get('news_interpretation') or 'News interpretation unavailable.'}")

    lines += [
        "",
        "═══ SECTOR / INDUSTRY ═══",
        f"Sector: {sector.get('sector', 'Unknown')} | Industry: {sector.get('industry', 'Unknown')}",
        f"Assessment: {icon_assessment(llm.get('sector_assessment'))} {llm.get('sector_assessment') or 'unknown'}",
        llm.get("sector_interpretation") or "Sector interpretation unavailable.",
        "",
        "═══ MACRO ═══",
        f"Assessment: {icon_assessment(llm.get('macro_assessment'))} {llm.get('macro_assessment') or 'unknown'}",
        llm.get("macro_interpretation") or "Macro interpretation unavailable.",
        "",
        "═══ OVERALL ═══",
        f"{icon_assessment(llm.get('overall_assessment'))} {str(llm.get('overall_assessment') or 'insufficient_data').upper()}",
        llm.get("overall_summary") or "Overall interpretation unavailable.",
        f"Key positive: {llm.get('key_positive') or 'n/a'}",
        f"Key risk: {llm.get('key_risk') or 'n/a'}",
        "",
        f"(Technical data: hourly-scan | fundamental/news/macro data: FMP | interpretation: {row.get('llm_provider', 'none')})",
    ]
    return "\n".join(lines)


def split_telegram(text, limit=3900):
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            while len(block) > limit:
                chunks.append(block[:limit])
                block = block[limit:]
            current = block
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Main collection

def main():
    if not NEWS_ENABLED:
        print("NEWS_ENABLED=0 — exiting")
        return
    if not FMP_KEY:
        print("FMP_KEY is missing")
        return

    explicit = [x.strip().upper() for x in sys.argv[1].split(",") if x.strip()] if sys.argv[1:] and sys.argv[1] else []
    if explicit:
        targets = [(t, {}) for t in explicit]
    else:
        targets = recent_alerts()
    if not targets:
        print(f"No alerts in the last {LOOKBACK_MIN} minutes")
        return

    signals = load_signals()
    tickers = [t for t, _ in targets]
    print(f"Signal intelligence for: {tickers}")

    # Shared calls — one earnings calendar, one sector snapshot, one industry
    # snapshot, and one macro collection for the whole report.
    print("Loading shared FMP context...")
    earnings_cal = earnings_calendar_all(tickers)
    today = dt.date.today().isoformat()
    sectors = sector_snapshot(today)
    industries = industry_snapshot(today)
    macro = macro_data()

    chosen_provider = provider()
    rows = []

    for ticker, alert in targets:
        print(f"\n=== {ticker} ===")
        sig = signals.get(ticker, {})
        prof = profile(ticker)
        asset_type = str(prof.get("type") or prof.get("exchangeShortName") or "").lower()
        is_etf = any(x in asset_type for x in ("etf", "fund")) or "etf" in str(prof.get("description") or "").lower() and not prof.get("companyName")
        if not is_etf:
            # FMP profile can expose isEtf directly on some responses.
            is_etf = bool(prof.get("isEtf"))

        earn = earnings_history(ticker, 8)
        inc = [] if is_etf else income_statements(ticker, 5)
        bal = {} if is_etf else balance_sheet(ticker)
        cf = {} if is_etf else cash_flow(ticker)
        estimates = analyst_estimates(ticker)
        grades = analyst_grades(ticker)
        news_rows, news_diag = news(ticker)

        fin, health = build_financials(ticker, earn, inc, bal, cf)
        forward = forward_data(earnings_cal.get(ticker, []), estimates)
        analysts = analyst_data(grades, estimates)
        sector = sector_data(prof, sectors, industries)

        prompt = PROMPT.format(
            ticker=ticker,
            name=prof.get("companyName") or sig.get("name") or ticker,
            trade_context=trade_text(sig, alert),
            technical=technical_text(sig),
            financials=financial_text(fin, health),
            forward=forward_text(forward),
            analysts=analysts_text(analysts),
            news=news_text(news_rows, news_diag),
            sector=sector_text(sector),
            macro=macro_text(macro),
        )

        llm_result = None
        llm_used = chosen_provider or "none"
        if chosen_provider:
            llm_result, llm_used = ask(prompt, chosen_provider)
        else:
            print("  No LLM key configured")

        row = {
            "ticker": ticker,
            "alert": alert,
            "signal": sig,
            "profile": prof,
            "financials": fin,
            "financial_health": health,
            "forward": forward,
            "analysts": analysts,
            "news": news_rows,
            "news_diag": news_diag,
            "sector": sector,
            "macro": macro,
            "llm": llm_result or {},
            "llm_provider": llm_used,
            "llm_ok": bool(llm_result),
            "is_etf": is_etf,
        }
        row["report"] = format_report(row)
        rows.append(row)

    when = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    subject = f"Signal intelligence — {', '.join(tickers)} — {when}"

    # Email can carry the complete multi-ticker report. Telegram is split to
    # stay below Telegram's message-size limit.
    full_report = "\n\n".join(row["report"] for row in rows)
    notify.send_email(subject, full_report, cfg)
    for chunk in split_telegram(full_report):
        notify.send_telegram(chunk, cfg)

    with open("news_log.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["time_utc", "ticker", "llm_provider", "llm_ok", "financial_count", "news_count", "news_status", "overall_assessment", "technical_lookalike"])
        for row in rows:
            llm = row.get("llm") or {}
            w.writerow([
                when, row["ticker"], row.get("llm_provider"), row.get("llm_ok"),
                len(row.get("financials") or []), len(row.get("news") or []),
                (row.get("news_diag") or {}).get("status"),
                llm.get("overall_assessment"), llm.get("technical_lookalike"),
            ])

    os.makedirs("docs", exist_ok=True)
    with open("docs/news.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated": when,
            "provider": chosen_provider or "none",
            "purpose": "signal intelligence follow-up",
            "source_files": ["alerted.json", "docs/signals.json"],
            "results": rows,
        }, f, indent=2, ensure_ascii=False, default=str)

    print(f"\nSaved rich signal intelligence for {len(rows)} ticker(s) to docs/news.json and news_log.csv")


if __name__ == "__main__":
    main()
