"""notify.py — Email + Telegram delivery. DRY_RUN=1 prints instead of sending."""
import json
import smtplib
import urllib.request
from email.mime.text import MIMEText


def format_alert(sig: dict, macro: dict, cyc: dict) -> str:
    lines = [
        f"🟢 BUY SIGNAL — {sig['ticker']}  ({sig['setup']})",
        f"Entry zone : ${sig['entry']:.2f}",
        f"Take profit: ${sig['tp']:.2f}  (+{sig['tp_pct']:.1%})",
        f"Suggested  : {sig['shares']} shares ≈ ${sig['value']:.0f}"
        f"  (fees {sig['fee_pct']:.1%} of expected profit)",
        f"Why: {', '.join(sig['reasons'])}",
        f"Daily trend score {sig['trend_score']}/5 | ADX {sig['adx']} | RS vs SPY {sig['rs']:+.1%}",
        f"Macro: {macro.get('risk')} (SPY 20d realized vol {macro.get('vol')}%)",
        cyc.get("line", ""),
        "No stoploss per your rules — thesis-broken alerts will monitor this position.",
    ]
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
