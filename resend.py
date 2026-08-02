"""resend.py — Re-send the CURRENT signals by email/Telegram, no rescan.

Reads docs/signals.json (written by the last scan) and re-delivers it using
the current notify.py formatting. Costs ZERO API credits — useful for:
  - testing email formatting changes
  - re-sending alerts you deleted or missed
  - checking the layout after editing tickers.csv names

Company names are re-attached from tickers.csv at send time, so name fixes
show up immediately without waiting for a nightly + scan.

Usage: python resend.py            (send)
       DRY_RUN=1 python resend.py  (print only)
"""
import json

import config as cfg
import notify
import universe


def main():
    try:
        with open(cfg.SIGNALS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"No {cfg.SIGNALS_FILE} yet — run a scan first.")
        return

    signals = data.get("signals", [])
    macro = data.get("macro", {})
    cyc = data.get("cycles", {})
    alerts = data.get("position_alerts", [])

    # refresh names/sectors from tickers.csv (no API calls)
    try:
        _, meta = universe.load()
    except Exception:
        meta = {}

    print(f"Signals file generated: {data.get('generated')}")
    print(f"Re-sending {len(signals)} signal(s) and {len(alerts)} position alert(s)\n")

    for s in signals:
        m = meta.get(s.get("ticker", "").upper(), {})
        if m.get("name"):
            s["name"] = m["name"]
        s.setdefault("pl_amount", round(
            (s.get("tp", 0) - s.get("entry", 0)) * s.get("shares", 0), 2))
        body = notify.format_alert(s, macro, cyc)
        nm = f" ({s['name']})" if s.get("name") else ""
        subject = (f"[RESEND] BUY {s['ticker']}{nm} — {s.get('setup','')} "
                   f"+{s.get('tp_pct',0):.0%} | eToro TP P/L ${s['pl_amount']:.0f}")
        notify.send_email(subject, body, cfg)
        notify.send_telegram(body, cfg)

    for m_ in alerts:
        notify.send_email("[RESEND] Position alert", m_, cfg)
        notify.send_telegram(m_, cfg)

    if not signals and not alerts:
        notify.send_email("[RESEND] No current signals",
                          "The latest scan produced no signals and no position "
                          "alerts.\nThis is a formatting test message.", cfg)
        print("Nothing to resend — sent a test message instead.")

    print("Done.")


if __name__ == "__main__":
    main()
