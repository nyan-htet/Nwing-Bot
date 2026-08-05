"""notify.py — Email + Telegram delivery. DRY_RUN=1 prints instead of sending."""
import json
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
        f"Shares        : {shares}  (≈ ${shares * entry:,.0f})",
        f"eToro TP      : set Take Profit P/L amount = ${sig.get('pl_amount', 0):.2f}",
        f"Fees          : {sig['fee_pct']:.1%} of expected profit",
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
                     when: str) -> str:
    """Compact status note for scans that produced no new signals."""
    def block(key, label):
        v = sorted(skip_report.get(key) or [])
        return f"{label} ({len(v)}): {', '.join(v)}" if v else None

    lines = [f"Run completed {when} UTC",
             f"{watch_n} tickers scanned from the nightly watchlist",
             "Result: no new signals", ""]
    for key, label in (("already_alerted", "Alerted earlier, still below target"),
                       ("cooldown", "Entered/skipped recently"),
                       ("earnings", "Blocked - earnings within 7 days"),
                       ("screen", "Blocked - failed fundamentals screen"),
                       ("target_cleared", "Cleared old target, now eligible")):
        line = block(key, label)
        if line:
            lines.append(line)
    lines += ["", f"Macro: {macro.get('risk')} (SPY 20d realized vol {macro.get('vol')}%)"]
    if cyc.get("line"):
        lines.append(cyc["line"])
    return "\n".join(lines)


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
