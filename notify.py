"""notify.py — Email + Telegram delivery. DRY_RUN=1 prints instead of sending."""
import json
import smtplib
import urllib.request
from email.mime.text import MIMEText


def format_alert(sig: dict, macro: dict, cyc: dict) -> str:
    name = f" — {sig['name']}" if sig.get("name") else ""
    sector = f"  [{sig['sector']}]" if sig.get("sector") else ""
    qd = sig.get("q_detail", {})
    lines = [
        f"🟢 BUY SIGNAL — {sig['ticker']}{name}{sector}",
        "",
        "═══ TRADE PLAN ═══",
        f"Setup       : {sig['setup']}",
        f"Entry zone  : ${sig['entry']:.2f}",
        f"Take profit : ${sig['tp']:.2f}  (+{sig['tp_pct']:.1%})",
        f"Suggested   : {sig['shares']} shares ≈ ${sig['value']:.0f}",
        f"eToro TP    : set Take Profit as P/L amount = ${sig.get('pl_amount', 0):.2f}",
        f"Fees        : {sig['fee_pct']:.1%} of expected profit",
        "",
        "═══ TECHNICAL ANALYSIS ═══",
        f"Daily trend : {sig['trend_score']}/5" + ("  ⭐ RUNNER (near 52w high, beating SPY)" if sig.get("runner") else ""),
        f"ADX         : {sig['adx']} (trend strength)",
        f"RS vs SPY   : {sig['rs']:+.1%}",
        f"Quality     : {sig.get('quality', '')} — rsi {qd.get('rsi', '?')}, bollinger {qd.get('bollinger', '?')}, "
        f"vwap {qd.get('vwap', '?')}, volume {qd.get('volume', '?')}, options {qd.get('options', '?')}",
        f"RSI (4h)    : {sig.get('rsi', '?')}",
        "",
        "═══ FUNDAMENTAL & MACRO ═══",
        f"Macro       : {macro.get('risk')} (SPY 20d realized vol {macro.get('vol')}%)",
        f"Cycles      : {cyc.get('line', 'n/a').replace('Cycles ', '')}",
    ]
    if sig.get("options_note") and sig["options_note"] != "options: n/a":
        lines.append(f"Options     : {sig['options_note']}")
    if sig.get("warnings"):
        lines += ["", "═══ WARNINGS ═══"] + [f"• {w}" for w in sig["warnings"]]
    lines += ["", "No stoploss per your rules — thesis-broken alerts will monitor this position."]
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
