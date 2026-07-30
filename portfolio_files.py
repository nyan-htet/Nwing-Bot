"""portfolio_files.py — Reads your two control files.

overrides.yaml — include/exclude rules from the portal:
  rules:
    - action: exclude          # or include (force into watchlist)
      match: TSLA              # ticker, sector, or industry name
      type: ticker             # ticker | sector | industry
      from: 2026-01-01         # effective from (optional)
      to: 2026-12-31           # effective to (optional)

positions.yaml — your trade decisions (via portal or manual edit):
  positions:
    - ticker: NVDA
      status: open             # open | closed | skipped
      entry: 132.50            # YOUR actual fill price (corrected for spread)
      shares: 4                # YOUR actual size (any amount, not fixed $250)
      tp: 148.00               # YOUR chosen target (editable any time)
      exit: 149.10             # actual exit price once closed (optional)
      opened: 2026-07-20
  'skipped' = you saw the alert but didn't enter; the bot logs it and
  won't re-alert that ticker during the cooldown window.
"""
import datetime as dt

import yaml


def load_yaml(path, key):
    try:
        with open(path) as f:
            d = yaml.safe_load(f) or {}
        return d.get(key, [])
    except FileNotFoundError:
        return []


def _active(rule, today):
    f = rule.get("from")
    t = rule.get("to")
    if f and today < dt.date.fromisoformat(str(f)):
        return False
    if t and today > dt.date.fromisoformat(str(t)):
        return False
    return True


def apply_overrides(tickers: list[str], meta: dict, path, today=None) -> list[str]:
    """meta: {ticker: {'sector':..., 'industry':...}} (may be partial)."""
    today = today or dt.date.today()
    rules = load_yaml(path, "rules")
    out = list(tickers)
    for r in rules:
        if not _active(r, today):
            continue
        m = str(r.get("match", "")).lower()
        typ = r.get("type", "ticker")
        def hits(t):
            if typ == "ticker":
                return t.lower() == m
            info = meta.get(t, {})
            return str(info.get(typ, "")).lower() == m
        if r.get("action") == "exclude":
            out = [t for t in out if not hits(t)]
        elif r.get("action") == "include":
            if typ == "ticker" and r["match"].upper() not in out:
                out.append(r["match"].upper())
    return out


def open_positions(path):
    return [p for p in load_yaml(path, "positions") if p.get("status") == "open"]


def recently_decided(path, cooldown_days=3, today=None):
    """Tickers with an open position OR skipped/opened within cooldown —
    suppresses duplicate alerts for these."""
    import datetime as dt
    today = today or dt.date.today()
    out = set()
    for p in load_yaml(path, "positions"):
        t = str(p.get("ticker", "")).upper()
        if p.get("status") == "open":
            out.add(t)
        else:
            d = p.get("opened") or p.get("skipped_on")
            try:
                if d and (today - dt.date.fromisoformat(str(d))).days <= cooldown_days:
                    out.add(t)
            except Exception:
                pass
    return out
