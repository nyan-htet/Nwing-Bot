"""notify.py — Email + Telegram delivery. DRY_RUN=1 prints instead of sending."""
import json
import re
import smtplib
import urllib.request
from email.mime.text import MIMEText


def _fmt(v):
    """Score with a visual cue."""
    if v is None:
        return "  ?  "
    mark = "✓" if v >= 0.5 else ("·" if v > -0.2 else "✗")
    return f"{mark} {v:+.1f}"


def format_alert(sig: dict, macro: dict, cyc: dict) -> str:
    name = sig.get("name") or ""
    sector = sig.get("sector") or ""
    header = f"🟢 BUY — {sig['ticker']}"
    if name:
        header += f" — {name}"
    if sector and sector != "Unknown":
        header += f" — {sector}"

    qd = sig.get("q_detail", {})
    qn = sig.get("q_notes", {})
    shares, entry = sig.get("shares", 0), sig.get("entry", 0)

    lines = [
        header,
        "",
        "═══ TRADE PLAN ═══",
        f"Entry price   : ${entry:.2f}",
        f"Exit price    : ${sig['tp']:.2f}   (+{sig['tp_pct']:.1%})",
        f"Shares        : {shares:g}  (≈ ${shares * entry:,.0f})",
        "-------",
        (f"Time frame    : {sig['eta_days_low']}-{sig['eta_days_high']} trading days "
         "(informative, from ATR and moving-average pace only)"
         + ("  [wide range: volatile but slow-advancing]"
            if sig.get("eta_days_high", 0) >= 3 * sig.get("eta_days_low", 1) else "")
         if sig.get("eta_days_low") else
         "Time frame    : not estimable for this setup"),
        "-------",
        f"eToro TP      : ${sig.get('pl_amount', 0):.2f}",
        f"eToro Fees    : {sig['fee_pct']:.1%} of profit",
        f"Strategy type : {sig.get('setup', '')}",
        "",
        "═══ TECHNICAL ANALYSIS ═══",
        f"Daily regime          : {str(sig.get('regime','?')).upper()} for ~{sig.get('regime_weeks','?')} weeks"
        + (" — price ABOVE 200 EMA (long-term bullish)" if sig.get("above_ema200")
           else " — price BELOW 200 EMA (long-term caution)" if sig.get("above_ema200") is False else ""),
        f"Trend score           : {sig['trend_score']}/5"
        + ("  ⭐ RUNNER (near 52-week high + outperforming SPY)" if sig.get("runner") else ""),
        f"Moving averages       : 20 EMA ${sig.get('ema20','?')} | 50 EMA ${sig.get('ema50','?')} | 200 EMA ${sig.get('ema200','?')}",
        f"ADX (Average Directional Index, trend strength) : {sig['adx']}",
        f"Relative Strength (RS) vs SPY, 3-month          : {sig['rs']:+.1%}",
        f"RSI (Relative Strength Index, 4-hour)           : {sig.get('rsi', '?')}"
        + ("  [40-60 = healthy pullback zone]" if isinstance(sig.get('rsi'), (int, float)) and 40 <= sig['rsi'] <= 60 else ""),
        f"Quality score         : {sig.get('quality', '')}  (max 1.0; stocks need 0.70, ETFs 0.50)",
        f"  • RSI momentum        {_fmt(qd.get('rsi'))} — {qn.get('rsi', '')}",
        f"  • Bollinger Bands     {_fmt(qd.get('bollinger'))} — {qn.get('bollinger', '')}",
        f"  • VWAP                {_fmt(qd.get('vwap'))} — {qn.get('vwap', '')}",
        f"  • Volume              {_fmt(qd.get('volume'))} — {qn.get('volume', '')}",
        f"  • Extension from EMA  {_fmt(qd.get('extension'))} — {qn.get('extension', '')}",
        "",
        "═══ FUNDAMENTAL & MACRO ═══",
        f"Macro       : {macro.get('risk')} (SPY 20d realized vol {macro.get('vol')}%)",
        f"Cycles      : {cyc.get('line', 'n/a').replace('Cycles ', '')}",
    ]
    if sig.get("warnings"):
        lines += ["", "═══ WARNINGS ═══"] + [f"• {w}" for w in sig["warnings"]]
    lines += ["", "No stoploss per your rules — thesis-broken alerts will monitor this position."]
    return "\n".join(lines)


def format_no_signal(watch_n: int, skip_report: dict, macro: dict, cyc: dict,
                     when: str, updates: list | None = None) -> str:
    """Compact status note for scans that produced no NEW signals.

    Position updates (target reached / thesis broken) are appended here so a
    quiet scan still delivers them in one message instead of separate ones.
    """
    lines = ["Result: NO NEW SIGNAL 🚦",
             f"Run completed {when} UTC",
             f"{watch_n} tickers scanned from the nightly watchlist",
             ""]
    if updates:
        lines.append("═══ UPDATES ON TRACKED SIGNALS ═══")
        lines += [f"• {u}" for u in updates]
        lines.append("")
    for key, label in (("already_alerted", "Still Active"),
                       ("earnings", "Earnings within 7 days"),
                       ("screen", "Fundamental / quality filter failed"),
                       ("target_cleared", "Cleared target, eligible again")):
        v = sorted(skip_report.get(key) or [])
        if v:
            lines.append(f"{label} ({len(v)}): {', '.join(v)}")
    return "\n".join(lines).rstrip()


def _market_cap_millions(reason: str):
    """Extract a displayed market-cap figure from an FMP reason, if present."""
    m = re.search(r"market cap\s+\$?([\d.]+)\s*M", str(reason or ""), re.I)
    return float(m.group(1)) if m else None


def _canonical_screen_reason(reason: str) -> str:
    """Collapse similar FMP rejection messages into readable groups."""
    s = str(reason or "").strip().lower()
    if "net profit margin" in s or "unprofitable" in s:
        return "Profitability — net margin below required level"
    if "debt/equity" in s:
        return "Debt — Debt/Equity above allowed level"
    if "liquidity" in s or "dollar volume" in s:
        return "Liquidity — insufficient trading liquidity"
    if "market cap" in s or "microcap" in s or "quality floor" in s:
        return "Market-cap / quality requirement"
    if "profile unavailable" in s or "screen unavailable" in s:
        return "FMP data unavailable"
    return "Other quality requirement"


def _group_screen_failures(screen_failed, details):
    """Hide < $100M microcaps and group remaining failures by reason."""
    groups = {}
    hidden_microcaps = 0
    for ticker in sorted(set(screen_failed or [])):
        reasons = details.get(ticker) or ["Quality filter failed"]
        caps = [c for c in (_market_cap_millions(r) for r in reasons) if c is not None]
        if (caps and max(caps) < 100) or any("microcap" in str(r).lower() for r in reasons):
            hidden_microcaps += 1
            continue
        group = _canonical_screen_reason(reasons[0])
        groups.setdefault(group, []).append(ticker)
    return groups, hidden_microcaps


def format_nightly(info: dict) -> str:
    """Readable nightly summary with clear phone-friendly sections."""
    L = [
        "🌙 NIGHTLY WATCHLIST",
        f"🕒 Completed {info['when']} UTC",
        f"🔎 {info['watchlist_n']} tickers selected from {info['csv_n']} in tickers.csv",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📌 CURRENT STATUS",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if info.get("tracked"):
        t = sorted(info["tracked"])
        L += [f"🟢 Still Active ({len(t)})", _format_ticker_lines(t), ""]
    if info.get("earnings"):
        e = sorted(info["earnings"])
        L += [f"🟡 Earnings within 7 days ({len(e)})", _format_ticker_lines(e), ""]

    failed = info.get("screen_failed") or []
    if failed:
        details = info.get("screen_failed_details") or {}
        groups, hidden_microcaps = _group_screen_failures(failed, details)
        if groups:
            L += ["🔴 FUNDAMENTAL / QUALITY FILTERS",
                  f"{len(failed)-hidden_microcaps} shown (microcaps < $100M hidden)"]
            for reason in sorted(groups):
                L.append(f"• {reason} ({len(groups[reason])})")
                L.append(_format_ticker_lines(groups[reason], per_line=12))
            L.append("")

    if info.get("new_entrants"):
        ne = sorted(info["new_entrants"])
        L += ["━━━━━━━━━━━━━━━━━━━━", f"🆕 NEW TO WATCHLIST ({len(ne)})",
              "━━━━━━━━━━━━━━━━━━━━",
              _format_ticker_lines(ne[:50], per_line=10) + (" …" if len(ne)>50 else ""), ""]
    if info.get("dropped"):
        dr = sorted(info["dropped"])
        L += [f"⚪ DROPPED OUT ({len(dr)})",
              _format_ticker_lines(dr[:50], per_line=10) + (" …" if len(dr)>50 else ""), ""]
    if info.get("top"):
        L += ["━━━━━━━━━━━━━━━━━━━━", "🔥 STRONGEST TRENDS", "━━━━━━━━━━━━━━━━━━━━"]
        for t, sc, rs in info["top"][:10]:
            L.append(f"• {t}  |  Trend {sc}/5  |  RS vs SPY {rs:+.0%}")
        L.append("")
    if info.get("thesis_warned"):
        tw = sorted(info["thesis_warned"])
        L += ["⚠️ THESIS-BROKEN TRACKING", f"{len(tw)}: {_format_ticker_lines(tw)}", ""]
    problems = []
    if info.get("no_data"):
        problems.append(f"Twelve Data missing ({len(info['no_data'])}): {', '.join(sorted(info['no_data'])[:20])}" + (" …" if len(info["no_data"])>20 else ""))
    if info.get("short_history"):
        problems.append(f"Insufficient history ({len(info['short_history'])}): {', '.join(sorted(info['short_history'])[:20])}" + (" …" if len(info["short_history"])>20 else ""))
    if problems:
        L += ["⚠️ DATA NOTES"] + [f"• {p}" for p in problems] + [""]
    return "\n".join(L).rstrip()


def send_email(subject: str, body: str, cfg):
    if cfg.DRY_RUN or not cfg.SMTP_HOST:
        print(f"[DRY-RUN email] {subject}\n{body}\n")
        return
    msg = MIMEText(body)
    msg["Subject"], msg["From"], msg["To"] = subject, cfg.SMTP_USER, cfg.EMAIL_TO
    with smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT) as s:
        s.starttls()
        s.login(cfg.SMTP_USER, cfg.SMTP_PASS)
        s.send_message(msg)


def send_telegram(body: str, cfg):
    if cfg.DRY_RUN or not cfg.TELEGRAM_TOKEN:
        print(f"[DRY-RUN telegram]\n{body}\n")
        return
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": cfg.TELEGRAM_CHAT_ID, "text": body}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)
