"""news_llm.py — OPTIONAL news red-flag check on tickers that just alerted.

FULLY ISOLATED from the trading system:
  * imports nothing from scan.py / analysis.py — only config + notify helpers
  * writes only news_log.csv and docs/news.json
  * never changes, blocks or re-sends a trading signal
  * disable the news-followup workflow and nothing else notices

How it picks tickers: reads alerted.json and keeps entries whose "alerted"
timestamp is inside LOOKBACK_MIN minutes — i.e. the ones the scan that just
finished has sent. No handoff from scan.py required.

Provider: set LLM_PROVIDER to anthropic | openai | auto (default auto = use
whichever key is present, preferring Anthropic).

Environment:
  ANTHROPIC_API_KEY / OPENAI_API_KEY   (one or both)
  LLM_PROVIDER, LLM_MODEL              (optional overrides)
  FMP_KEY                              (news source)
  NEWS_ENABLED=0                       (kill switch, no workflow edit needed)
  DRY_RUN=1                            (print instead of sending)

Usage:
  python news_llm.py                # tickers alerted in the last 90 minutes
  python news_llm.py NVDA,UPS       # explicit tickers (testing)
"""
import csv
import datetime as dt
import json
import os
import sys
import urllib.request

import config as cfg
import notify

LOOKBACK_MIN = int(os.getenv("NEWS_LOOKBACK_MIN", "90"))
MAX_TICKERS = int(os.getenv("NEWS_MAX_TICKERS", "12"))
HEADLINES_PER_TICKER = 8
FMP_KEY = os.getenv("FMP_KEY", "")
ENABLED = os.getenv("NEWS_ENABLED", "1") != "0"

PROMPT = """You are screening a stock for NEWS RISK only. Ignore chart or
valuation opinions — a separate technical system already decided to buy.

Ticker: {ticker}{name}
Recent headlines (newest first):
{headlines}

Decide whether recent news contains a red flag that a swing trader holding
this for a few weeks should know about. Examples of red flags: guidance cut,
earnings miss, regulatory or legal action, accounting concerns, executive
departure, product failure, credit or liquidity trouble, merger collapse.
Routine analyst notes, price-target changes and general market commentary are
NOT red flags.

Reply with ONLY a JSON object, no other text:
{{"risk": "none|elevated|severe",
  "company_specific": true|false,
  "summary": "one short sentence on what the news says",
  "reason": "one short sentence on why this is or is not a risk"}}"""


# ----------------------------------------------------------------- tickers
def recent_alerts():
    try:
        ledger = json.load(open("alerted.json"))
    except Exception:
        return []
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for t, e in ledger.items():
        try:
            ts = dt.datetime.fromisoformat(str(e["alerted"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if (now - ts).total_seconds() <= LOOKBACK_MIN * 60:
            out.append((t, e))
    out.sort(key=lambda x: x[1].get("alerted", ""), reverse=True)
    return out[:MAX_TICKERS]


# -------------------------------------------------------------------- news
def fetch_headlines(ticker):
    """Recent headlines from FMP. Returns a list of 'date — title' strings."""
    if not FMP_KEY:
        return []
    urls = [
        f"https://financialmodelingprep.com/stable/news/stock?symbols={ticker}"
        f"&limit={HEADLINES_PER_TICKER}&apikey={FMP_KEY}",
        f"https://financialmodelingprep.com/api/v3/stock_news?tickers={ticker}"
        f"&limit={HEADLINES_PER_TICKER}&apikey={FMP_KEY}",
    ]
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=20).read())
            if isinstance(data, list) and data:
                out = []
                for a in data[:HEADLINES_PER_TICKER]:
                    d = str(a.get("publishedDate") or a.get("date") or "")[:10]
                    title = (a.get("title") or "").strip()
                    site = a.get("site") or a.get("publisher") or ""
                    if title:
                        out.append(f"{d} — {title}" + (f" ({site})" if site else ""))
                return out
        except Exception:
            continue
    return []


# --------------------------------------------------------------------- LLM
def pick_provider():
    """anthropic | openai | both | auto  (auto = whichever key exists).
    'both' queries each and keeps the MORE SEVERE verdict."""
    want = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    has_a = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_o = bool(os.getenv("OPENAI_API_KEY"))
    if want == "both":
        if has_a and has_o:
            return "both"
        print("LLM_PROVIDER=both but only one key present — using that one")
        return "anthropic" if has_a else ("openai" if has_o else None)
    if want == "anthropic" and has_a:
        return "anthropic"
    if want == "openai" and has_o:
        return "openai"
    if want in ("anthropic", "openai"):
        print(f"LLM_PROVIDER={want} but its key is missing — falling back")
    if has_a:
        return "anthropic"
    if has_o:
        return "openai"
    return None


SEVERITY = {"none": 0, "elevated": 1, "severe": 2}


def ask_both(prompt):
    """Query both providers; return (verdict, label). Keeps the worse verdict
    and notes any disagreement, so one over-optimistic model can't hide a flag."""
    a = parse_verdict(ask_llm("anthropic", prompt))
    o = parse_verdict(ask_llm("openai", prompt))
    picks = [(v, n) for v, n in ((a, "anthropic"), (o, "openai")) if v]
    if not picks:
        return None, "both-failed"
    v, n = max(picks, key=lambda p: SEVERITY.get(p[0].get("risk", "none"), 0))
    if len(picks) == 2 and picks[0][0].get("risk") != picks[1][0].get("risk"):
        v = dict(v)
        v["reason"] = (f"{v.get('reason','')} [providers disagreed: "
                       f"anthropic={a.get('risk')}, openai={o.get('risk')}; "
                       f"kept the more severe]")
        return v, "both"
    return v, n


def _post(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json", **headers})
    return json.loads(urllib.request.urlopen(req, timeout=45).read())


def ask_llm(provider, prompt):
    """Returns raw model text, or None on any failure."""
    try:
        if provider == "anthropic":
            model = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
            r = _post("https://api.anthropic.com/v1/messages",
                      {"model": model, "max_tokens": 400, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]},
                      {"x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
                       "anthropic-version": "2023-06-01"})
            return "".join(b.get("text", "") for b in r.get("content", [])
                           if b.get("type") == "text")
        model = os.getenv("LLM_MODEL", "gpt-4o-mini")
        r = _post("https://api.openai.com/v1/chat/completions",
                  {"model": model, "temperature": 0, "max_tokens": 400,
                   "messages": [{"role": "user", "content": prompt}]},
                  {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}"})
        return r["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  LLM call failed: {str(e)[:120]}")
        return None


def parse_verdict(text):
    """Strict-ish JSON parse; returns None rather than guessing."""
    if not text:
        return None
    t = text.strip().replace("```json", "").replace("```", "").strip()
    try:
        i, j = t.index("{"), t.rindex("}") + 1
        v = json.loads(t[i:j])
    except Exception:
        return None
    risk = str(v.get("risk", "")).lower()
    if risk not in ("none", "elevated", "severe"):
        return None
    return {"risk": risk,
            "company_specific": bool(v.get("company_specific", False)),
            "summary": str(v.get("summary", ""))[:300],
            "reason": str(v.get("reason", ""))[:300]}


# ------------------------------------------------------------------ output
ICON = {"none": "✅", "elevated": "⚠️", "severe": "⛔"}
LABEL = {"none": "no red flags", "elevated": "elevated risk", "severe": "SEVERE"}


def format_followup(rows, when, provider):
    n = len(rows)
    L = [f"📰 News check — {n} signal{'s' if n != 1 else ''} from the "
         f"{when} UTC scan", ""]
    order = {"severe": 0, "elevated": 1, "none": 2, "unknown": 3}
    for r in sorted(rows, key=lambda x: order.get(x["risk"], 9)):
        icon = ICON.get(r["risk"], "❔")
        lab = LABEL.get(r["risk"], "no news data")
        head = f"{icon} {r['ticker']} — {lab}"
        if r["risk"] in ("elevated", "severe") and not r.get("company_specific", True):
            head += " (sector/market-wide, not company-specific)"
        L.append(head)
        if r.get("summary"):
            L.append(f"   {r['summary']}")
        if r.get("reason") and r["risk"] != "none":
            L.append(f"   → {r['reason']}")
        if not r.get("headlines"):
            L.append("   (no recent headlines found)")
        L.append("")
    L += ["Informative only — this does not change or cancel any signal.",
          f"(news via FMP, judgement via {provider})"]
    return "\n".join(L)


def log_rows(rows, when):
    path = "news_log.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["scan_time_utc", "ticker", "entry", "tp", "risk",
                        "company_specific", "summary", "reason", "n_headlines"])
        for r in rows:
            w.writerow([when, r["ticker"], r.get("entry"), r.get("tp"), r["risk"],
                        r.get("company_specific"), r.get("summary"),
                        r.get("reason"), len(r.get("headlines") or [])])


def main():
    if not ENABLED:
        print("NEWS_ENABLED=0 — news follow-up disabled, exiting.")
        return

    when = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")

    if len(sys.argv) > 1 and sys.argv[1].strip():
        targets = [(s.strip().upper(), {}) for s in sys.argv[1].split(",") if s.strip()]
        print(f"Explicit tickers: {[t for t, _ in targets]}")
    else:
        targets = recent_alerts()
        if not targets:
            print(f"No tickers alerted in the last {LOOKBACK_MIN} minutes — nothing to do.")
            return
        print(f"Tickers alerted recently: {[t for t, _ in targets]}")

    provider = pick_provider()
    if not provider:
        print("No ANTHROPIC_API_KEY or OPENAI_API_KEY set — exiting quietly.")
        return
    print(f"Using provider: {provider}")

    rows = []
    for tkr, e in targets:
        print(f"\n{tkr}:")
        heads = fetch_headlines(tkr)
        print(f"  {len(heads)} headline(s)")
        row = {"ticker": tkr, "entry": e.get("entry"), "tp": e.get("tp"),
               "headlines": heads, "risk": "unknown",
               "company_specific": None, "summary": "", "reason": ""}
        if heads:
            prompt = PROMPT.format(ticker=tkr, name="",
                                   headlines="\n".join(f"- {h}" for h in heads))
            if provider == "both":
                verdict, used = ask_both(prompt)
            else:
                verdict, used = parse_verdict(ask_llm(provider, prompt)), provider
            if verdict:
                row.update(verdict)
                print(f"  verdict: {verdict['risk']} — {verdict['summary'][:80]}")
            else:
                print("  no usable verdict (skipped)")
        rows.append(row)

    note = format_followup(rows, when, provider)
    print("\n" + note)
    flagged = sum(1 for r in rows if r["risk"] in ("elevated", "severe"))
    subj = (f"News check — {len(rows)} signal(s)"
            + (f", {flagged} flagged" if flagged else ", all clear")
            + f" ({when} UTC)")
    notify.send_email(subj, note, cfg)
    notify.send_telegram(note, cfg)

    log_rows(rows, when)
    os.makedirs("docs", exist_ok=True)
    with open("docs/news.json", "w") as f:
        json.dump({"generated": when, "provider": provider, "results": rows},
                  f, indent=2, default=str)
    print(f"\nSaved news_log.csv and docs/news.json")


if __name__ == "__main__":
    main()
