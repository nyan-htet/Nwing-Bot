"""sweep.py — Compare strategy variants on the SAME data, one download.

Answers: "what would actually improve returns?" — by testing exit rules,
position sizing and entry filters side by side against buy & hold SPY.

Usage:
  TWELVEDATA_KEY=... python sweep.py [years] [max_stocks] [include_etfs]

Outputs: docs/sweep.json + console table ranked by final equity.
"""
import copy
import datetime as dt
import json
import sys

import backtest_hist as bt

VARIANTS = [
    ("A. Hold to target only (no thesis exit)",
     {"position_pct": 0.05, "exit_mode": "tp", "use_thesis_exit": False}),
    ("B. Same + thesis exit ON",
     {"position_pct": 0.05, "exit_mode": "tp", "use_thesis_exit": True}),
    ("C. Hold to target, stock floor 0.60",
     {"position_pct": 0.05, "exit_mode": "tp", "use_thesis_exit": False,
      "min_score_stock": 0.60}),
    ("D. Hold to target, stock floor 0.50",
     {"position_pct": 0.05, "exit_mode": "tp", "use_thesis_exit": False,
      "min_score_stock": 0.50}),
    ("E. Hold to target + require RS>0",
     {"position_pct": 0.05, "exit_mode": "tp", "use_thesis_exit": False,
      "require_rs": True}),
    ("F. Hold to target + above 200EMA",
     {"position_pct": 0.05, "exit_mode": "tp", "use_thesis_exit": False,
      "require_ema200": True}),
    ("G. Hold to target, 20 slots",
     {"position_pct": 0.05, "exit_mode": "tp", "use_thesis_exit": False,
      "max_concurrent": 20}),
    ("H. Hold to target, 10% positions",
     {"position_pct": 0.10, "exit_mode": "tp", "use_thesis_exit": False}),
    ("I. Thesis exit ON + trailing after target",
     {"position_pct": 0.05, "exit_mode": "tp_then_trail", "use_thesis_exit": True}),
]


def main():
    years = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else 6
    mx = int(sys.argv[2]) if len(sys.argv) > 2 and str(sys.argv[2]).strip() else None
    inc = (str(sys.argv[3]).strip().lower() in ("yes", "y", "true", "1")
           if len(sys.argv) > 3 and str(sys.argv[3]).strip() else True)

    base = copy.deepcopy(bt.PARAMS)
    results = []
    for i, (label, overrides) in enumerate(VARIANTS, 1):
        bt.PARAMS.clear()
        bt.PARAMS.update(base)
        bt.PARAMS.update(overrides)
        print(f"\n───── [{i}/{len(VARIANTS)}] {label} ─────")
        try:
            out = bt.run(years, mx, inc)
            s = out["stats"]
            results.append({"variant": label, "params": overrides,
                            "final_equity": s["final_equity"],
                            "total_return_pct": s["total_return_pct"],
                            "cagr_pct": s["cagr_pct"],
                            "max_drawdown_pct": s["max_drawdown_pct"],
                            "sharpe": s["sharpe"], "n_trades": s["n_trades"],
                            "win_rate_pct": s["win_rate_pct"],
                            "avg_win_pct": s["avg_win_pct"],
                            "avg_loss_pct": s["avg_loss_pct"],
                            "buy_hold_spy_pct": s.get("buy_hold_spy_pct"),
                            "vs_buy_hold_pct": s.get("vs_buy_hold_pct"),
                            "exits": s.get("exits", {})})
        except Exception as e:
            print(f"variant failed: {e}")

    results.sort(key=lambda r: r["final_equity"], reverse=True)
    bh = next((r["buy_hold_spy_pct"] for r in results if r.get("buy_hold_spy_pct")), None)

    print("\n\n=================== SWEEP RESULTS ===================")
    print(f"{'variant':<42}{'final $':>10}{'ret%':>8}{'CAGR%':>7}{'DD%':>8}"
          f"{'win%':>6}{'trades':>7}{'Sharpe':>7}")
    for r in results:
        print(f"{r['variant']:<42}{r['final_equity']:>10,.0f}{r['total_return_pct']:>8.1f}"
              f"{r['cagr_pct']:>7.1f}{r['max_drawdown_pct']:>8.1f}"
              f"{r['win_rate_pct']:>6.0f}{r['n_trades']:>7}{r['sharpe']:>7.2f}")
    if bh is not None:
        print(f"\nBuy & hold SPY over same period: {bh:+.1f}%  "
              f"(${10000 * (1 + bh / 100):,.0f} from $10,000)")
    print("\n⚠️  Picking the best row here is CURVE FITTING unless the winner also")
    print("    makes economic sense and holds up on a different period/universe.")

    import os
    os.makedirs("docs", exist_ok=True)
    with open("docs/sweep.json", "w") as f:
        json.dump({"generated": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "years": years, "max_stocks": mx, "include_etfs": inc,
                   "buy_hold_spy_pct": bh, "results": results}, f, indent=2)
    print("Saved: docs/sweep.json")


if __name__ == "__main__":
    main()
