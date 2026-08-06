"""alerts_ledger.py — remembers which tickers were already alerted.

Rule (per user): once a ticker is alerted with a target, it is MUTED until
price actually exceeds that target. When price closes above the old TP, the
entry is cleared and the ticker may alert again — with a fresh, higher target.

Stored in alerted.json, committed by the workflow so it survives across runs:
  { "MU": {"entry": 100.0, "tp": 150.0, "alerted": "2026-08-04T12:00:00Z",
            "scans_muted": 7} }

Also expires after MAX_AGE_DAYS so a dead signal cannot mute a ticker forever.
"""
import datetime as dt
import json

FILE = "alerted.json"
MAX_AGE_DAYS = 90


def load(path=FILE):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save(ledger, path=FILE):
    try:
        with open(path, "w") as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception:
        pass


def _age_days(iso):
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - t).days
    except Exception:
        return 0


def is_muted(ledger, ticker, price):
    """True if this ticker was already alerted and price has NOT yet cleared
    the recorded take-profit."""
    e = ledger.get(ticker.upper())
    if not e:
        return False
    if _age_days(e.get("alerted")) > MAX_AGE_DAYS:
        return False                       # stale entry -> allow again
    try:
        return float(price) <= float(e["tp"])
    except Exception:
        return False


def clear_if_cleared(ledger, ticker, price):
    """Remove the mute once price exceeds the recorded target.
    Returns the old entry if it was cleared, else None."""
    t = ticker.upper()
    e = ledger.get(t)
    if not e:
        return None
    try:
        if float(price) > float(e["tp"]) or _age_days(e.get("alerted")) > MAX_AGE_DAYS:
            return ledger.pop(t)
    except Exception:
        pass
    return None


def record(ledger, ticker, entry, tp):
    ledger[ticker.upper()] = {
        "entry": round(float(entry), 4),
        "tp": round(float(tp), 4),
        "alerted": dt.datetime.now(dt.timezone.utc).isoformat(),
        "thesis_warned": False,
    }


def mark_thesis_warned(ledger, ticker):
    e = ledger.get(ticker.upper())
    if e:
        e["thesis_warned"] = True
        e["thesis_warned_on"] = dt.datetime.now(dt.timezone.utc).isoformat()


def tracked(ledger):
    """Every ticker the bot has alerted and is still following."""
    return sorted(ledger.keys())


def bump_muted(ledger, ticker):
    e = ledger.get(ticker.upper())
    if e:
        e["scans_muted"] = int(e.get("scans_muted", 0)) + 1
