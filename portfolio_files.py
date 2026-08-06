"""portfolio_files.py — reads overrides.yaml (include/exclude rules).

Position tracking now happens automatically via alerts_ledger.py, so
positions.yaml is no longer used.

overrides.yaml format:
  rules:
    - action: exclude          # or include
      match: TSLA              # ticker, sector, or industry name
      type: ticker             # ticker | sector | industry
      from: 2026-01-01         # optional
      to: 2026-12-31           # optional
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
    f, t = rule.get("from"), rule.get("to")
    if f and today < dt.date.fromisoformat(str(f)):
        return False
    if t and today > dt.date.fromisoformat(str(t)):
        return False
    return True


def apply_overrides(tickers: list[str], meta: dict, path, today=None) -> list[str]:
    """meta: {ticker: {'sector':..., 'industry':...}} (may be partial)."""
    today = today or dt.date.today()
    out = list(tickers)
    for r in load_yaml(path, "rules"):
        if not _active(r, today):
            continue
        m = str(r.get("match", "")).lower()
        typ = r.get("type", "ticker")

        def hits(t):
            if typ == "ticker":
                return t.lower() == m
            return str(meta.get(t, {}).get(typ, "")).lower() == m

        if r.get("action") == "exclude":
            out = [t for t in out if not hits(t)]
        elif r.get("action") == "include" and typ == "ticker":
            if r["match"].upper() not in out:
                out.append(r["match"].upper())
    return out
