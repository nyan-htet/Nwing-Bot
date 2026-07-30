"""cycles.py — Long-horizon seasonality & cycle context. INFORMATIVE ONLY.

Uses 40+ years of daily index data to estimate where we sit vs:
  - monthly seasonality (average month-of-year returns)
  - US presidential 4-year cycle (year 1-4 average returns)
  - decade positioning (10-year cycle, weakest evidence — reported, low weight)
Output is a context line + tailwind score in [-1, +1]. Never a trade trigger.
"""
import numpy as np
import pandas as pd


def _monthly_seasonality(df):
    r = df.set_index("time")["close"].resample("ME").last().pct_change().dropna()
    by_month = r.groupby(r.index.month).mean()
    return by_month  # avg return per calendar month


def _presidential_cycle(df):
    """Avg annual return by year-in-cycle. Election years: 1984, 1988, ... —
    cycle year 1 = post-election year."""
    yearly = df.set_index("time")["close"].resample("YE").last().pct_change().dropna()
    cyc = ((yearly.index.year - 1981) % 4) + 1  # 1981 was cycle year 1
    return yearly.groupby(cyc).mean()


def _decade_position(df):
    yearly = df.set_index("time")["close"].resample("YE").last().pct_change().dropna()
    pos = yearly.index.year % 10
    return yearly.groupby(pos).mean()


def context(df: pd.DataFrame, today=None) -> dict:
    """df: long daily history with columns time, close."""
    today = pd.Timestamp(today) if today else pd.Timestamp.today()
    mseas = _monthly_seasonality(df)
    pres = _presidential_cycle(df)
    dec = _decade_position(df)

    m = today.month
    cyc_year = ((today.year - 1981) % 4) + 1
    dpos = today.year % 10

    m_avg = float(mseas.get(m, 0.0))
    p_avg = float(pres.get(cyc_year, 0.0))
    d_avg = float(dec.get(dpos, 0.0))

    # tailwind score: month vs typical month, cycle year vs typical year
    m_z = (m_avg - mseas.mean()) / (mseas.std() or 1)
    p_z = (p_avg - pres.mean()) / (pres.std() or 1)
    d_z = (d_avg - dec.mean()) / (dec.std() or 1)
    score = float(np.clip(0.5 * m_z + 0.35 * p_z + 0.15 * d_z, -1, 1))

    label = "tailwind" if score > 0.3 else ("headwind" if score < -0.3 else "neutral")
    line = (f"Cycles [{label}]: month {m} avg {m_avg:+.1%}, "
            f"presidential-cycle year {cyc_year} avg {p_avg:+.1%}, "
            f"decade-pos {dpos} avg {d_avg:+.1%} (informative only)")
    return {"score": round(score, 2), "label": label, "line": line}
