"""Sanity checks: verify engine math on a few trades + buy-and-hold benchmark."""
import pandas as pd

import backtest as bt

df = pd.read_csv("data_BTC_USD.csv", index_col=0, parse_dates=True)
sig = bt.trend_pullback(df)
res = bt.run_backtest(df, sig)
trades = res["trades"][:5]
for t in trades:
    i = t["exit_bar"]
    print(f"exit {df.index[i]}  entry=${t['entry']:.2f} exit=${t['exit']:.2f} "
          f"units={t['units']:.6f} why={t['why']} hold={t['hold_bars']} bars pnl=${t['pnl']:.4f}")
# verify first trade's PnL by hand
t = trades[0]
gross = (t["exit"] - t["entry"]) * t["units"]
fees = (t["entry"] * t["units"] + t["exit"] * t["units"]) * bt.FEE
print(f"\nHand-check trade 0: gross=${gross:.4f} fees=${fees:.4f} net=${gross-fees:.4f} "
      f"(engine: ${t['pnl']:.4f})")

# benchmark: buy & hold, same costs
for f, lab in (("data_BTC_USD.csv", "BTC"), ("data_ETH_USD.csv", "ETH")):
    d = pd.read_csv(f, index_col=0, parse_dates=True)
    rt = (d["close"].iloc[-1] / d["close"].iloc[0] - 1) * 100
    net = (d["close"].iloc[-1] / d["close"].iloc[0] * (1 - bt.COST_RT) - 1) * 100
    print(f"{lab} buy&hold 60d: {rt:+.1f}% gross, {net:+.1f}% after one round-trip cost")
