"""news_llm.py — News risk follow-up for recently alerted tickers.

This module is isolated from the trading scanner:
  - reads alerted.json
  - fetches recent stock news from FMP's current /stable endpoint
  - optionally sends the headlines to Anthropic, OpenAI, or both
  - writes news_log.csv and docs/news.json
  - never changes a trading signal

Environment:
  FMP_KEY                              FMP API key
  ANTHROPIC_API_KEY / OPENAI_API_KEY   one or both LLM keys
  LLM_PROVIDER                         anthropic | openai | both | auto
  LLM_MODEL                            optional common model override
  ANTHROPIC_MODEL / OPENAI_MODEL       optional provider-specific overrides
  NEWS_LOOKBACK_MIN                    default 90
  NEWS_MAX_TICKERS                     default 12
  NEWS_MAX_AGE_HOURS                   default 48
  NEWS_ENABLED                         0 disables the module
  DRY_RUN                              1 prints notifications instead of sending

Usage:
  python news_llm.py
  python news_llm.py AAPL,NVDA
"""

import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import config as cfg
import notify


LOOKBACK_MIN = int(os.getenv("NEWS_LOOKBACK_MIN", "90"))
MAX_TICKERS = int(os.getenv("NEWS_MAX_TICKERS", "12"))
HEADLINES_PER_TICKER = int(os.getenv("NEWS_HEADLINES_PER_TICKER", "8"))
MAX_NEWS_AGE_HOURS = int(os.getenv("NEWS_MAX_AGE_HOURS", "48"))

FMP_KEY = os.getenv("FMP_KEY", "").strip()
ENABLED = os.getenv("NEWS_ENABLED", "1") != "0"

FMP_NEWS_URL = "https://financialmodelingprep.com/stable/news/stock"
FMP_TIMEOUT = 25


PROMPT = """You are screening a stock for NEWS RISK only. Ignore chart or
valuation opinions — a separate technical system already decided to buy.

Ticker: {ticker}
Recent headlines (newest first):
{headlines}

Decide whether recent news contains a red flag that a swing trader holding
this for a few weeks should know about. Examples of red flags: guidance cut,
earnings miss, regulatory or legal action, accounting concerns, executive
departure, product failure, credit or liquidity trouble, merger collapse.
Routine analyst notes, price-target changes and general market commentary are
NOT red flags.

Reply with ONLY a JSON object, no markdown:
{{"risk": "none|elevated|severe",
  "company_specific": true|false,
  "summary": "one short sentence on what the news says",
  "reason": "one short sentence on why this is or is not a risk"}}"""


# ---------------------------------------------------------------------------
# Utility / diagnostics
def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_news_datetime(value):
    """Parse common FMP date formats into an aware UTC datetime."""
    if not value:
        return None

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    # ISO 8601 first.
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        pass

    # Common FMP legacy/stable timestamp format.
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = dt.datetime.strptime(text[:19], fmt)
            return parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue

    return None


def response_json(url, headers=None, timeout=FMP_TIMEOUT):
    """GET JSON and raise useful errors instead of hiding API failures."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Nwing-Bot/1.0",
            "Accept": "application/json",
            **(headers or {}),
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = raw[:700].replace("\n", " ")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("request timed out") from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"HTTP {status}: {raw[:700].replace(chr(10), ' ')}")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid JSON response: {raw[:500].replace(chr(10), ' ')}"
        ) from exc


# ---------------------------------------------------------------------------
# Tickers
def recent_alerts():
    try:
        with open("alerted.json", "r", encoding="utf-8") as f:
            ledger = json.load(f)
    except FileNotFoundError:
        print("alerted.json not found.")
        return []
    except Exception as exc:
        print(f"Could not read alerted.json: {exc}")
        return []

    now = utc_now()
    out = []

    for ticker, entry in ledger.items():
        try:
            timestamp = dt.datetime.fromisoformat(
                str(entry["alerted"]).replace("Z", "+00:00")
            )
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
            timestamp = timestamp.astimezone(dt.timezone.utc)
        except Exception:
            continue

        age_seconds = (now - timestamp).total_seconds()
        if 0 <= age_seconds <= LOOKBACK_MIN * 60:
            out.append((ticker.upper(), entry))

    out.sort(key=lambda x: x[1].get("alerted", ""), reverse=True)
    return out[:MAX_TICKERS]


# ---------------------------------------------------------------------------
# FMP news
def fetch_headlines(ticker):
    """Fetch recent FMP stock news.

    Returns:
        (headlines, diagnostics)

    diagnostics always contains enough information to distinguish:
      NO_NEWS        = valid API response but no usable articles
      FMP_ERROR      = API/network/response problem
      OK              = usable articles returned
    """
    diagnostics = {
        "status": "NO_NEWS",
        "http_error": None,
        "message": "",
        "source": "FMP /stable/news/stock",
    }

    if not FMP_KEY:
        diagnostics.update(
            status="FMP_ERROR",
            message="FMP_KEY is not set",
        )
        return [], diagnostics

    params = urllib.parse.urlencode(
        {
            "symbols": ticker.upper(),
            "limit": str(HEADLINES_PER_TICKER),
        }
    )
    url = f"{FMP_NEWS_URL}?{params}"

    try:
        data = response_json(
            url,
            headers={"apikey": FMP_KEY},
        )
    except RuntimeError as exc:
        message = str(exc)
        diagnostics.update(
            status="FMP_ERROR",
            message=message,
        )
        if message.startswith("HTTP "):
            diagnostics["http_error"] = message.split(":", 1)[0]
        print(f"  FMP ERROR [{ticker}]: {message}")
        return [], diagnostics

    if isinstance(data, dict):
        # FMP may return an error/message object instead of a list.
        message = (
            data.get("Error Message")
            or data.get("error")
            or data.get("message")
            or data.get("errorMessage")
        )
        if message:
            diagnostics.update(status="FMP_ERROR", message=str(message))
            print(f"  FMP ERROR [{ticker}]: {message}")
            return [], diagnostics

        # Be tolerant if an API response wraps the articles.
        data = data.get("data") or data.get("results") or data.get("articles")

    if not isinstance(data, list):
        diagnostics.update(
            status="FMP_ERROR",
            message=f"unexpected response type: {type(data).__name__}",
        )
        print(f"  FMP ERROR [{ticker}]: unexpected response shape")
        return [], diagnostics

    now = utc_now()
    headlines = []

    for article in data:
        if not isinstance(article, dict):
            continue

        title = str(article.get("title") or "").strip()
        if not title:
            continue

        published_raw = (
            article.get("publishedDate")
            or article.get("publishedAt")
            or article.get("date")
            or ""
        )
        published = parse_news_datetime(published_raw)

        # FMP can return older articles even when the endpoint is working.
        # Keep the latest window, but don't discard an article if its date
        # cannot be parsed.
        if published is not None:
            age_hours = (now - published).total_seconds() / 3600
            if age_hours < -2 or age_hours > MAX_NEWS_AGE_HOURS:
                continue

        site = str(article.get("site") or article.get("publisher") or "").strip()
        url_value = str(article.get("url") or "").strip()
        text = str(article.get("text") or article.get("content") or "").strip()

        date_display = (
            published.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if published
            else str(published_raw)[:32]
        )

        item = {
            "date": date_display,
            "title": title,
            "site": site,
            "url": url_value,
            "text": text[:1000],
        }
        headlines.append(item)

        if len(headlines) >= HEADLINES_PER_TICKER:
            break

    if headlines:
        diagnostics.update(
            status="OK",
            message=f"{len(headlines)} usable article(s)",
        )
    else:
        diagnostics.update(
            status="NO_NEWS",
            message="FMP responded successfully but returned no usable recent articles",
        )

    return headlines, diagnostics


def headline_text(headlines):
    """Format article data for the LLM without sending unnecessary fields."""
    lines = []
    for h in headlines:
        source = f" ({h['site']})" if h.get("site") else ""
        lines.append(
            f"- {h.get('date', '')} — {h.get('title', '')}{source}"
        )
        if h.get("text"):
            lines.append(f"  Context: {h['text'][:500]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM
def pick_provider():
    """anthropic | openai | both | auto."""
    wanted = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    has_openai = bool(os.getenv("OPENAI_API_KEY", "").strip())

    if wanted == "both":
        if has_anthropic and has_openai:
            return "both"
        print(
            "LLM_PROVIDER=both but both keys are not present; "
            "using the available provider."
        )
        return "anthropic" if has_anthropic else ("openai" if has_openai else None)

    if wanted == "anthropic" and has_anthropic:
        return "anthropic"
    if wanted == "openai" and has_openai:
        return "openai"

    if wanted in ("anthropic", "openai"):
        print(f"LLM_PROVIDER={wanted} but that key is missing; falling back.")

    if has_anthropic:
        return "anthropic"
    if has_openai:
        return "openai"
    return None


SEVERITY = {"none": 0, "elevated": 1, "severe": 2}


def ask_both(prompt):
    """Query both providers and keep the more severe valid verdict."""
    anthropic_verdict = parse_verdict(ask_llm("anthropic", prompt))
    openai_verdict = parse_verdict(ask_llm("openai", prompt))

    picks = [
        (v, name)
        for v, name in (
            (anthropic_verdict, "anthropic"),
            (openai_verdict, "openai"),
        )
        if v
    ]

    if not picks:
        return None, "both-failed"

    verdict, provider = max(
        picks,
        key=lambda item: SEVERITY.get(item[0].get("risk", "none"), 0),
    )

    if len(picks) == 2:
        a_risk = anthropic_verdict.get("risk")
        o_risk = openai_verdict.get("risk")
        if a_risk != o_risk:
            verdict = dict(verdict)
            verdict["reason"] = (
                f"{verdict.get('reason', '')} "
                f"[providers disagreed: anthropic={a_risk}, openai={o_risk}; "
                "kept the more severe]"
            ).strip()

    return verdict, provider


def post_json(url, payload, headers):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Nwing-Bot/1.0",
            **headers,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code}: {raw[:700].replace(chr(10), ' ')}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid JSON response: {raw[:500].replace(chr(10), ' ')}"
        ) from exc


def ask_llm(provider, prompt):
    """Returns raw model text, or None with a visible diagnostic."""
    try:
        common_model = os.getenv("LLM_MODEL", "").strip()

        if provider == "anthropic":
            model = (
                os.getenv("ANTHROPIC_MODEL", "").strip()
                or common_model
                or "claude-sonnet-4-6"
            )
            response = post_json(
                "https://api.anthropic.com/v1/messages",
                {
                    "model": model,
                    "max_tokens": 400,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                },
                {
                    "x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
                    "anthropic-version": "2023-06-01",
                },
            )
            return "".join(
                block.get("text", "")
                for block in response.get("content", [])
                if block.get("type") == "text"
            )

        model = (
            os.getenv("OPENAI_MODEL", "").strip()
            or common_model
            or "gpt-4o-mini"
        )
        response = post_json(
            "https://api.openai.com/v1/chat/completions",
            {
                "model": model,
                "temperature": 0,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            {
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}",
            },
        )
        return response["choices"][0]["message"]["content"]

    except Exception as exc:
        print(f"  {provider.upper()} LLM ERROR: {str(exc)[:700]}")
        return None


def parse_verdict(text):
    """Strict-ish JSON parse; return None rather than guessing."""
    if not text:
        return None

    cleaned = (
        text.strip()
        .replace("```json", "")
        .replace("```", "")
        .strip()
    )

    try:
        start = cleaned.index("{")
        end = cleaned.rindex("}") + 1
        verdict = json.loads(cleaned[start:end])
    except Exception:
        return None

    risk = str(verdict.get("risk", "")).lower()
    if risk not in ("none", "elevated", "severe"):
        return None

    return {
        "risk": risk,
        "company_specific": bool(verdict.get("company_specific", False)),
        "summary": str(verdict.get("summary", ""))[:300],
        "reason": str(verdict.get("reason", ""))[:300],
    }


# ---------------------------------------------------------------------------
# Output
ICON = {"none": "✅", "elevated": "⚠️", "severe": "⛔"}
LABEL = {
    "none": "no red flags",
    "elevated": "elevated risk",
    "severe": "SEVERE",
    "unknown": "no usable news/LLM result",
}


def format_followup(rows, when, provider):
    order = {"severe": 0, "elevated": 1, "none": 2, "unknown": 3}
    lines = [
        f"📰 News check — {len(rows)} signal(s) from the {when} UTC scan",
        "",
    ]

    for row in sorted(rows, key=lambda x: order.get(x["risk"], 9)):
        risk = row["risk"]
        icon = ICON.get(risk, "❔")
        label = LABEL.get(risk, "unknown")
        lines.append(f"{icon} {row['ticker']} — {label}")

        if row.get("summary"):
            lines.append(f"   {row['summary']}")

        if row.get("reason") and risk != "none":
            lines.append(f"   → {row['reason']}")

        diagnostics = row.get("diagnostics") or {}
        status = diagnostics.get("status")

        if status == "FMP_ERROR":
            lines.append(
                f"   ⚠️ FMP error: {diagnostics.get('message', 'unknown error')}"
            )
        elif status == "NO_NEWS":
            lines.append("   (FMP returned no recent usable headlines)")
        elif status == "OK" and not row.get("llm_ok"):
            lines.append("   ⚠️ Headlines found, but LLM returned no usable verdict.")

        if row.get("headlines"):
            lines.append(f"   {len(row['headlines'])} recent headline(s) retrieved.")

        lines.append("")

    lines += [
        "Informative only — this does not change or cancel any signal.",
        f"(news via FMP, judgement via {provider})",
    ]
    return "\n".join(lines)


def log_rows(rows, when):
    path = "news_log.csv"
    new_file = not os.path.exists(path)

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if new_file:
            writer.writerow(
                [
                    "scan_time_utc",
                    "ticker",
                    "entry",
                    "tp",
                    "risk",
                    "company_specific",
                    "summary",
                    "reason",
                    "n_headlines",
                    "fmp_status",
                    "fmp_message",
                    "llm_ok",
                ]
            )

        for row in rows:
            diagnostics = row.get("diagnostics") or {}
            writer.writerow(
                [
                    when,
                    row["ticker"],
                    row.get("entry"),
                    row.get("tp"),
                    row["risk"],
                    row.get("company_specific"),
                    row.get("summary"),
                    row.get("reason"),
                    len(row.get("headlines") or []),
                    diagnostics.get("status"),
                    diagnostics.get("message"),
                    row.get("llm_ok"),
                ]
            )


def main():
    if not ENABLED:
        print("NEWS_ENABLED=0 — news follow-up disabled, exiting.")
        return

    when = utc_now().strftime("%Y-%m-%d %H:%M")

    if len(sys.argv) > 1 and sys.argv[1].strip():
        targets = [
            (symbol.strip().upper(), {})
            for symbol in sys.argv[1].split(",")
            if symbol.strip()
        ]
        print(f"Explicit tickers: {[ticker for ticker, _ in targets]}")
    else:
        targets = recent_alerts()
        if not targets:
            print(
                f"No tickers alerted in the last {LOOKBACK_MIN} minutes — "
                "nothing to do."
            )
            return
        print(
            f"Tickers alerted recently: "
            f"{[ticker for ticker, _ in targets]}"
        )

    if not FMP_KEY:
        print("FMP_KEY is missing. Cannot fetch news.")
        return

    provider = pick_provider()
    if not provider:
        print(
            "No ANTHROPIC_API_KEY or OPENAI_API_KEY set. "
            "News retrieval will be tested, but LLM analysis cannot run."
        )
        # Continue anyway so manual runs can validate FMP.

    print(f"FMP endpoint: {FMP_NEWS_URL}")
    print(f"FMP news limit per ticker: {HEADLINES_PER_TICKER}")
    print(f"FMP news age window: {MAX_NEWS_AGE_HOURS} hours")
    print(f"LLM provider: {provider or 'none'}")

    rows = []

    for ticker, entry in targets:
        print(f"\n{ticker}:")
        headlines, diagnostics = fetch_headlines(ticker)
        print(
            f"  FMP status: {diagnostics['status']} — "
            f"{diagnostics.get('message', '')}"
        )
        print(f"  {len(headlines)} headline(s)")

        row = {
            "ticker": ticker,
            "entry": entry.get("entry"),
            "tp": entry.get("tp"),
            "headlines": headlines,
            "risk": "unknown",
            "company_specific": None,
            "summary": "",
            "reason": "",
            "diagnostics": diagnostics,
            "llm_ok": False,
        }

        if headlines and provider:
            prompt = PROMPT.format(
                ticker=ticker,
                headlines=headline_text(headlines),
            )

            if provider == "both":
                verdict, used_provider = ask_both(prompt)
            else:
                verdict = parse_verdict(ask_llm(provider, prompt))
                used_provider = provider

            if verdict:
                row.update(verdict)
                row["llm_ok"] = True
                row["llm_provider"] = used_provider
                print(
                    f"  verdict: {verdict['risk']} — "
                    f"{verdict['summary'][:120]}"
                )
            else:
                row["llm_provider"] = used_provider
                print("  no usable LLM verdict.")
        elif headlines and not provider:
            print("  headlines retrieved; no LLM key configured.")
        elif diagnostics["status"] == "FMP_ERROR":
            print("  skipping LLM because FMP failed.")

        rows.append(row)

    note = format_followup(rows, when, provider or "no LLM")
    print("\n" + note)

    flagged = sum(
        1 for row in rows if row["risk"] in ("elevated", "severe")
    )
    fmp_errors = sum(
        1
        for row in rows
        if (row.get("diagnostics") or {}).get("status") == "FMP_ERROR"
    )

    subject = (
        f"News check — {len(rows)} signal(s)"
        + (f", {flagged} flagged" if flagged else ", no red flags")
        + (f", {fmp_errors} FMP error(s)" if fmp_errors else "")
        + f" ({when} UTC)"
    )

    notify.send_email(subject, note, cfg)
    notify.send_telegram(note, cfg)

    log_rows(rows, when)

    os.makedirs("docs", exist_ok=True)
    with open("docs/news.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated": when,
                "provider": provider or "none",
                "fmp_endpoint": FMP_NEWS_URL,
                "results": rows,
            },
            f,
            indent=2,
            default=str,
        )

    print("\nSaved news_log.csv and docs/news.json")


if __name__ == "__main__":
    main()
