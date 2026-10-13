"""Run all 3 strategies on BTC + ETH, print comparison table, save equity curves."""
import numpy as np
import pandas as pd

import backtest as bt

STRATS = {
    "Trend Pullback": bt.trend_pullback,
    "Mean Reversion": bt.mean_reversion,
    "Breakout": bt.breakout,
}

rows = []
curves = {}
for file, label in (("data_BTC_USD.csv", "BTC"), ("data_ETH_USD.csv", "ETH")):
    df = pd.read_csv(file, index_col=0, parse_dates=True)
    for name, fn in STRATS.items():
        try:
            sig = fn(df)
        except Exception as e:
            print(f"{label} / {name}: FAILED {e}")
            continue
        res = bt.run_backtest(df, sig)
        s = res["stats"]
        rows.append({
            "Market": label, "Strategy": name,
            "Final $": round(s["final"], 2),
            "Return %": round(s["return_pct"], 1),
            "Trades": s["trades"],
            "Win %": round(s["win_rate"], 1),
            "Profit Fctr": round(s["profit_factor"], 2) if np.isfinite(s["profit_factor"]) else "inf",
            "Max DD %": round(s["max_dd_pct"], 1),
            "Avg Hold (bars)": round(s["avg_hold_bars"], 1),
            "Avg $/trade": round(s["avg_trade"], 3),
        })
        curves[f"{label} / {name}"] = res["equity"]

table = pd.DataFrame(rows)
pd.set_option("display.width", 200)
print(table.to_string(index=False))

# --- chart ---
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, axes = plt.subplots(2, 3, figsize=(16, 8))
fig.suptitle(f"{bt.INITIAL_CAPITAL:.0f} USD starting capital | 5-min bars, 60d | 0.30% round-trip costs | 1 USD risk per trade",
             fontsize=13)
for ax, (label, curve) in zip(axes.flat, curves.items()):
    ax.plot(curve.index, curve.values, lw=1.2)
    ax.axhline(bt.INITIAL_CAPITAL, color="gray", ls="--", lw=0.8)
    ax.set_title(label, fontsize=10)
    ax.grid(alpha=0.3)
    ax.tick_params(labelsize=8)
    ax.set_ylabel("Equity ($)", fontsize=9)
fig.autofmt_xdate()
fig.tight_layout()
fig.savefig("equity_curves.png", dpi=130)
print("\nChart saved: equity_curves.png")

# --- extra: what would the best strategy's daily compounding need to look like? ---
r = 1.0 / bt.INITIAL_CAPITAL
print(f"\n--- Reality check: $1/hour on ${bt.INITIAL_CAPITAL:.0f} ---")
print(f"Required: +{r*100:.2f}%/hr compounded 6h/day = {((1+r)**6-1)*100:.0f}%/day")
best = table.sort_values("Return %", ascending=False).iloc[0]
days = 60
print(f"Best result above: {best['Market']} {best['Strategy']}: "
      f"{best['Return %']}% over {days} days = "
      f"{((best['Final $']/bt.INITIAL_CAPITAL)**(1/days)-1)*100:.2f}%/day average")
