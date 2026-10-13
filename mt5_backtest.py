"""
Forex M5 backtest on REAL broker data from MT5.

Cost model (much better than the crypto test, because micro-lots exist):
  - Costs are the REAL per-bar spread from the broker feed (buy at ask, exit long at bid)
  - Plus commission per round trip if the broker charges (per the account's deal history)
  - Position sized by $1 risk to stop; floored at 0.01 lot (broker minimum)
  - Same no-lookahead rules: signal on bar close, act at next bar open, stop-before-target
"""
import numpy as np
import pandas as pd

import backtest as bt

FEE = 0.0                      # spread IS the cost here (applied via ask/bid)
COMMISSION_RT = 0.06           # conservative: ~$0.06 round trip per 0.01 lot
RISK_PER_TRADE = 1.0
INITIAL_CAPITAL = 100.0        # generic starting capital (override to test other sizes)
MIN_LOT = 0.01
LEVERAGE = 100                 # generic assumption for the margin cap

# pip value in $ per 0.01 lot (1 standard lot = 100,000 base units; 0.01 = 1,000)
PIP_VALUE = {"EURUSD": 0.10, "GBPUSD": 0.10, "USDJPY": 0.069, "XAUUSD": 0.10}
POINT_PIP = {"EURUSD": 10, "GBPUSD": 10, "USDJPY": 100, "XAUUSD": 10}  # points per pip
# base units per 0.01 lot (forex: 1,000; gold: 1 oz since contract = 100 oz/lot)
UNITS_PER_001 = {"EURUSD": 1000, "GBPUSD": 1000, "USDJPY": 1000, "XAUUSD": 1}


def size_lots(stop_distance_pips: float, equity: float) -> float:
    """Lots risking ~$1 to the stop, floored at min lot, capped by equity."""
    per_lot_risk = stop_distance_pips * PIP_VALUE["*"]  # placeholder, replaced below
    return MIN_LOT


def run_forex(df: pd.DataFrame, sig: pd.DataFrame, symbol: str) -> dict:
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    sp = df["spread"].values  # points (in the symbol's native digits)
    n = len(df)
    point = 10 ** (-int(df["digits"].iloc[0]))

    pipval = PIP_VALUE[symbol]
    pt_per_pip = POINT_PIP[symbol]

    ent = sig["enter"].fillna(False).values
    exi = sig["exit"].fillna(False).values
    stp = sig["stop"].values
    tgt = sig["target"].values

    cash = INITIAL_CAPITAL
    lots = 0.0
    pos_stop = pos_target = entry_px = 0.0
    entry_bar = 0
    pending = None
    busted = False
    equity = np.empty(n)
    trades = []

    def exit_trade(px_bid, i, why):
        nonlocal cash, lots
        pnl_pips = (px_bid - entry_px) / point / pt_per_pip
        # pipval is $/pip per 0.01 lot, so scale lots down to micro-lot units
        pnl = pnl_pips * pipval * (lots / MIN_LOT) - COMMISSION_RT * (lots / MIN_LOT)
        cash += pnl
        trades.append({"entry_bar": entry_bar, "exit_bar": i, "lots": lots,
                       "entry": entry_px, "exit": px_bid, "pnl": pnl,
                       "why": why, "hold_bars": i - entry_bar,
                       "pips": pnl_pips})
        lots = 0.0

    for i in range(n):
        bid_open, bid_close = o[i], c[i]
        spread_pips = sp[i] * point / point / pt_per_pip  # points -> pips

        # 1) act on previous bar's signal at this bar's open
        if lots == 0 and pending == "enter" and i > 0 and not busted:
            ask_open = bid_open + sp[i - 1] * point
            eff_stop = stp[i - 1]
            stop_pips = (ask_open - eff_stop) / point / pt_per_pip
            if stop_pips > 0:
                # risk per 0.01 lot = stop_pips * pipval  (pipval = $/pip per 0.01 lot)
                lots_want = (RISK_PER_TRADE / (stop_pips * pipval)) * MIN_LOT
                # margin cap: notional per 0.01 lot = contract units * price
                notional_per_001 = UNITS_PER_001[symbol] * bid_open
                lots_cap = cash * LEVERAGE / notional_per_001
                lots = max(MIN_LOT, min(lots_want, lots_cap))
                # NOTE: if MIN_LOT forces lots >> lots_want, actual risk > $1
                entry_px = ask_open
                pos_stop, pos_target = eff_stop, tgt[i - 1]
                entry_bar = i
        elif i > 0 and lots > 0 and exi[i - 1] and not busted:
            exit_trade(bid_open, i, "signal")

        pending = "enter" if ent[i] else None

        # 2) intra-bar stop / target (MT5 convention: SL/TP trigger on bid for longs)
        if lots > 0 and not busted:
            if l[i] <= pos_stop:
                exit_trade(min(pos_stop, bid_open), i, "stop")
            elif pos_target > 0 and h[i] >= pos_target:
                exit_trade(pos_target, i, "target")

        # 3) mark-to-market; a real broker margin-calls at zero equity
        if lots > 0:
            mtm = cash + (bid_close - entry_px) / point / pt_per_pip * pipval * (lots / MIN_LOT)
            if mtm <= 0:
                exit_trade(bid_close, i, "margin_call")
                busted = True
                cash = 0.0

        equity[i] = cash if not busted else 0.0

    eq = pd.Series(equity, index=df.index)
    if lots > 0 and not busted:
        exit_trade(c[-1], n - 1, "eod")
    stats = _stats(eq, trades)
    stats["busted"] = busted
    return {"equity": eq, "trades": trades, "stats": stats}


def _stats(eq: pd.Series, trades: list) -> dict:
    final = eq.iloc[-1]
    ret = final / INITIAL_CAPITAL - 1
    peak = eq.cummax()
    dd = (eq / peak - 1).min()
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    costs_drag = sum(abs(min(t["pips"], 0)) for t in trades)  # informational
    return {
        "final": final, "return_pct": ret * 100, "max_dd_pct": dd * 100,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "profit_factor": gw / gl if gl > 0 else (np.inf if gw > 0 else 0),
        "avg_trade": np.mean([t["pnl"] for t in trades]) if trades else 0,
        "avg_pips": np.mean([t["pips"] for t in trades]) if trades else 0,
        "avg_hold_bars": np.mean([t["hold_bars"] for t in trades]) if trades else 0,
    }


if __name__ == "__main__":
    STRATS = {"Trend Pullback": bt.trend_pullback,
              "Mean Reversion": bt.mean_reversion,
              "Breakout": bt.breakout}
    rows = []
    curves = {}
    for sym in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"):
        df = pd.read_csv(f"mt5_{sym}.csv", index_col=0, parse_dates=True)
        for name, fn in STRATS.items():
            try:
                sig = fn(df)
                res = run_forex(df, sig, sym)
                s = res["stats"]
                rows.append({"Sym": sym, "Strat": name,
                             "Final $": round(s["final"], 2),
                             "Ret %": round(s["return_pct"], 1),
                             "Trades": s["trades"],
                             "Win %": round(s["win_rate"], 1),
                             "PF": round(s["profit_factor"], 2) if np.isfinite(s["profit_factor"]) else "inf",
                             "MaxDD %": round(s["max_dd_pct"], 1),
                             "AvgPips": round(s["avg_pips"], 1),
                             "AvgHold": round(s["avg_hold_bars"], 1)})
                curves[f"{sym} / {name}"] = res["equity"]
            except Exception as e:
                print(f"{sym} {name}: FAILED {type(e).__name__} {e}")
    table = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(table.to_string(index=False))

    print("\n--- buy-and-hold benchmark (same 60d, real spreads) ---")
    for sym in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"):
        df = pd.read_csv(f"mt5_{sym}.csv", index_col=0, parse_dates=True)
        pt = 10 ** (-int(df["digits"].iloc[0]))
        p0 = df["open"].iloc[0] + df["spread"].iloc[0] * pt   # buy at ask
        p1 = df["close"].iloc[-1]                              # sell at bid
        pips = (p1 - p0) / pt / POINT_PIP[sym]
        pnl = pips * PIP_VALUE[sym] * (INITIAL_CAPITAL / (UNITS_PER_001[sym] * p0))
        print(f"  {sym}: {pips:+8.1f} pips  ->  ${INITIAL_CAPITAL:+.2f} start, ${pnl:+.2f} on full balance")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(4, 3, figsize=(16, 13))
    fig.suptitle(f"MT5 broker M5 data, 60d | {INITIAL_CAPITAL:.0f} USD start | real per-bar spread | 1 USD risk, min 0.01 lot",
                 fontsize=13)
    for ax, (label, curve) in zip(axes.flat, curves.items()):
        ax.plot(curve.index, curve.values, lw=1.1)
        ax.axhline(INITIAL_CAPITAL, color="gray", ls="--", lw=0.8)
        ax.set_title(label, fontsize=9)
        ax.grid(alpha=0.3)
        ax.tick_params(labelsize=7)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("equity_mt5.png", dpi=130)
    print("\nChart saved: equity_mt5.png")
